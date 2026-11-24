# Runbook: add a service

> Moved verbatim from CLAUDE.md § Policy for adding new services on 2026-11-22, truenas-infra#233.

### Policy for adding new services

Decision tree — **apply every time you add an HTTPS endpoint on this network**:

1. **Admin / mgmt UI a human opens in a browser?**
   → Expose through Traefik at `10.10.5.20:443`. Backend plain HTTP on
     mgmt-VLAN IP. Portless URL `<name>.w1.lv`. Add DNS record (via
     `mikrotik-infra/manage.sh` option 5, "Sync DNS static records") pointing at `10.10.5.20`.
     Add a route in `apps/traefik/routes.yaml`.
2. **Data-plane API a machine consumes (S3, gRPC, K8s API, …)?**
   → Bind directly on the service's own VLAN IP using its **native
     port** (`:443` on data-VLAN IPs is reserved for future growth).
     Mount wildcard cert from `/mnt/tank/system/tls/`. DNS record
     points at the service VLAN IP.
     An app with no TLS of its own (Bambuddy): Traefik ends TLS on that
     VLAN IP instead, on the app's native port, through an entry point
     only its routers name (`devapi` on `10.10.15.10:8000` for
     `bambuddy-api-dev.w1.lv`, API paths only). Never add a mgmt UI's
     router to such an entry point.
3. **Fundamental infra-plane UI (TrueNAS, MikroTik, switch)?**
   → Leave on the device's native port, **never proxy**. These must
     remain reachable when Traefik is down.
4. **Internet-facing?**
   → Not in scope today. When needed: separate public DNS record on
     CloudFlare, CloudFlare Tunnel or dedicated ingress — NOT through
     mgmt-VLAN Traefik.

Hostname convention: `<role>.w1.lv` for singletons, `<role>-<env>.w1.lv`
for multi-instance (minio-prd, minio-dev), `<role>-<NN>` for per-box
(msa2-prd-01). All lowercase, hyphen-separated.

---
