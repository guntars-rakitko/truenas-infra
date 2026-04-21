"""Phase: tls — internal CA + ACME DNS-01 for real certificates.

Planned:
  * Ensure TrueNAS has generated an internal CA; export its public cert to
    `docs/nas-internal-ca.pem` for client trust distribution.
  * Configure ACME (`certificateauthority.create` / `certificate.create` with
    `DNS` challenge) against the user's DNS provider to issue a real cert
    for `nas-01.w1.lv`.
  * Bind the ACME cert to the web UI (`system.general.update`).
  * Bind to MinIO endpoints via per-app compose `MINIO_SERVER_URL` (applied
    in phase 9).
"""

from __future__ import annotations

from typing import Any


def run(cli: Any, ctx: Any, only: str | None = None) -> int:
    ctx.log.info("module_stub", module="tls", phase="tls")
    raise NotImplementedError(
        "tls phase not yet implemented — see docs/plans/zesty-drifting-castle.md §Phase 3"
    )
