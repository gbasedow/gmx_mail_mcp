"""Tests for the SMTP login check."""

from __future__ import annotations

import smtplib
import ssl

import pytest

from custom_components.gmx_mail_mcp.client.errors import GmxConnectionError, GmxSmtpAuthError, GmxTlsError
from custom_components.gmx_mail_mcp.client.smtp import SmtpSettings, check_smtp_login
from custom_components.gmx_mail_mcp.const import Security

from .fakes import EMAIL, PASSWORD, FakeSmtpServer

STARTTLS = SmtpSettings(host="mail.gmx.net", port=587, security=Security.STARTTLS, username=EMAIL, password=PASSWORD)
IMPLICIT = SmtpSettings(host="mail.gmx.net", port=465, security=Security.SSL, username=EMAIL, password=PASSWORD)


def test_repr_hides_password() -> None:
    """No password in reprs."""
    assert PASSWORD not in repr(STARTTLS)


def test_starttls_login_returns_size(smtp_server: FakeSmtpServer) -> None:
    """587 + STARTTLS: SIZE is read after TLS; the session is closed with QUIT."""
    assert check_smtp_login(STARTTLS, ssl.create_default_context()) == 141557760
    assert smtp_server.sessions[0].quit_called


def test_implicit_tls_login(smtp_server: FakeSmtpServer) -> None:
    """465 with implicit TLS; a missing SIZE yields None."""
    smtp_server.size = ""
    assert check_smtp_login(IMPLICIT, ssl.create_default_context()) is None


def test_no_starttls_never_sends_credentials(smtp_server: FakeSmtpServer) -> None:
    """Without STARTTLS the check aborts before the login."""
    smtp_server.starttls_offered = False
    with pytest.raises(GmxTlsError):
        check_smtp_login(STARTTLS, ssl.create_default_context())
    assert smtp_server.sessions[0].closed


def test_wrong_password(smtp_server: FakeSmtpServer) -> None:
    """535 is reported as SMTP auth error without the password."""
    settings = SmtpSettings(host="mail.gmx.net", port=587, security=Security.STARTTLS, username=EMAIL, password="nope")
    with pytest.raises(GmxSmtpAuthError) as err:
        check_smtp_login(settings, ssl.create_default_context())
    assert "nope" not in str(err.value)
    assert smtp_server.sessions[0].closed


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ssl.SSLCertVerificationError("certificate verify failed"), GmxTlsError),
        (ConnectionRefusedError(), GmxConnectionError),
        (smtplib.SMTPServerDisconnected("gone"), GmxConnectionError),
    ],
)
def test_connection_errors(smtp_server: FakeSmtpServer, error: Exception, expected: type[Exception]) -> None:
    """Errors while connecting are mapped."""
    smtp_server.connect_error = error
    with pytest.raises(expected):
        check_smtp_login(STARTTLS, ssl.create_default_context())
