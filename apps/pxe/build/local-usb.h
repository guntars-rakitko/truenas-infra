/*
 * iPXE USB HCD override for the Homelab build.
 *
 * This file is copied to src/config/local/usb.h inside the iPXE tree
 * during the Docker build. Flags here override defaults in
 * src/config/usb.h.
 *
 * USB_HCD_USBIO tells iPXE to use UEFI's USB HID drivers for keyboard
 * / mouse input instead of iPXE's native USB HID stack. This is the
 * defensive fix for iPXE issue #1643 — the native HID driver
 * introduced in the iPXE 2.0.0 series has an xHCI regression on
 * Intel Q170-class chipsets (our Beelink K8s nodes) that leaves the
 * menu unnavigable from USB keyboard.
 *
 * For the pre-2.0.0 iPXE we pin in the Dockerfile this flag is
 * redundant, but it's cheap and future-proofs us if iPXE HEAD is
 * ever bumped past the rewrite.
 */

#define USB_HCD_USBIO
