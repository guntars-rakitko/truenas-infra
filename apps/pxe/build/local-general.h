/*
 * iPXE feature flag overrides for the Homelab build.
 *
 * This file is copied to src/config/local/general.h inside the iPXE
 * tree during the Docker build. Flags here override defaults in
 * src/config/general.h.
 *
 * Keep tight — every enabled feature adds binary size and potential
 * firmware-compat bugs. We only enable what we actually use.
 */

/* HTTPS — needed when chaining https:// URLs (e.g. direct chain to
 * upstream menu fallbacks, or fetching large assets served by the
 * NAS over Traefik). Default is disabled. */
#define DOWNLOAD_PROTO_HTTPS

/* Image format: PXE + EFI are built-in; no need for COMBOOT / NBI /
 * SDI / etc. Keep them disabled to shrink the binary. */

/* Menu system — default enabled, explicit here for clarity. */
#define IMAGE_EFI
