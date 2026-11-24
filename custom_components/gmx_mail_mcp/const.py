"""Constants for the GMX Mail integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "gmx_mail_mcp"

# --------------------------------------------------------------------------- entry data

CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"  # noqa: S105 - key name, not a secret
CONF_IMAP_HOST: Final = "imap_host"
CONF_IMAP_PORT: Final = "imap_port"
CONF_IMAP_SECURITY: Final = "imap_security"
CONF_SMTP_HOST: Final = "smtp_host"
CONF_SMTP_PORT: Final = "smtp_port"
CONF_SMTP_SECURITY: Final = "smtp_security"

SECTION_SERVER: Final = "server"
# HA user whose access tokens the MCP endpoint admits (D9); every other user gets 403.
CONF_ALLOWED_USER: Final = "allowed_user"
# Names of the MCP tools that are switched on (D12). Missing in the options = default: every tool of level READ.
CONF_ENABLED_TOOLS: Final = "enabled_tools"
# Seconds a single tool call may take before the client gets an error (the IMAP command itself is not cancelled).
TOOL_CALL_TIMEOUT_S: Final = 120.0


class Security(StrEnum):
    """Transport security of a connection. Plain text is deliberately not offered."""

    SSL = "ssl"  # implicit TLS
    STARTTLS = "starttls"


DEFAULT_IMAP_HOST: Final = "imap.gmx.net"
DEFAULT_IMAP_PORT: Final = 993
DEFAULT_IMAP_SECURITY: Final = Security.SSL
DEFAULT_SMTP_HOST: Final = "mail.gmx.net"
DEFAULT_SMTP_PORT: Final = 587
DEFAULT_SMTP_SECURITY: Final = Security.STARTTLS

# --------------------------------------------------------------------------- options


class Permission(StrEnum):
    """Permission levels R/O/D/S (SPEC section 5, measure 1)."""

    READ = "read"
    ORGANIZE = "organize"
    DELETE = "delete"
    SEND = "send"


class SendMode(StrEnum):
    """Send mode (SPEC section 5, measure 2)."""

    OFF = "off"
    ALLOWLIST = "allowlist"
    ANY = "any"


class SentCopyMode(StrEnum):
    """How a copy of a sent message reaches the Sent folder (GMX-NOTES row 22)."""

    APPEND = "append"
    AUTO = "auto"
    NONE = "none"


class FolderRole(StrEnum):
    """Special folder roles."""

    INBOX = "inbox"
    SENT = "sent"
    DRAFTS = "drafts"
    TRASH = "trash"
    JUNK = "junk"


CONF_PERMISSIONS: Final = "permissions"
CONF_SEND_MODE: Final = "send_mode"
CONF_ALLOWLIST: Final = "allowlist"
CONF_ALLOW_REPLY_TO_SENDER: Final = "allow_reply_to_sender"
CONF_ALLOW_PERMANENT_DELETE: Final = "allow_permanent_delete"
CONF_ALLOW_HTML_OUTPUT: Final = "allow_html_output"
CONF_MAX_RECIPIENTS: Final = "max_recipients"
CONF_MAX_SEND_PER_DAY: Final = "max_send_per_day"
CONF_MAX_DELETE_PER_DAY: Final = "max_delete_per_day"
CONF_MAX_RECIPIENTS_PER_HOUR: Final = "max_recipients_per_hour"
CONF_MAX_MESSAGE_SIZE_MB: Final = "max_message_size_mb"
CONF_SENDER_NAME: Final = "sender_name"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_IDLE_ENABLED: Final = "idle_enabled"
CONF_IDLE_RENEW_MINUTES: Final = "idle_renew_minutes"
CONF_FOLDER_SENT: Final = "folder_sent"
CONF_FOLDER_DRAFTS: Final = "folder_drafts"
CONF_FOLDER_TRASH: Final = "folder_trash"
CONF_FOLDER_JUNK: Final = "folder_junk"
CONF_EVENT_INCLUDE_HEADERS: Final = "event_include_headers"
CONF_SENT_COPY_MODE: Final = "sent_copy_mode"
CONF_AUDIT_RETENTION_DAYS: Final = "audit_retention_days"
CONF_ATTACHMENT_RETENTION_DAYS: Final = "attachment_retention_days"

SECTION_PERMISSIONS: Final = "permissions_section"
SECTION_SENDING: Final = "sending_section"
SECTION_LIMITS: Final = "limits_section"
SECTION_CONNECTION: Final = "connection_section"
SECTION_FOLDERS: Final = "folders_section"
SECTION_PRIVACY: Final = "privacy_section"

FOLDER_OVERRIDE_KEYS: Final[dict[FolderRole, str]] = {
    FolderRole.SENT: CONF_FOLDER_SENT,
    FolderRole.DRAFTS: CONF_FOLDER_DRAFTS,
    FolderRole.TRASH: CONF_FOLDER_TRASH,
    FolderRole.JUNK: CONF_FOLDER_JUNK,
}

# Limits (SPEC section 5, measure 5): defaults only, freely adjustable in the options flow (minimum 1).
# GMX does not publish its own sending limits (GMX-NOTES row 20); high values risk an account block.
DEFAULT_MAX_RECIPIENTS: Final = 10
DEFAULT_MAX_SEND_PER_DAY: Final = 20
DEFAULT_MAX_DELETE_PER_DAY: Final = 200
# Third-party claim "max. 11 recipients per hour from a mail client" (GMX-NOTES row 20, unconfirmed) -> stay below it.
DEFAULT_MAX_RECIPIENTS_PER_HOUR: Final = 10
# Attachment limit of GMX FreeMail (GMX-NOTES row 19); ProMail 50 MB, TopMail 100 MB. SMTP SIZE allows ~135 MiB.
DEFAULT_MAX_MESSAGE_SIZE_MB: Final = 20
MAX_MESSAGE_SIZE_MB: Final = 100
MAX_SENDER_NAME: Final = 100

POLL_INTERVAL_MIN_S: Final = 60
POLL_INTERVAL_MAX_S: Final = 3600
DEFAULT_POLL_INTERVAL_S: Final = 300
# GMX drops idle connections after ~30 min (M0) -> IDLE must be renewed after at most 25 min.
IDLE_RENEW_MIN_MINUTES: Final = 5
IDLE_RENEW_MAX_MINUTES: Final = 25
DEFAULT_IDLE_RENEW_MINUTES: Final = 24
AUDIT_RETENTION_MIN_DAYS: Final = 1
AUDIT_RETENTION_MAX_DAYS: Final = 365
DEFAULT_AUDIT_RETENTION_DAYS: Final = 90
ATTACHMENT_RETENTION_MAX_DAYS: Final = 365
DEFAULT_ATTACHMENT_RETENTION_DAYS: Final = 7  # 0 = never delete saved attachments

ALLOWLIST_WILDCARD: Final = "*"

DEFAULT_OPTIONS: Final[dict[str, object]] = {
    CONF_PERMISSIONS: [Permission.READ.value],
    CONF_SEND_MODE: SendMode.OFF.value,
    CONF_ALLOWLIST: [],
    CONF_ALLOW_REPLY_TO_SENDER: False,
    CONF_ALLOW_PERMANENT_DELETE: False,
    CONF_ALLOW_HTML_OUTPUT: False,
    CONF_MAX_RECIPIENTS: DEFAULT_MAX_RECIPIENTS,
    CONF_MAX_SEND_PER_DAY: DEFAULT_MAX_SEND_PER_DAY,
    CONF_MAX_DELETE_PER_DAY: DEFAULT_MAX_DELETE_PER_DAY,
    CONF_MAX_RECIPIENTS_PER_HOUR: DEFAULT_MAX_RECIPIENTS_PER_HOUR,
    CONF_MAX_MESSAGE_SIZE_MB: DEFAULT_MAX_MESSAGE_SIZE_MB,
    CONF_SENDER_NAME: "",
    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_S,
    CONF_IDLE_ENABLED: True,
    CONF_IDLE_RENEW_MINUTES: DEFAULT_IDLE_RENEW_MINUTES,
    CONF_FOLDER_SENT: "",
    CONF_FOLDER_DRAFTS: "",
    CONF_FOLDER_TRASH: "",
    CONF_FOLDER_JUNK: "",
    CONF_EVENT_INCLUDE_HEADERS: False,
    CONF_SENT_COPY_MODE: SentCopyMode.APPEND.value,
    CONF_AUDIT_RETENTION_DAYS: DEFAULT_AUDIT_RETENTION_DAYS,
    CONF_ATTACHMENT_RETENTION_DAYS: DEFAULT_ATTACHMENT_RETENTION_DAYS,
}

# --------------------------------------------------------------------------- connection handling

MAX_IMAP_CONNECTIONS: Final = 2  # one IDLE + one work connection per account
CONNECT_TIMEOUT_S: Final = 20.0
COMMAND_TIMEOUT_S: Final = 30.0
SMTP_TIMEOUT_S: Final = 30.0
NOOP_AFTER_IDLE_S: Final = 5 * 60
CLOSE_AFTER_IDLE_S: Final = 10 * 60
IDLE_CHECK_INTERVAL_S: Final = 60
BACKOFF_MIN_S: Final = 5.0
BACKOFF_MAX_S: Final = 15 * 60.0
BACKOFF_JITTER: Final = 0.2  # +/- 20 %

# --------------------------------------------------------------------------- push, polling, events (M3)

IDLE_ACK_TIMEOUT_S: Final = 10.0
IDLE_PUSH_GRACE_S: Final = 30.0  # extra wait beyond the renewal timer before assuming the connection hangs
MAX_NEW_MESSAGE_EVENTS: Final = 20  # per update; more are counted but not announced (flood protection)
REFRESH_COOLDOWN_S: Final = 5.0

EVENT_NEW_MESSAGE: Final = "gmx_mail_new_message"
EVENT_ACTION_EXECUTED: Final = "gmx_mail_action_executed"

# --------------------------------------------------------------------------- organising (M4)

MAX_REFS_PER_CALL: Final = 100
MAX_DELETE_PER_CALL: Final = 50  # trash / delete_permanently per call (SPEC section 5, measure 5)
ATTACHMENT_CLEANUP_TIME: Final = (3, 17, 0)  # local time, once a day


class PushState(StrEnum):
    """State of the IDLE watcher."""

    DISABLED = "disabled"  # switched off in the options
    UNSUPPORTED = "unsupported"  # no IDLE capability or STARTTLS (aioimaplib cannot do STARTTLS)
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


# --------------------------------------------------------------------------- repairs

ISSUE_IMAP_POSSIBLY_DISABLED: Final = "imap_possibly_disabled"
ISSUE_UIDVALIDITY_CHANGED: Final = "uidvalidity_changed"
ISSUE_FOLDER_MAPPING_AMBIGUOUS: Final = "folder_mapping_ambiguous"
ISSUE_SERVER_RULES_UNAVAILABLE: Final = "server_rules_unavailable"
ISSUE_SMTP_AUTH_FAILED: Final = "smtp_auth_failed"
