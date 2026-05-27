"""SMTP email send via AWS SES (or any SMTP server).

Used by the daily-digest summary path to deliver a markdown summary
to the operator's inbox. Credentials are sourced from Doppler keys
mirrored from `infrastructure/shr` (`SHARED_SES_W1_*`) so the cluster-
agent + Alertmanager + every other system sender share one credential
pair — see kube-infra/CLAUDE.md "Email (AWS SES)" section.

Transport: stdlib `smtplib` + `email.message.EmailMessage`. No extra
package dep — keeps the venv self-heal check simple.

Auth: STARTTLS on port 587 (SES SMTP requirement). The SES SMTP
password is HMAC-SHA256-derived from the IAM access key; operator
generates it via the SES console and stores both the IAM and the
SMTP-derived password in the same Doppler record.

Idempotency: not applicable — each send is a one-shot. Caller is
responsible for not calling repeatedly with the same body.
"""
from __future__ import annotations
import logging
import os
import smtplib
from email.message import EmailMessage

from .audit import audit


log = logging.getLogger(__name__)


def _required_env(key: str) -> str:
    """Read a required env var, raising a descriptive error if missing.

    Better than letting smtplib fail mid-connection with a confusing
    `None` value — points the operator at the Doppler key that needs
    setting.
    """
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(
            f"email.send: required env var {key!r} is not set. "
            f"Verify it's present in `doppler secrets get {key} "
            f"--project cluster-agent --config prd --plain`."
        )
    return val


@audit(tool="email_send")
def send_email(
    *,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    from_addr: str | None = None,
) -> None:
    """Send a single email via SES SMTP.

    Args:
        to:        Single recipient email (no list — keep it simple).
        subject:   Subject line.
        body_text: Plain-text body (markdown source renders OK as-is).
        body_html: Optional HTML alternative — clients prefer this
                   when both are present.
        from_addr: Optional override; defaults to SHARED_SES_W1_FROM_DEFAULT.

    Raises:
        RuntimeError: if any required SMTP env var is missing.
        smtplib.SMTPException + subclasses: on SES-side errors.
    """
    host = _required_env("SES_SMTP_HOST")
    port = int(_required_env("SES_SMTP_PORT"))
    user = _required_env("SES_SMTP_USERNAME")
    password = _required_env("SES_SMTP_PASSWORD")
    sender = from_addr or _required_env("SES_FROM_DEFAULT")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    # SES SMTP endpoint accepts STARTTLS on port 587. Plain SMTP (25)
    # and direct TLS (465) are NOT supported per SES docs. Connection
    # times out at 30s — SES is normally <1s per send, so this catches
    # routing failures fast without blocking the digest pipeline.
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)
    log.info("email.send: delivered to %s via %s:%s", to, host, port)
