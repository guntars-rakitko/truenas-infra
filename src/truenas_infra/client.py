"""WebSocket client factory for the TrueNAS JSON-RPC API.

TrueNAS 25.10 uses JSON-RPC 2.0 over WebSocket as the primary API surface
(REST was deprecated in 25.04, planned removal in 26). The official Python
client handles session persistence and auth.

One connection per CLI invocation is opened by `cli.py` and passed through
every module — required for `interface.commit()` / `interface.checkin()` to
happen in the same auth session.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import structlog

if TYPE_CHECKING:
    from truenas_api_client import Client as _APIClient  # noqa: F401


@contextmanager
def connected(
    host: str,
    api_key: str,
    *,
    verify_ssl: bool = False,
) -> Iterator[Any]:
    """Open an authenticated WebSocket session; yield the client; close on exit."""
    # Imported lazily so `--help` works without the lib installed (useful for CI
    # and for discoverability before `./manage.sh` has populated the venv).
    from truenas_api_client import Client  # type: ignore[import-not-found]

    log = structlog.get_logger("truenas_infra.client")
    uri = f"wss://{host}/api/current"
    log.debug("connecting", uri=uri, verify_ssl=verify_ssl)

    client = Client(uri=uri, verify_ssl=verify_ssl)
    try:
        authenticated = client.call("auth.login_with_api_key", api_key)
        if not authenticated:
            raise RuntimeError(
                "TrueNAS auth.login_with_api_key returned false — "
                "API key may be invalid or revoked."
            )
        log.info("connected", host=host)
        yield client
    finally:
        try:
            client.close()
            log.debug("closed")
        except Exception as exc:  # noqa: BLE001
            log.warning("close_failed", error=str(exc))


# ─── HTTP file upload ────────────────────────────────────────────────────────


class UploadError(RuntimeError):
    """Raised when /_upload fails or the backing job ends in FAILED state."""


def upload_file(
    cli: Any,
    *,
    host: str,
    api_key: str,
    local_path: Path,
    remote_path: str,
    mode: int = 0o644,
    verify_ssl: bool = False,
    job_timeout: float = 60.0,
) -> int:
    """Upload `local_path` onto the NAS at `remote_path` via POST /_upload.

    Uses the TrueNAS multipart upload endpoint, which is the backing
    transport for the `filesystem.put` job — the WebSocket client can
    kick off the job but bytes have to travel over HTTP.

    `cli` is the already-authenticated WS client; we use it to wait for
    the job to finish so callers get a proper SUCCESS/FAILED signal.

    Returns the TrueNAS job_id.
    """
    log = structlog.get_logger("truenas_infra.client.upload")
    data = local_path.read_bytes()
    boundary = f"----truenas-infra-{uuid.uuid4().hex[:16]}"
    payload = json.dumps({
        "method": "filesystem.put",
        "params": [remote_path, {"mode": mode}],
    })

    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="data"\r\n\r\n')
    parts.append(payload.encode() + b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="file"; filename="'
        + local_path.name.encode()
        + b'"\r\nContent-Type: application/octet-stream\r\n\r\n'
    )
    parts.append(data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        f"https://{host}/_upload",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    log.debug("upload_request", size=len(data), remote_path=remote_path)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=job_timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise UploadError(f"/_upload HTTP {e.code}: {e.read().decode()[:500]}") from e

    try:
        job_id = json.loads(raw)["job_id"]
    except (json.JSONDecodeError, KeyError) as e:
        raise UploadError(f"/_upload returned unexpected body: {raw!r}") from e

    # Poll for completion — filesystem.put is a proper job.
    deadline = time.monotonic() + job_timeout
    while time.monotonic() < deadline:
        jobs = cli.call("core.get_jobs", [["id", "=", job_id]])
        if jobs:
            job = jobs[0]
            if job["state"] == "SUCCESS":
                log.debug("upload_success", job_id=job_id, size=len(data))
                return job_id
            if job["state"] == "FAILED":
                raise UploadError(f"filesystem.put job {job_id} FAILED: {job.get('error')}")
        time.sleep(0.3)
    raise UploadError(f"filesystem.put job {job_id} timed out after {job_timeout}s")
