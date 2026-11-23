"""SMTP access: login check (config flow) and sending (M6).

Sending is never repeated. Failures before the message data was handed over (connection, login, refused sender or
recipient) mean nothing was sent; a connection loss *during or after* DATA leaves the outcome unknown
(``GmxOutcomeUnknownError``), because the server may already have accepted the message.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field

from ..const import SMTP_TIMEOUT_S, Security
from .errors import GmxConnectionError, GmxOutcomeUnknownError, GmxSmtpAuthError, GmxSmtpRejectedError, GmxTlsError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """SMTP connection parameters. The password is excluded from ``repr``."""

    host: str
    port: int
    security: Security
    username: str
    password: str = field(repr=False)


def _open(settings: SmtpSettings, context: ssl.SSLContext) -> smtplib.SMTP:
    """Connect and secure the connection; never continues without TLS (blocking)."""
    if settings.security is Security.SSL:
        return smtplib.SMTP_SSL(settings.host, settings.port, timeout=SMTP_TIMEOUT_S, context=context)
    smtp = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT_S)
    try:
        smtp.ehlo()
        if not smtp.has_extn("starttls"):
            raise GmxTlsError("STARTTLS not offered by the SMTP server")
        smtp.starttls(context=context)
        smtp.ehlo()
    except BaseException:
        smtp.close()
        raise
    return smtp


def check_smtp_login(settings: SmtpSettings, context: ssl.SSLContext) -> int | None:
    """Log in and log out again; return the announced ``SIZE`` limit in bytes, if any (blocking)."""
    smtp: smtplib.SMTP | None = None
    try:
        smtp = _open(settings, context)
        smtp.login(settings.username, settings.password)
        size = smtp.esmtp_features.get("size", "").strip()
        return int(size) if size.isdigit() else None
    except smtplib.SMTPAuthenticationError as err:
        raise GmxSmtpAuthError("SMTP login rejected") from err
    except ssl.SSLError as err:
        raise GmxTlsError(f"SMTP TLS failed: {type(err).__name__}") from err
    except (smtplib.SMTPException, OSError) as err:
        raise GmxConnectionError(f"SMTP connection failed: {type(err).__name__}") from err
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except smtplib.SMTPException, OSError:
                smtp.close()


def send_message(settings: SmtpSettings, context: ssl.SSLContext, sender: str, recipients: list[str], data: bytes) -> None:
    """Send ``data`` to ``recipients`` (blocking). All recipients must be accepted, otherwise nothing is sent."""
    smtp: smtplib.SMTP | None = None
    try:
        smtp = _open(settings, context)
        smtp.login(settings.username, settings.password)
        size = smtp.esmtp_features.get("size", "").strip()
        if size.isdigit() and len(data) > int(size):
            raise GmxSmtpRejectedError("message_too_large")
        code, _ = smtp.mail(sender, [f"SIZE={len(data)}"] if size else [])
        if code != 250:
            raise GmxSmtpRejectedError("sender_rejected")
        refused = [r for r in recipients if smtp.rcpt(r)[0] not in (250, 251)]
        if refused:
            smtp.rset()  # do not send to the rest
            raise GmxSmtpRejectedError("recipient_rejected")
    except smtplib.SMTPAuthenticationError as err:
        _close(smtp)
        raise GmxSmtpAuthError("SMTP login rejected") from err
    except ssl.SSLError as err:
        _close(smtp)
        raise GmxTlsError(f"SMTP TLS failed: {type(err).__name__}") from err
    except GmxSmtpRejectedError:
        _quit(smtp)
        raise
    except (smtplib.SMTPException, OSError) as err:
        _close(smtp)
        raise GmxConnectionError(f"SMTP failed before sending: {type(err).__name__}") from err
    assert smtp is not None
    try:
        code, _ = smtp.data(data)
    except smtplib.SMTPDataError as err:
        _quit(smtp)
        raise GmxSmtpRejectedError("message_rejected") from err
    except (smtplib.SMTPException, OSError) as err:
        _close(smtp)
        raise GmxOutcomeUnknownError(f"SMTP connection lost during DATA: {type(err).__name__}") from err
    _quit(smtp)
    if code != 250:
        raise GmxSmtpRejectedError("message_rejected")


def _quit(smtp: smtplib.SMTP | None) -> None:
    if smtp is None:
        return
    try:
        smtp.quit()
    except smtplib.SMTPException, OSError:
        smtp.close()


def _close(smtp: smtplib.SMTP | None) -> None:
    if smtp is not None:
        smtp.close()
