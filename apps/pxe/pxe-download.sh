#!/usr/bin/env bash
# pxe-download.sh — pre-fetch PXE assets into categorized local dirs.
#
# curl (on the NAS) handles HTTPS + redirects cleanly; iPXE doesn't.
# Files land in categorized subdirs of /mnt/tank/system/pxe/http/extras:
#
#   extras/utils/<name>.iso      — rescue/disk tools (sanboot)
#   extras/distros/<name>.iso    — server install ISOs (sanboot)
#   extras/live/<name>.iso       — live desktops (sanboot)
#
# Plus a few non-ISO specials (EFI binaries, netboot kernels):
#   extras/uefishell/Shell.efi
#   extras/zbm/zfsbootmenu-recovery.EFI
#   extras/debian-13/{linux,initrd.gz}
#   extras/debian-12/{linux,initrd.gz}
#   extras/alpine/{vmlinuz-lts,initramfs-lts,modloop-lts}
#
# Idempotent: any file already present with size >= MIN is skipped.
# Delete the file to re-fetch. Operator adds a new ISO by appending
# a `fetch` line below and re-running the script.
#
# After finishing, this script calls pxe-genmenu.sh to regenerate
# the dynamic menu files so new ISOs are immediately visible.

set -uo pipefail

EXTRAS="${EXTRAS:-/mnt/tank/system/pxe/http/extras}"
LOG="${LOG:-/mnt/tank/system/apps-config/pxe/pxe-download.log}"
GENMENU="${GENMENU:-/mnt/tank/system/apps-config/pxe/pxe-genmenu.sh}"

mkdir -p "$EXTRAS" "$(dirname "$LOG")"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG" >&2; }

# fetch URL DEST_FILE [MIN_SIZE_BYTES]
fetch() {
    local url="$1" dest="$2" min="${3:-1}"
    mkdir -p "$(dirname "$dest")"
    if [[ -f "$dest" ]]; then
        local size; size=$(stat -c%s "$dest" 2>/dev/null || echo 0)
        if (( size >= min )); then
            log "SKIP  ${dest#$EXTRAS/} ($size bytes)"
            return 0
        fi
        log "REFETCH ${dest#$EXTRAS/} ($size < $min)"
    fi
    log "FETCH $url"
    if curl -fL --progress-bar -o "${dest}.tmp" "$url"; then
        mv -f "${dest}.tmp" "$dest"
        chown 1000:1000 "$dest"
        log "OK    ${dest#$EXTRAS/} ($(stat -c%s "$dest") bytes)"
    else
        log "ERR   $url — curl failed"
        rm -f "${dest}.tmp"
    fi
}

log "pxe-download starting — EXTRAS=$EXTRAS"

# ─── Tiny EFI / kernel binaries (non-ISO, special) ─────────────────

# UEFI Shell v2.2 from TianoCore EDK2
fetch "https://github.com/tianocore/edk2/raw/edk2-stable202408/ShellBinPkg/UefiShell/X64/Shell.efi" \
      "$EXTRAS/uefishell/Shell.efi" 500000

# ZFSBootMenu v3.1.0 (linux6.18) recovery image — ~87 MB
fetch "https://github.com/zbm-dev/zfsbootmenu/releases/download/v3.1.0/zfsbootmenu-recovery-x86_64-v3.1.0-linux6.18.EFI" \
      "$EXTRAS/zbm/zfsbootmenu-recovery.EFI" 50000000

# ─── Utilities (ISOs) ──────────────────────────────────────────────

# GParted Live 1.8.1-3 — ~450 MB
fetch "https://downloads.sourceforge.net/project/gparted/gparted-live-stable/1.8.1-3/gparted-live-1.8.1-3-amd64.iso" \
      "$EXTRAS/utils/gparted-live-1.8.1.iso" 100000000

# Clonezilla Live 3.2.2-15 — ~400 MB
fetch "https://downloads.sourceforge.net/project/clonezilla/clonezilla_live_stable/3.2.2-15/clonezilla-live-3.2.2-15-amd64.iso" \
      "$EXTRAS/utils/clonezilla-live-3.2.2.iso" 100000000

# SystemRescue 13.00 — ~900 MB
fetch "https://downloads.sourceforge.net/project/systemrescuecd/sysresccd-x86/13.00/systemrescue-13.00-amd64.iso" \
      "$EXTRAS/utils/systemrescue-13.00.iso" 500000000

# ShredOS 2025.11 — ~100 MB (i686 build; boots on x86_64)
fetch "https://github.com/PartialVolume/shredos.x86_64/releases/download/v2025.11_29_x86-64_0.40/shredos-2025.11_29_i686_v0.40_20260402_lite.iso" \
      "$EXTRAS/utils/shredos-2025.11.iso" 50000000

# ─── Linux netboot (kernel + initrd, small, non-ISO) ────────────────

# Debian 13 trixie — netinst kernel + initrd
fetch "http://deb.debian.org/debian/dists/trixie/main/installer-amd64/current/images/netboot/debian-installer/amd64/linux" \
      "$EXTRAS/debian-13/linux" 1000000
fetch "http://deb.debian.org/debian/dists/trixie/main/installer-amd64/current/images/netboot/debian-installer/amd64/initrd.gz" \
      "$EXTRAS/debian-13/initrd.gz" 5000000

# Debian 12 bookworm
fetch "http://deb.debian.org/debian/dists/bookworm/main/installer-amd64/current/images/netboot/debian-installer/amd64/linux" \
      "$EXTRAS/debian-12/linux" 1000000
fetch "http://deb.debian.org/debian/dists/bookworm/main/installer-amd64/current/images/netboot/debian-installer/amd64/initrd.gz" \
      "$EXTRAS/debian-12/initrd.gz" 5000000

# Alpine Linux 3.21 netboot
fetch "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/netboot/vmlinuz-lts" \
      "$EXTRAS/alpine/vmlinuz-lts" 5000000
fetch "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/netboot/initramfs-lts" \
      "$EXTRAS/alpine/initramfs-lts" 5000000
fetch "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/netboot/modloop-lts" \
      "$EXTRAS/alpine/modloop-lts" 100000000

# ─── Distribution ISOs (sanboot — server installers) ───────────────

# Ubuntu Server 24.04.4 LTS — ~3 GB
fetch "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso" \
      "$EXTRAS/distros/ubuntu-server-24.04.iso" 1000000000

# Ubuntu Server 22.04.5 LTS — ~2 GB
fetch "https://releases.ubuntu.com/22.04/ubuntu-22.04.5-live-server-amd64.iso" \
      "$EXTRAS/distros/ubuntu-server-22.04.iso" 1000000000

# ─── Live CDs (sanboot — desktop + privacy) ─────────────────────────

# Ubuntu 24.04.4 Desktop Live — ~5 GB
fetch "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-desktop-amd64.iso" \
      "$EXTRAS/live/ubuntu-desktop-24.04.iso" 1000000000

# Debian 13 Live KDE — ~3 GB
fetch "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/debian-live-13.0.0-amd64-kde.iso" \
      "$EXTRAS/live/debian-live-13-kde.iso" 1000000000

# Tails 7.6.2 — ~2 GB
fetch "https://download.tails.net/tails/stable/tails-amd64-7.6.2/tails-amd64-7.6.2.iso" \
      "$EXTRAS/live/tails-7.6.2.iso" 1000000000

# ─── Regenerate the dynamic menu files ──────────────────────────────
#
# After any download run, refresh the auto-generated .ipxe menus so
# new/removed ISOs show up in the menu immediately.

if [[ -x "$GENMENU" ]]; then
    log "running pxe-genmenu.sh..."
    "$GENMENU" 2>&1 | tee -a "$LOG"
else
    log "skipping pxe-genmenu.sh (not found at $GENMENU)"
fi

log "pxe-download complete"
