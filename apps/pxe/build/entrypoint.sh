#!/bin/bash
# Homelab PXE container entrypoint.
#
# Starts:
#   * dnsmasq — TFTP server on :69/udp, root = /srv/tftp
#     (serves ipxe.efi + our menu tree)
#   * nginx   — HTTP server on :80, root = /srv/http
#     (serves cached distro/utility assets mirrored from upstream)
#
# Both run as the nbxyz user (uid 1000). Logs go to stdout so
# `docker logs pxe` shows everything.
#
# We don't use supervisord — dnsmasq forks to background if we let
# it, so we launch it with --keep-in-foreground and trap signals
# ourselves so tini can reap cleanly.

set -eu

echo "[entrypoint] Homelab PXE starting"

# Copy the image-baked iPXE binary into /srv/tftp so dnsmasq can serve
# it. /srv/tftp is bind-mounted from the NAS (rw) — it shadows anything
# the Dockerfile placed at /srv/tftp, so the binary lives at /opt/ipxe-bin
# until we put it in place here.
#
# Idempotent: `install` replaces the file atomically. If the NAS-side
# copy already matches (subsequent starts after first run), the content
# is the same but we overwrite anyway — it's <200 KB, the time cost is
# negligible.
install -o nbxyz -g nbxyz -m 0644 \
    /opt/ipxe-bin/ipxe.efi /srv/tftp/ipxe.efi
echo "[entrypoint] ipxe.efi deployed to /srv/tftp/ipxe.efi"

# Bake-in binaries that don't have direct release URLs (memtest86plus
# only ships as a zip, so we extracted during image build). Deploy
# into /srv/tftp so dnsmasq can serve them, mirroring the ipxe.efi
# pattern.
#
# NOTE: memtest86plus v8.x dropped EFI binaries — only ships a
# Linux-bootable kernel (multiboot). iPXE boots it via
# `kernel <url>; boot` rather than `chain`.
install -o nbxyz -g nbxyz -m 0644 \
    /opt/ipxe-bin/memtest.bin /srv/tftp/memtest.bin
echo "[entrypoint] memtest.bin deployed to /srv/tftp/memtest.bin"

echo "[entrypoint] dnsmasq serving TFTP on 10.10.5.10:69/udp (/srv/tftp)"
echo "[entrypoint] nginx  serving HTTP on 10.10.5.10:8080/tcp (/srv/http)"

# Launch dnsmasq in background. --port=0 disables DNS (we only want
# TFTP). --tftp-secure restricts serving to files readable by the
# dnsmasq user, which is why every file we upload to the TFTP root
# must be chowned to uid 1000 (see apps.py::ensure_pxe_menu_files).
#
# --listen-address + --bind-interfaces: we run with Docker
# network_mode=host (see docker-compose.yaml comment block). Without
# explicit binding, dnsmasq would listen on ALL host IPs, which on a
# multi-homed NAS (4 sub-interfaces: mgmt/prd/dev/home) means UDP
# replies can originate from the wrong IP — the TFTP client then
# rejects with "received packet from wrong source". Pinning to the
# mgmt VLAN IP fixes this.
dnsmasq \
    --port=0 \
    --enable-tftp \
    --tftp-root=/srv/tftp \
    --tftp-secure \
    --user=nbxyz \
    --keep-in-foreground \
    --log-facility=- \
    --log-dhcp \
    --log-queries \
    --listen-address=10.10.5.10 \
    --bind-interfaces &
DNSMASQ_PID=$!
echo "[entrypoint] dnsmasq pid=${DNSMASQ_PID}"

# Trap so we exit cleanly when the container is stopped.
trap "echo '[entrypoint] stopping'; kill ${DNSMASQ_PID} 2>/dev/null; exit 0" TERM INT

# nginx in foreground — its exit means container exits.
exec nginx -g 'daemon off;'
