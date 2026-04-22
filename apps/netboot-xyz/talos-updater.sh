#!/bin/sh
# talos-updater.sh — fetch Talos Linux PXE images for our schematic.
#
# Default behavior: run ONE update cycle and exit.
#   1. Register (or reuse) our schematic ID at factory.talos.dev.
#   2. Resolve target Talos version (either fixed or GitHub "latest").
#   3. Download vmlinuz + initramfs from pxe.factory.talos.dev for that
#      version + schematic.
#   4. Enforce retention: keep newest N version directories, delete older.
#   5. Render the interactive iPXE menu listing ALL remaining versions.
#
# Invocation by our TrueNAS cronjob sets the knobs via env vars:
#   TALOS_VERSION=latest|vX.Y.Z   (default: latest)
#   RETENTION=<int>               (default: 5)
#   ARCH=amd64|arm64              (default: amd64)
#   PLATFORM=metal|aws|...        (default: metal)
#
# Dependencies: curl, jq.
set -eu

SCHEMATIC_FILE="${SCHEMATIC_FILE:-/mnt/tank/system/apps-config/talos-updater/schematic.yaml}"
ASSETS_DIR="${ASSETS_DIR:-/mnt/tank/system/pxe/assets/talos}"
MENU_DIR="${MENU_DIR:-/mnt/tank/system/pxe/config/menus/remote}"
STATE_FILE="${STATE_FILE:-/mnt/tank/system/apps-config/talos-updater/state}"
NAS_IP="${NAS_IP:-10.10.5.10}"
UPDATE_INTERVAL="${UPDATE_INTERVAL:-86400}"
ARCH="${ARCH:-amd64}"
PLATFORM="${PLATFORM:-metal}"
TALOS_VERSION="${TALOS_VERSION:-latest}"
RETENTION="${RETENTION:-5}"

# Factory-recommended kernel command line (mirrors what
# https://pxe.factory.talos.dev/pxe/<sid>/<ver>/metal-<arch> returns).
KERNEL_ARGS="talos.platform=${PLATFORM} console=tty0 init_on_alloc=1 slab_nomerge pti=on consoleblank=0 nvme_core.io_timeout=4294967295 printk.devkmsg=on selinux=1 module.sig_enforce=1"

mkdir -p "$ASSETS_DIR" "$MENU_DIR" "$(dirname "$STATE_FILE")"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

get_schematic_id() {
    # Cached in state unless schematic.yaml has changed. The cache key is
    # the sha256 of the schematic file — if it changes, we re-register
    # AND nuke cached kernel+initramfs dirs so the next download pulls
    # the new schematic's binaries rather than stale ones.
    schematic_sha=$(sha256sum "$SCHEMATIC_FILE" 2>/dev/null | cut -d' ' -f1)
    if [ -f "$STATE_FILE" ]; then
        cached_sha=$(grep '^SCHEMATIC_SHA=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
        cached_sid=$(grep '^SCHEMATIC_ID=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
        if [ -n "$cached_sha" ] && [ "$cached_sha" = "$schematic_sha" ] && [ -n "$cached_sid" ]; then
            echo "$cached_sid"
            return 0
        fi
    fi
    log "Registering schematic with factory.talos.dev..."
    sid=$(curl -sfX POST --data-binary "@${SCHEMATIC_FILE}" \
              -H "Content-Type: application/yaml" \
              https://factory.talos.dev/schematics | jq -r '.id')
    if [ -z "$sid" ] || [ "$sid" = "null" ]; then
        log "ERROR: could not obtain schematic ID"
        return 1
    fi
    # Any registration (new OR changed) invalidates previously-downloaded
    # kernel+initramfs pairs — they were built for a DIFFERENT schematic
    # and would boot into the wrong extension set. Nuke every v*/ dir
    # under ASSETS_DIR. A single dot-file marker (.sid) in the assets
    # root tracks which SID the current images were built for, so this
    # also triggers when someone runs the updater after manually wiping
    # the state file.
    current_sid_marker=$(cat "$ASSETS_DIR/.sid" 2>/dev/null || true)
    if [ "$current_sid_marker" != "$sid" ]; then
        log "Schematic ID changed (was '${current_sid_marker:-<none>}', now '$sid'); wiping cached images."
        rm -rf "$ASSETS_DIR"/v*
        echo "$sid" > "$ASSETS_DIR/.sid"
    fi
    # Write both SID and the file sha into state so we detect edits.
    {
        echo "SCHEMATIC_ID=$sid"
        echo "SCHEMATIC_SHA=$schematic_sha"
    } > "$STATE_FILE"
    log "Registered schematic: $sid"
    echo "$sid"
}

resolve_version() {
    if [ "$TALOS_VERSION" = "latest" ]; then
        curl -sf https://api.github.com/repos/siderolabs/talos/releases/latest \
            | jq -r '.tag_name'
    else
        echo "$TALOS_VERSION"
    fi
}

download_if_missing() {
    sid="$1"; ver="$2"
    dir="$ASSETS_DIR/$ver"
    mkdir -p "$dir"
    if [ ! -f "$dir/vmlinuz-${ARCH}" ]; then
        log "Downloading Talos $ver kernel ($ARCH)..."
        curl -fL -o "$dir/vmlinuz-${ARCH}.tmp" \
            "https://pxe.factory.talos.dev/image/${sid}/${ver}/kernel-${ARCH}"
        mv "$dir/vmlinuz-${ARCH}.tmp" "$dir/vmlinuz-${ARCH}"
    fi
    if [ ! -f "$dir/initramfs-${ARCH}.xz" ]; then
        log "Downloading Talos $ver initramfs ($ARCH)..."
        curl -fL -o "$dir/initramfs-${ARCH}.xz.tmp" \
            "https://pxe.factory.talos.dev/image/${sid}/${ver}/initramfs-${ARCH}.xz"
        mv "$dir/initramfs-${ARCH}.xz.tmp" "$dir/initramfs-${ARCH}.xz"
    fi
    # Write a tiny metadata file so the menu can display timestamps.
    date -u +%Y-%m-%d > "$dir/.fetched-at"
}

# Keep only the newest $RETENTION version directories. Sort descending by
# version string (semver-ish — works for vMAJOR.MINOR.PATCH), drop the tail.
prune_old_versions() {
    log "Applying retention: keep newest $RETENTION version(s)"
    # List version dirs, skip non-v* entries, sort version-aware desc.
    versions=$(ls -1 "$ASSETS_DIR" 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+' | sort -V -r)
    kept=0
    for v in $versions; do
        kept=$((kept + 1))
        if [ "$kept" -gt "$RETENTION" ]; then
            log "  pruning: $v"
            rm -rf "$ASSETS_DIR/$v"
        fi
    done
}

# Render two iPXE files:
#   1. /mnt/tank/system/pxe/config/menus/remote/talos.ipxe
#      Overrides netboot.xyz's bundled talos.ipxe so selecting the
#      default "Talos" flow from the linux sub-menu boots locally.
#   2. /mnt/tank/system/pxe/assets/talos-menu.ipxe
#      Standalone rich menu served via HTTP :8080, chained from our
#      homelab custom.ipxe. Shows schematic, extensions, args, version
#      picker.
render_menus() {
    sid="$1"; default_ver="$2"

    # Gather available versions (descending).
    versions=$(ls -1 "$ASSETS_DIR" 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+' | sort -V -r)
    # Extract schematic extensions for display — parse officialExtensions: from schematic.yaml.
    ext_list=$(awk '/officialExtensions:/,/^[^ ]/' "$SCHEMATIC_FILE" 2>/dev/null \
                    | grep -E '^\s*- ' | sed 's#.*siderolabs/##' | tr '\n' ',' | sed 's/,$//; s/,/, /g')
    [ -z "$ext_list" ] && ext_list="(none)"

    # ─── File 1: /config/menus/remote/talos.ipxe (netboot.xyz drop-in) ───
    # `imgfree` mirrors what factory.talos.dev's own PXE endpoint does —
    # releases any previously-loaded images so the new kernel/initrd
    # pair replaces them cleanly. Without this, chaining here from a
    # menu that already loaded a kernel can leave iPXE in a weird state.
    cat > "$MENU_DIR/talos.ipxe" <<IPXE
#!ipxe
# Auto-generated by talos-updater. Overrides netboot.xyz's default.
# Schematic: $sid  |  Version: $default_ver  |  Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

:talos
imgfree
echo Booting Talos $default_ver (local, schematic $sid)
kernel http://${NAS_IP}:8080/talos/${default_ver}/vmlinuz-${ARCH} ${KERNEL_ARGS}
initrd http://${NAS_IP}:8080/talos/${default_ver}/initramfs-${ARCH}.xz
boot
IPXE
    log "Wrote $MENU_DIR/talos.ipxe"

    # ─── File 2: /assets/talos-menu.ipxe (the rich homelab menu) ─────────
    talos_menu="$ASSETS_DIR/../talos-menu.ipxe"
    {
        cat <<HEADER
#!ipxe
# Auto-generated by talos-updater — do not edit manually.
# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Re-generates on every updater run; to change layout, edit
# render_menus() in apps/netboot-xyz/talos-updater.sh and re-apply.

:start
clear menu
menu Homelab Talos Boot (local)
item --gap ─── Schematic ─────────────────────────────────
item --gap   ID: $sid
item --gap   Extensions: $ext_list
item --gap   Platform: $PLATFORM  Arch: $ARCH
item --gap
item --gap ─── Available versions (newest first) ─────────
HEADER

        default_shown=""
        for v in $versions; do
            fetched=$(cat "$ASSETS_DIR/$v/.fetched-at" 2>/dev/null || echo "?")
            # Compute total size in MiB
            size_mb=$(du -sm "$ASSETS_DIR/$v" 2>/dev/null | awk '{print $1}')
            tag=""
            if [ "$v" = "$default_ver" ] && [ -z "$default_shown" ]; then
                tag="(default)"
                default_shown="1"
            fi
            label="${v}  (fetched ${fetched}, ${size_mb} MB) ${tag}"
            printf 'item boot-%s   %s\n' "$v" "$label"
        done

        cat <<'FOOTER_ARGS_HEADER'
item --gap
item --gap ─── Kernel command line ────────────────────────
FOOTER_ARGS_HEADER
        # Wrap kernel args so they don't run off the screen.
        echo "$KERNEL_ARGS" | fold -sw 60 | while read -r line; do
            printf 'item --gap   %s\n' "$line"
        done

        cat <<FOOTER_NAV
item --gap
item --gap ─── Navigation ─────────────────────────────────
item return   <-- Return to netboot.xyz main menu

choose --default boot-${default_ver} selection || goto return
goto \${selection}

FOOTER_NAV

        # Per-version boot labels. `imgfree` mirrors factory.talos.dev's
        # PXE endpoint — frees previously-loaded images so the new kernel
        # pair replaces them cleanly. Without this, chaining from a menu
        # that loaded other images can leave iPXE in a weird state (can
        # manifest as immediate reboot after Talos kernel starts).
        for v in $versions; do
            cat <<BOOT
:boot-${v}
imgfree
echo Booting Talos ${v} (local, schematic ${sid})
kernel http://${NAS_IP}:8080/talos/${v}/vmlinuz-${ARCH} ${KERNEL_ARGS}
initrd http://${NAS_IP}:8080/talos/${v}/initramfs-${ARCH}.xz
boot

BOOT
        done

        cat <<TAIL
:return
exit
TAIL
    } > "$talos_menu"
    log "Wrote $talos_menu ($(wc -c < "$talos_menu") bytes)"
}

update_once() {
    sid=$(get_schematic_id) || return 1
    ver=$(resolve_version) || { log "ERROR: could not resolve target version"; return 1; }
    log "Target Talos: $ver (${TALOS_VERSION})  |  Schematic: $sid"
    download_if_missing "$sid" "$ver"
    prune_old_versions
    render_menus "$sid" "$ver"
    log "Update cycle complete for Talos $ver"
}

# Entry point. Default: one-shot. Passing `--loop` runs forever (sidecar mode).
MODE="${1:-oneshot}"
case "$MODE" in
    --loop|loop)
        log "talos-updater starting in daemon mode (interval=${UPDATE_INTERVAL}s)"
        while true; do
            if ! update_once; then
                log "update cycle FAILED; retrying in 5 min"
                sleep 300
                continue
            fi
            sleep "$UPDATE_INTERVAL"
        done
        ;;
    oneshot|--oneshot)
        log "talos-updater starting in one-shot mode"
        log "  TALOS_VERSION=$TALOS_VERSION  RETENTION=$RETENTION  ARCH=$ARCH  PLATFORM=$PLATFORM"
        update_once
        log "talos-updater exiting"
        ;;
    *)
        echo "Unknown mode: $MODE (expected --loop or --oneshot)" >&2
        exit 2
        ;;
esac
