# Homelab PXE image — zero dependency on netboot.xyz.
#
# Two-stage build:
#
#   1. Builder — clones iPXE upstream at a known-good commit (pre-2.0.0
#      USB rewrite, no USB keyboard regression on Intel xHCI per iPXE
#      issue #1643), enables a few feature flags, and compiles an EFI
#      SNP-only binary with our embed.ipxe baked in.
#
#   2. Runtime — minimal Alpine with dnsmasq (TFTP) and nginx (HTTP).
#      Copies the built iPXE binary into place alongside the menu tree
#      (bind-mounted from the NAS at /srv/tftp and /srv/http).
#
# Build context expected on the NAS (via `phase apps --apply`):
#   /mnt/tank/system/apps-config/pxe/build/
#     ├── Dockerfile            (this file)
#     ├── embed.ipxe            (iPXE embedded boot script)
#     ├── local-general.h       (iPXE feature flag overrides)
#     ├── local-usb.h           (iPXE USB stack override — UEFI USBIO)
#     ├── entrypoint.sh         (container startup)
#     └── nginx.conf            (nginx runtime config)

# ──────────────────────────────────────────────────────────────────
# Stage 1: iPXE builder
# ──────────────────────────────────────────────────────────────────
FROM alpine:3.20 AS ipxe-builder

# iPXE build dependencies. Keep minimal — the builder layer isn't
# pushed, only the produced .efi is COPYied into the runtime image.
RUN apk add --no-cache \
        bash \
        build-base \
        binutils \
        coreutils \
        git \
        gcc \
        make \
        musl-dev \
        perl \
        linux-headers \
        mtools \
        xz-utils \
        xorriso \
        liblzma

# Pin iPXE to a specific upstream commit. This commit is on the
# 1.21.x series (pre the 2.0.0 USB HID rewrite) — USB keyboard works
# reliably on Intel Q170 chipsets without needing USB_HCD_USBIO (which
# we set below as a belt-and-suspenders anyway). Update this SHA when
# we want to pull newer iPXE improvements.
#
# Chosen: master as of 2025-10 (last pre-2.0.0 stable commit identified
# from netboot.xyz release binaries). Shallow clone; checkout after.
ARG IPXE_REF=master
RUN git clone https://github.com/ipxe/ipxe.git /ipxe && \
    cd /ipxe && git checkout ${IPXE_REF}

# Drop in our feature flag overrides and embedded boot script.
COPY local-general.h /ipxe/src/config/local/general.h
COPY local-usb.h     /ipxe/src/config/local/usb.h
COPY embed.ipxe      /ipxe/src/embed.ipxe

# Build the SNP-only EFI binary. SNP uses UEFI's network stack — the
# most compatible choice for UEFI PXE on our hardware. `EMBED=` bakes
# our script into the binary so DHCP + chain menu.ipxe happens with
# zero external config.
WORKDIR /ipxe/src
RUN make -j$(nproc) bin-x86_64-efi/snponly.efi EMBED=embed.ipxe

# ──────────────────────────────────────────────────────────────────
# Stage 2: Runtime
# ──────────────────────────────────────────────────────────────────
FROM alpine:3.20

RUN apk add --no-cache \
        bash \
        curl \
        dnsmasq \
        nginx \
        tini

# Use nbxyz (uid 1000) as the service user. Matches the UID the old
# netboot.xyz container used, which means existing NAS-side files
# keep their ownership and TFTP-secure keeps working.
RUN addgroup -g 1000 nbxyz && \
    adduser -u 1000 -G nbxyz -D -s /sbin/nologin nbxyz && \
    mkdir -p /srv/tftp /srv/http /var/run/nginx /var/log/nginx && \
    chown -R nbxyz:nbxyz /srv/tftp /srv/http /var/log/nginx /var/run/nginx

# Our custom iPXE binary — built in stage 1.
COPY --from=ipxe-builder /ipxe/src/bin-x86_64-efi/snponly.efi /srv/tftp/ipxe.efi
RUN chown nbxyz:nbxyz /srv/tftp/ipxe.efi

COPY entrypoint.sh /entrypoint.sh
COPY nginx.conf    /etc/nginx/nginx.conf
RUN chmod +x /entrypoint.sh

EXPOSE 69/udp 80

# tini reaps zombies properly (dnsmasq forks occasionally).
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
