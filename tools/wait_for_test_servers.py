#!/usr/bin/env python3
"""Wait until the test mail servers from docker-compose.yml answer (CI helper).

Usage: python3 tools/wait_for_test_servers.py greenmail dovecot
"""

from __future__ import annotations

import imaplib
import ssl
import sys
import time
from pathlib import Path

CA_FILE = Path(__file__).resolve().parent.parent / "tests" / "integration" / "certs" / "ca.crt"

SERVERS = {
    "greenmail": lambda: imaplib.IMAP4("127.0.0.1", 3143),
    # Verified against the test CA (tools/make_test_certs.py) - certificate checks are never switched off.
    "dovecot": lambda: imaplib.IMAP4_SSL("127.0.0.1", 31993, ssl_context=ssl.create_default_context(cafile=str(CA_FILE))),
}


def main(names: list[str]) -> None:
    """Poll each server until it greets, at most two minutes."""
    for name in names:
        for _ in range(60):
            try:
                SERVERS[name]().logout()
                print(f"{name}: ready")
                break
            except OSError, imaplib.IMAP4.error:  # port open before the server answers -> abort
                time.sleep(2)
        else:
            raise SystemExit(f"{name} did not start")


if __name__ == "__main__":
    main(sys.argv[1:] or list(SERVERS))
