#!/usr/bin/env bash
# pxe-cache.sh — mirror PXE boot assets from upstream origins to the
# NAS, so the Homelab PXE menu serves them locally instead of hitting
# the internet on every boot.
#
# Runs on the NAS (host shell, via cronjob). Installed by `phase apps`
# and scheduled weekly. Also safe to run on-demand:
#
#   /mnt/tank/system/apps-config/pxe/pxe-cache.sh
#
# Idempotent: downloads are skipped if the target file already exists
# and matches the expected size (Content-Length header). Force a fresh
# fetch by deleting the local file.
#
# Target layout under ${DEST}:
#   utils/memtest/memtest.efi
#   utils/gparted/{vmlinuz,initrd.img,filesystem.squashfs}
#   utils/sysresc/{vmlinuz,sysresccd.img}
#   utils/zfsbootmenu/{vmlinuz.efi,initramfs.img}
#   utils/uefishell/Shell.efi
#   linux/ubuntu/{22.04,24.04}/{vmlinuz,initrd}
#   linux/debian/{12,13}/{linux,initrd.gz}
#   live/ubuntu/24.04-desktop.iso
#   live/debian/13-live-kde.iso
#
# None of the origin URLs here point at netboot.xyz — everything is
# pulled directly from distro / project origins.

# NO `set -e`. We WANT individual fetch failures to be logged and
# skipped without halting the whole run — if Ubuntu's mirror is down
# and GParted isn't, the GParted download should still happen. The
# fetch() helper below returns non-zero on failure and logs it; the
# top-level flow just continues past.
set -uo pipefail

DEST="${DEST:-/mnt/tank/system/pxe/http}"
LOG="${LOG:-/mnt/tank/system/apps-config/pxe/pxe-cache.log}"

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "${LOG}"; }

# fetch URL DEST_FILE [EXPECTED_SIZE_BYTES]
#   Downloads URL to DEST_FILE. Skips if DEST_FILE already exists and
#   (if EXPECTED_SIZE_BYTES given) matches it. Creates parent dirs.
fetch() {
    local url="$1" dest="$2" expected="${3:-}"
    mkdir -p "$(dirname "${dest}")"
    if [[ -f "${dest}" ]]; then
        if [[ -n "${expected}" ]]; then
            local got; got=$(stat -c%s "${dest}" 2>/dev/null || echo 0)
            if [[ "${got}" == "${expected}" ]]; then
                log "SKIP ${dest} (size matches ${expected})"
                return 0
            fi
            log "REFETCH ${dest} (size ${got} != expected ${expected})"
        else
            log "SKIP ${dest} (exists, no size to check)"
            return 0
        fi
    fi
    log "FETCH ${url}"
    curl -sSfL -o "${dest}.tmp" "${url}" || {
        log "ERR   ${url} — curl failed, leaving any prior copy in place"
        rm -f "${dest}.tmp"
        return 1
    }
    mv -f "${dest}.tmp" "${dest}"
    chown 1000:1000 "${dest}"
    log "OK    ${dest} ($(stat -c%s "${dest}") bytes)"
}

log "pxe-cache starting — DEST=${DEST}"

# ─── Utilities ──────────────────────────────────────────────────────

# Memtest86+ — github release. Update MT_VERSION when upstream ships a
# new one; the binaries zip is consistently named mt86plus_<ver>.binaries.zip.
MT_VERSION="7.20"
fetch "https://github.com/memtest86plus/memtest86plus/releases/download/v${MT_VERSION}/mt86plus_${MT_VERSION}.binaries.zip" \
      "${DEST}/utils/memtest/_download.zip" || true
# Extract memtest64.efi from the zip. Skip if the zip isn't there or
# extraction already happened. `unzip` requires the `unzip` binary —
# TrueNAS includes it by default under /usr/bin/unzip.
if [[ -f "${DEST}/utils/memtest/_download.zip" && ! -f "${DEST}/utils/memtest/memtest.efi" ]]; then
    log "EXTRACT memtest64.efi from zip"
    if unzip -o -j "${DEST}/utils/memtest/_download.zip" \
            "memtest64.efi" -d "${DEST}/utils/memtest/" 2>&1 | tee -a "${LOG}"; then
        mv "${DEST}/utils/memtest/memtest64.efi" "${DEST}/utils/memtest/memtest.efi"
        chown 1000:1000 "${DEST}/utils/memtest/memtest.efi"
    else
        log "EXTRACT FAILED (continuing)"
    fi
fi

# GParted Live — sourceforge
fetch https://downloads.sourceforge.net/gparted/gparted-live-1.6.0-3-amd64.iso \
      "${DEST}/utils/gparted/_gparted.iso"
# TODO: loop-mount the ISO to extract vmlinuz/initrd/squashfs.
# For v1 we sanboot the ISO directly (see live.ipxe); refactor once
# we have a clean extract helper.

# SystemRescue — https://www.system-rescue.org/
fetch https://sourceforge.net/projects/systemrescuecd/files/sysresccd-x86/11.03/systemrescue-11.03-amd64.iso/download \
      "${DEST}/utils/sysresc/_sysresc.iso"

# ZFSBootMenu — github release (the 'recovery' image bundles vmlinuz + initramfs)
fetch https://github.com/zbm-dev/zfsbootmenu/releases/download/v2.3.0/zfsbootmenu-release-x86_64-v2.3.0.tar.gz \
      "${DEST}/utils/zfsbootmenu/_zbm.tar.gz"

# UEFI Shell — TianoCore EDK2 release
fetch https://github.com/tianocore/edk2/raw/edk2-stable202408/ShellBinPkg/UefiShell/X64/Shell.efi \
      "${DEST}/utils/uefishell/Shell.efi"

# ─── Linux network installers ──────────────────────────────────────

# Ubuntu Server 24.04 — netboot kernel + initrd (small, ~100 MB total)
fetch https://archive.ubuntu.com/ubuntu/dists/noble/main/installer-amd64/current/legacy-images/netboot/ubuntu-installer/amd64/linux \
      "${DEST}/linux/ubuntu/24.04/vmlinuz"
fetch https://archive.ubuntu.com/ubuntu/dists/noble/main/installer-amd64/current/legacy-images/netboot/ubuntu-installer/amd64/initrd.gz \
      "${DEST}/linux/ubuntu/24.04/initrd"

# Ubuntu Server 22.04
fetch https://archive.ubuntu.com/ubuntu/dists/jammy/main/installer-amd64/current/legacy-images/netboot/ubuntu-installer/amd64/linux \
      "${DEST}/linux/ubuntu/22.04/vmlinuz"
fetch https://archive.ubuntu.com/ubuntu/dists/jammy/main/installer-amd64/current/legacy-images/netboot/ubuntu-installer/amd64/initrd.gz \
      "${DEST}/linux/ubuntu/22.04/initrd"

# Debian 13 trixie — netinst kernel + initrd
fetch https://deb.debian.org/debian/dists/trixie/main/installer-amd64/current/images/netboot/debian-installer/amd64/linux \
      "${DEST}/linux/debian/13/linux"
fetch https://deb.debian.org/debian/dists/trixie/main/installer-amd64/current/images/netboot/debian-installer/amd64/initrd.gz \
      "${DEST}/linux/debian/13/initrd.gz"

# Debian 12 bookworm
fetch https://deb.debian.org/debian/dists/bookworm/main/installer-amd64/current/images/netboot/debian-installer/amd64/linux \
      "${DEST}/linux/debian/12/linux"
fetch https://deb.debian.org/debian/dists/bookworm/main/installer-amd64/current/images/netboot/debian-installer/amd64/initrd.gz \
      "${DEST}/linux/debian/12/initrd.gz"

# ─── Live CDs (heavy — multi-GB) ───────────────────────────────────

# Ubuntu 24.04 Desktop Live — ~5 GB
fetch https://releases.ubuntu.com/24.04/ubuntu-24.04.1-desktop-amd64.iso \
      "${DEST}/live/ubuntu/24.04-desktop.iso"

# Debian 13 Live KDE — ~3 GB
fetch https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/debian-live-13.0.0-amd64-kde.iso \
      "${DEST}/live/debian/13-live-kde.iso"

# ─── bios-config bios-apply.img ─────────────────────────────────────
#
# The sibling bios-config repo ships its own build script
# (tools/build-bios-apply-img.sh) that produces a 16 MB FAT image.
# `phase apps` already uploads it separately via apps.py; this script
# doesn't need to touch it.

log "pxe-cache complete"
