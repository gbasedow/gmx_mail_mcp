"""Errors raised by the GMX mail clients.

Messages never contain credentials, addresses or mail content.
"""

from __future__ import annotations


class GmxMailError(Exception):
    """Base class for all client errors."""


class GmxAuthError(GmxMailError):
    """Login rejected.

    At GMX this means *wrong password or IMAP/POP3 access disabled* - the two cannot be told apart
    (GMX-NOTES row 6).
    """


class GmxSmtpAuthError(GmxMailError):
    """SMTP login rejected."""


class GmxTlsError(GmxMailError):
    """TLS handshake or certificate verification failed."""


class GmxConnectionError(GmxMailError):
    """Server not reachable, connection lost or timed out."""


class GmxBackoffError(GmxConnectionError):
    """A reconnect is not allowed yet because of the backoff after a failed attempt."""

    def __init__(self, retry_in: float) -> None:
        """Store the remaining backoff time."""
        super().__init__(f"reconnect backoff active, retry in {retry_in:.0f} s")
        self.retry_in = retry_in


class GmxConnectionLimitError(GmxMailError):
    """The per-account IMAP connection budget is exhausted."""


class GmxOutcomeUnknownError(GmxMailError):
    """The connection broke during a writing command; it is unknown whether the server executed it.

    Writing commands are never repeated automatically (SPEC section 5, measure 6).
    """


class GmxFolderNotFoundError(GmxMailError):
    """The folder does not exist (or cannot be selected)."""


class GmxUidValidityError(GmxMailError):
    """UIDVALIDITY of the folder changed; mail references are no longer valid."""


class GmxMessageNotFoundError(GmxMailError):
    """No message with this UID in the folder."""


class GmxPartNotFoundError(GmxMailError):
    """The message has no part with this id."""


class GmxAttachmentTooLargeError(GmxMailError):
    """The attachment exceeds the allowed size."""


class GmxSearchRejectedError(GmxMailError):
    """The server rejected the search criteria (e.g. the UTF-8 charset)."""


class GmxUnsupportedError(GmxMailError):
    """The server lacks what the operation needs (e.g. MOVE and UIDPLUS, keywords)."""

    def __init__(self, reason: str) -> None:
        """Store a translation-key-like reason."""
        super().__init__(reason)
        self.reason = reason


class GmxFolderExistsError(GmxMailError):
    """A folder with this name already exists."""


class GmxInvalidFolderNameError(GmxMailError):
    """The folder name is not allowed (empty, too long, '/', '%', '*', control characters) or source equals target."""

    def __init__(self, reason: str) -> None:
        """Store a translation-key-like reason."""
        super().__init__(reason)
        self.reason = reason


class GmxProtectedFolderError(GmxMailError):
    """Special folders and INBOX cannot be renamed, and get no subfolders."""


class GmxProtectedFolderDeleteError(GmxProtectedFolderError):
    """INBOX and special folders cannot be deleted."""


class GmxMessageTooLargeError(GmxMailError):
    """The message exceeds the server's APPENDLIMIT."""


class GmxDeleteRuleError(GmxMailError):
    """A deleting action is not allowed for these messages or this folder (``reason`` is the translation key)."""

    def __init__(self, reason: str) -> None:
        """Store a translation-key-like reason."""
        super().__init__(reason)
        self.reason = reason


class GmxSmtpRejectedError(GmxMailError):
    """The SMTP server refused the sender, a recipient or the message; nothing was sent (``reason`` = translation key)."""

    def __init__(self, reason: str) -> None:
        """Store a translation-key-like reason."""
        super().__init__(reason)
        self.reason = reason


class GmxSendInputError(GmxMailError):
    """The message to send is not usable as given (e.g. an invalid address in a draft); ``reason`` = translation key."""

    def __init__(self, reason: str) -> None:
        """Store a translation-key-like reason."""
        super().__init__(reason)
        self.reason = reason
