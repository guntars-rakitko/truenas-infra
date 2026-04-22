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
echo "[entrypoint] dnsmasq serving TFTP from /srv/tftp (port 69/udp)"
echo "[entrypoint] nginx  serving HTTP from /srv/http (port 80)"

# Launch dnsmasq in background. --port=0 disables DNS (we only want
# TFTP). --tftp-secure restricts serving to files readable by the
# dnsmasq user, which is why every file we upload to the TFTP root
# must be chowned to uid 1000 (see apps.py::ensure_pxe_menu_files).
dnsmasq \
    --port=0 \
    --enable-tftp \
    --tftp-root=/srv/tftp \
    --tftp-secure \
    --user=nbxyz \
    --keep-in-foreground \
    --log-facility=- \
    --log-dhcp \
    --log-queries &
DNSMASQ_PID=$!
echo "[entrypoint] dnsmasq pid=${DNSMASQ_PID}"

# Trap so we exit cleanly when the container is stopped.
trap "echo '[entrypoint] stopping'; kill ${DNSMASQ_PID} 2>/dev/null; exit 0" TERM INT

# nginx in foreground — its exit means container exits.
exec nginx -g 'daemon off;'
