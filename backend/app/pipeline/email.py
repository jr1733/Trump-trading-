"""Email delivery.

Phase 1 ships the disabled-by-default path and the failure handling that the
rest of the system depends on: **email is switched off automatically when SMTP
credentials are absent**, and a send failure is logged and never propagates to
event processing. The digest content and scheduling land in Phase 3.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from ..config import settings
from ..models import EmailDeliveryLog, NotificationDelivery, utcnow

log = logging.getLogger(__name__)


class EmailSender:
    """Thin SMTP wrapper. Inject `transport` in tests."""

    def __init__(self, transport=None) -> None:
        self._transport = transport

    @property
    def enabled(self) -> bool:
        # No host or no from-address means email is off. There is no way to
        # accidentally half-enable it.
        return bool(self._transport) or settings.email_enabled

    def _send(self, message: EmailMessage) -> None:
        if self._transport is not None:
            self._transport.send_message(message)
            return
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            server.starttls()
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)

    def send(
        self,
        db: Session,
        *,
        user_id: str,
        to_address: str | None,
        subject: str,
        body: str,
        notification_id: str | None = None,
    ) -> EmailDeliveryLog:
        """Attempt one email. Always returns a log row; never raises."""
        entry = EmailDeliveryLog(
            user_id=user_id,
            notification_id=notification_id,
            to_address=to_address or "",
            subject=subject[:320],
        )

        if not self.enabled:
            entry.status = "DISABLED"
            entry.error = "SMTP is not configured; email delivery is disabled"
        elif not to_address:
            entry.status = "FAILED"
            entry.error = "no destination address configured for this user"
        else:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = settings.email_from or "noreply@localhost"
            message["To"] = to_address
            message.set_content(body)
            try:
                self._send(message)
                entry.status = "SENT"
            except Exception as exc:
                # A dead SMTP server must not stop the pipeline or lose the
                # notification -- the in-app copy already exists.
                log.warning("email delivery failed: %s", exc)
                entry.status = "FAILED"
                entry.error = f"{type(exc).__name__}: {exc}"[:2000]

        db.add(entry)
        if notification_id:
            db.add(
                NotificationDelivery(
                    notification_id=notification_id,
                    channel="email",
                    status=(
                        "SENT"
                        if entry.status == "SENT"
                        else ("UNAVAILABLE" if entry.status == "DISABLED" else "FAILED")
                    ),
                    provider_response=entry.error,
                    sent_at=utcnow() if entry.status == "SENT" else None,
                    failed_at=utcnow() if entry.status == "FAILED" else None,
                )
            )
        db.flush()
        return entry
