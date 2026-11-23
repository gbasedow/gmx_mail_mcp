#!/usr/bin/env python3
"""gmx_probe.py - Live-Test gegen ein GMX-Konto (nur Metadaten, keine Mailinhalte).

Aufruf (App-Passwort verwenden, bevorzugt ein Testkonto):
    export GMX_USER='testkonto@gmx.de'
    export GMX_PASS='<App-Passwort>'
    python3 tools/gmx_probe.py > gmx_probe_result.json
    python3 tools/gmx_probe.py --send-self-test > gmx_probe_result_send.json

Sicherheitsregeln dieses Skripts:
  * Es gibt nie Passwort, Adressen, Betreffs oder Mailinhalte aus.
  * Im Standardlauf werden nur zwei eigene Testordner angelegt/benutzt und wieder geloescht.
    Existieren sie schon, bricht das Skript ab.
  * Im Standardlauf wird keine Mail versendet (SMTP: nur EHLO, STARTTLS, Login).
  * --send-self-test sendet genau eine Mail an die eigene Adresse und loescht danach die beiden
    selbst erzeugten Testmails (Posteingang und Gesendet) anhand ihrer eindeutigen Message-ID.
  * Ordnernamen werden nur mit --print-folders ausgegeben (Spezialordner immer).

Nur Standardbibliothek, Python >= 3.10.

Zertifikatsfehler (SSLCertVerificationError / "unable to get local issuer certificate"):
  * Das Skript prueft das Server-Zertifikat immer. Es probiert nacheinander den System-Vertrauensspeicher,
    certifi (falls installiert) und unter macOS die System-Schluesselbunde. Die gewaehlte Quelle steht in
    connect.trust_source. Die Pruefung wird nie abgeschaltet, weil das App-Passwort uebertragen wird.
  * --tls-diagnose zeigt, welche Zertifikatskette der Server liefert und welche Vertrauensquelle funktioniert.
    Dabei werden KEINE Zugangsdaten gesendet.
  * --cafile PFAD  waehlt eine eigene CA-Datei (PEM oder einzelnes DER-.cer), z. B. die Root-CA des Ausstellers
    oder eines Firmen-/Antiviren-Proxys. Alternativ die Umgebungsvariable SSL_CERT_FILE setzen.
  * Windows: Python sieht nur Root-Zertifikate, die Windows schon lokal installiert hat. Abhilfe: py -m pip install certifi
"""

from __future__ import annotations

import argparse
import atexit
import base64
import email.utils
import hashlib
import imaplib
import json
import os
import re
import select
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from typing import Any

IMAP_HOST = "imap.gmx.net"
IMAP_PORT = 993
SMTP_HOST = "mail.gmx.net"
SMTP_PORT = 587
TIMEOUT = 30
MAX_PARALLEL = 4
PROBE_VERSION = "v3d"
# Neuere imaplib-Versionen setzen Mailbox-Namen selbst in Anfuehrungszeichen (_astring), aeltere nicht.
AUTO_QUOTE = hasattr(imaplib.IMAP4, "_astring")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------------- helpers


def utf7_encode(name: str) -> str:
    """Modified UTF-7 (RFC 3501 section 5.1.3) for IMAP mailbox names."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            enc = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            out.append("&" + enc + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def utf7_decode(name: str) -> str:
    """Decode modified UTF-7 mailbox names."""

    def repl(match: re.Match[str]) -> str:
        chunk = match.group(1)
        if chunk == "":
            return "&"
        b64 = chunk.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", repl, name)


def quote(mailbox: str) -> str:
    """Quote an already UTF-7 encoded mailbox name (for raw commands)."""
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mb(mailbox: str) -> str:
    """Mailbox argument for imaplib high-level methods (version independent)."""
    return mailbox if AUTO_QUOTE else quote(mailbox)


def imap_date(ts: float) -> str:
    """IMAP date (dd-Mon-yyyy) independent of the system locale."""
    t = time.gmtime(ts)
    return f"{t.tm_mday:02d}-{MONTHS[t.tm_mon - 1]}-{t.tm_year}"


def decode_list(lines: list[Any]) -> list[dict[str, Any]]:
    """Parse LIST responses into dicts: flags, delimiter, name (decoded)."""
    out = []
    pat = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')
    for line in lines:
        if line is None:
            continue
        if isinstance(line, tuple):  # literal form: (b'(\\Flags) "/" {12}', b'mailbox name')
            line = re.sub(rb"\s*\{\d+\+?\}\s*$", b"", line[0]) + b" " + quote(line[1].decode("utf-8", "replace")).encode()
        text = line.decode("utf-8", errors="replace")
        m = pat.match(text.strip())
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        delim = m.group("delim")
        out.append(
            {
                "flags": m.group("flags").split(),
                "delimiter": None if delim == "NIL" else delim.strip('"'),
                "raw_name": name,
                "name": utf7_decode(name),
            }
        )
    return out


def err(e: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    if isinstance(e, ssl.SSLCertVerificationError):
        out["verify_code"] = e.verify_code
        out["verify_message"] = e.verify_message
        out["hint"] = TLS_HINT
    return out


TLS_HINT = (
    "Zertifikatspruefung fehlgeschlagen: Dieser Rechner vertraut der Zertifikatskette nicht. Typische Ursachen: "
    "(1) macOS mit python.org-Python ohne Zertifikate -> 'Install Certificates.command' im Python-Ordner ausfuehren "
    "oder 'pip install certifi'; (2) Firmen- oder Antiviren-Proxy mit eigener CA (TLS-Inspektion) -> Root-CA als PEM "
    "exportieren und mit --cafile angeben; (3) Linux ohne ca-certificates -> Paket installieren; (4) falsche Systemzeit; "
    "(5) 'Hostname mismatch' -> falscher Host oder Proxy; (6) Windows: Python sieht nur Root-Zertifikate, die Windows schon lokal "
    "installiert hat -> 'py -m pip install certifi' (das Skript nutzt certifi dann automatisch). Ursache eingrenzen mit: python3 tools/gmx_probe.py --tls-diagnose "
    "(sendet keine Zugangsdaten). Die Pruefung nie abschalten, solange das App-Passwort uebertragen wird."
)


def pem_from_cafile(path: str) -> str:
    """Return a PEM file path for `path`; converts a single DER certificate (.cer/.crt) to a temp PEM if needed."""
    with open(path, "rb") as fh:
        data = fh.read()
    if b"BEGIN CERTIFICATE" in data:
        return path
    return temp_pem(ssl.DER_cert_to_PEM_cert(data))


def temp_pem(pem: str) -> str:
    """Write PEM text to a temp file that is removed at exit."""
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(pem)

    def remove() -> None:
        if os.path.exists(f.name):
            os.unlink(f.name)

    atexit.register(remove)
    return f.name


def make_context(cafile: str | None = None) -> ssl.SSLContext:
    if cafile:
        ctx = ssl.create_default_context(cafile=pem_from_cafile(cafile))
    else:
        ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def certifi_path() -> str | None:
    try:
        import certifi  # type: ignore[import-not-found,unused-ignore]  # optional dependency

        return str(certifi.where())
    except Exception:  # noqa: BLE001
        return None


def macos_keychain_pem() -> str | None:
    """Export macOS root/system/login keychain certificates (OS trust, incl. admin-installed CAs) to a temp PEM."""
    if sys.platform != "darwin":
        return None
    keychains = ["/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain"]
    login = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    if os.path.exists(login):
        keychains.append(login)
    try:
        cp = subprocess.run(
            ["security", "find-certificate", "-a", "-p", *keychains], capture_output=True, text=True, timeout=30, check=False
        )
    except Exception:  # noqa: BLE001
        return None
    if "BEGIN CERTIFICATE" not in cp.stdout:
        return None
    return temp_pem(cp.stdout)


def trust_candidates(cafile: str | None) -> Iterator[tuple[str, str | None]]:
    """Yield (label, cafile) in order of preference; lazily, so the macOS export only happens when needed."""
    if cafile:
        yield "cafile", cafile
        return
    yield "system", None
    cp = certifi_path()
    if cp:
        yield "certifi", cp
    mp = macos_keychain_pem()
    if mp:
        yield "macos_keychain", mp


class TLSTrustError(Exception):
    def __init__(self, tried: list[dict[str, Any]]) -> None:
        super().__init__("certificate verification failed for all trust sources")
        self.tried = tried


def cert_info(der: bytes) -> dict[str, Any]:
    info: dict[str, Any] = {"sha256": hashlib.sha256(der).hexdigest()}
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
            path = f.name
        try:
            d = ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]  # noqa: SLF001 (CPython-internal, optional)
        finally:
            os.unlink(path)

        def flat(t: Any) -> dict[str, str]:
            return {k: v for rdn in t for k, v in rdn}

        subj, iss = flat(d.get("subject", ())), flat(d.get("issuer", ()))
        info.update(
            {
                "subject_cn": subj.get("commonName"),
                "issuer_cn": iss.get("commonName"),
                "issuer_o": iss.get("organizationName"),
                "not_before": d.get("notBefore"),
                "not_after": d.get("notAfter"),
                "self_signed": subj == iss,
            }
        )
    except Exception as e:  # noqa: BLE001
        info["decode_error"] = type(e).__name__
    return info


def tls_endpoint(host: str, port: int, smtp_starttls: bool, cafile_arg: str | None) -> dict[str, Any]:
    """Look at the certificate chain of an endpoint WITHOUT sending credentials."""
    out: dict[str, Any] = {"host": host, "port": port}
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    insecure.minimum_version = ssl.TLSVersion.TLSv1_2

    def open_sock(ctx: ssl.SSLContext) -> tuple[Any, Any]:
        if smtp_starttls:
            S = smtplib.SMTP(host, port, timeout=10)
            S.ehlo()
            S.starttls(context=ctx)
            return S.sock, S
        raw = socket.create_connection((host, port), timeout=10)
        return ctx.wrap_socket(raw, server_hostname=host), raw

    try:
        sock, _ = open_sock(insecure)
        try:
            out["tls_version"] = sock.version()
            der = sock.getpeercert(binary_form=True)
            chain: list[bytes] = []
            getter = getattr(sock, "get_unverified_chain", None)
            if getter:
                try:
                    for c in getter() or []:
                        chain.append(c if isinstance(c, bytes) else c.public_bytes(ssl._ssl.ENCODING_DER))  # type: ignore[attr-defined]  # noqa: SLF001
                except Exception:  # noqa: BLE001
                    chain = []
            if not chain and der:
                chain = [der]
            out["chain_length_sent_by_server"] = len(chain)
            out["chain"] = [cert_info(c) for c in chain]
        finally:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        out["connect_error"] = err(e)
        return out

    attempts = []
    for label, cafile in trust_candidates(cafile_arg):
        ctx = make_context(cafile)
        try:
            sock, _ = open_sock(ctx)
            sock.close()
            attempts.append({"trust": label, "ok": True})
            break
        except ssl.SSLCertVerificationError as e:
            attempts.append({"trust": label, "ok": False, "verify_code": e.verify_code, "verify_message": e.verify_message})
        except Exception as e:  # noqa: BLE001
            attempts.append({"trust": label, "ok": False, "error": type(e).__name__})
    out["verification_attempts"] = attempts
    return out


def build_notes(res: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for key in ("imap", "smtp"):
        ep = res.get(key, {})
        if "connect_error" in ep:
            notes.append(f"{key}: Verbindung nicht moeglich ({ep['connect_error'].get('error')}).")
            continue
        att = ep.get("verification_attempts", [])
        okay = [a["trust"] for a in att if a.get("ok")]
        if okay:
            notes.append(f"{key}: Pruefung erfolgreich mit Vertrauensquelle '{okay[0]}' - gmx_probe.py waehlt sie automatisch.")
            continue
        codes = sorted({a.get("verify_message") for a in att if a.get("verify_message")})
        notes.append(f"{key}: Pruefung scheiterte mit allen Quellen ({', '.join(codes) or 'siehe verification_attempts'}).")
        chain = ep.get("chain", [])
        if not chain:
            continue
        names = " -> ".join(str(c.get("subject_cn")) for c in chain)
        top = chain[-1]
        if top.get("self_signed"):
            notes.append(
                f"{key}: Kette (vom Server gesendet): {names}. Das oberste Zertifikat '{top.get('subject_cn')}' ist ein Root, "
                "dem dieser Rechner nicht vertraut. Ist das keine oeffentliche CA, liegt vermutlich TLS-Inspektion vor "
                "-> Root-CA exportieren und mit --cafile angeben."
            )
        else:
            notes.append(
                f"{key}: Kette (vom Server gesendet): {names}. Die Kette ist bis zu einem Cross-Zertifikat vollstaendig; es fehlt der "
                f"Vertrauensanker '{top.get('issuer_cn')}' (ersatzweise der Root '{top.get('subject_cn')}') im Speicher, den Python sieht. "
                "Abhilfe: 'pip install certifi' oder die Root-Datei der CA per --cafile angeben (Fingerabdruck beim Aussteller pruefen)."
            )
    return notes


def tls_diagnose(args: argparse.Namespace) -> dict[str, Any]:
    paths = ssl.get_default_verify_paths()
    res: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "openssl": ssl.OPENSSL_VERSION,
        "default_verify_paths": {"cafile": paths.cafile, "capath": paths.capath},
        "env_set": {
            k: bool(os.environ.get(k)) for k in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "https_proxy")
        },
        "certifi_available": certifi_path() is not None,
        "utc_now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    res["imap"] = tls_endpoint(args.imap_host, args.imap_port, False, args.cafile)
    res["smtp"] = tls_endpoint(SMTP_HOST, SMTP_PORT, True, args.cafile)
    res["notes"] = build_notes(res)
    return res


def parse_uid(data: Any, kind: str) -> list[int]:
    """Extract UIDs from [APPENDUID v u] / [COPYUID v src dst] responses."""
    text = b" ".join(d if isinstance(d, bytes) else b"" for d in (data or [])).decode("utf-8", errors="replace")
    m = re.search(rf"{kind}\s+\d+\s+(?:[\d:,*]+\s+)?([\d:,]+)", text)
    if not m:
        return []
    uids: list[int] = []
    for part in m.group(1).split(","):
        if ":" in part:
            lo, hi = sorted(int(x) for x in part.split(":"))  # RFC 3501: a range may be given in either order
            uids.extend(range(lo, hi + 1))
        elif part.isdigit():
            uids.append(int(part))
    return uids


def make_message(msg_id: str, user: str, subject: str, text: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = user
    msg["Subject"] = subject
    msg["Message-ID"] = msg_id
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(text)
    return msg.as_bytes()


class Probe:
    def __init__(self, user: str, password: str, args: argparse.Namespace) -> None:
        self.imap_host = args.imap_host
        self.imap_port = args.imap_port
        self.user = user
        self.password = password
        self.args = args
        self.result: dict[str, Any] = {}
        self.ctx = make_context(args.cafile)
        self.trust_source = "cafile" if args.cafile else "system"
        self.token = f"haprobe-{int(time.time())}"
        self.M: imaplib.IMAP4_SSL | None = None
        self.delim = "/"
        self.caps: set[str] = set()
        self.folder_names: set[str] | None = None
        self.special: dict[str, str] = {}
        self.created: list[str] = []  # UTF-7 names of test folders we created (for cleanup)
        self.a = ""
        self.b = ""

    def step(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
        except Exception as e:  # noqa: BLE001 - we want every failure recorded, not raised
            value = err(e)
        if value is not None:
            self.result[name] = value
        return value

    # ---- IMAP -------------------------------------------------------------------

    def connect(self) -> bool:
        t0 = time.monotonic()
        try:
            M = self.open_imap()
        except TLSTrustError as e:
            self.result["connect"] = {
                "error": "SSLCertVerificationError: Zertifikat des Servers wird mit keiner Vertrauensquelle akzeptiert",
                "trust_sources_tried": e.tried,
                "hint": TLS_HINT,
            }
            return False
        except Exception as e:  # noqa: BLE001
            self.result["connect"] = err(e)
            return False
        self.M = M
        info: dict[str, Any] = {}
        if not self.args.no_tls:
            info["trust_source"] = self.trust_source
            sock = M.sock
            cert = sock.getpeercert() or {}
            cn = None
            for rdn in cert.get("subject", ()):
                for k, v in rdn:
                    if k == "commonName":
                        cn = v
            san = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
            info |= {
                "tls_version": sock.version(),
                "cipher": sock.cipher()[0] if sock.cipher() else None,
                "cert_cn": cn,
                "cert_san_matches_host": any(s == self.imap_host or (s.startswith("*.") and self.imap_host.endswith(s[1:])) for s in san),
            }
        info["greeting"] = M.welcome.decode("utf-8", errors="replace")[:80]
        info["capability_pre_login"] = sorted(M.capabilities)
        self.result["connect"] = info
        try:
            M.login(self.user, self.password)
        except imaplib.IMAP4.error as e:
            self.result["login"] = {
                "ok": False,
                "error": str(e)[:160],
                "hint": "App-Passwort korrekt? POP3/IMAP im Webmailer erlaubt (wird bei Nichtnutzung abgeschaltet)?",
            }
            return False
        self.result["login"] = {"ok": True, "seconds": round(time.monotonic() - t0, 2)}
        _typ, data = M.capability()
        cap_line = data[0].decode() if data and data[0] else ""
        self.caps = set(cap_line.upper().split())
        self.result["capability_post_login"] = sorted(self.caps)
        return True

    def open_imap(self) -> Any:
        """Open the IMAP connection. Tries trust sources in order; never disables verification."""
        if self.args.no_tls:
            return imaplib.IMAP4(self.imap_host, self.imap_port, timeout=TIMEOUT)
        tried: list[dict[str, Any]] = []
        for label, cafile in trust_candidates(self.args.cafile):
            ctx = make_context(cafile)
            try:
                M = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=ctx, timeout=TIMEOUT)
            except ssl.SSLCertVerificationError as e:
                tried.append({"trust": label, "verify_code": e.verify_code, "verify_message": e.verify_message})
                continue
            self.ctx = ctx
            self.trust_source = label
            return M
        raise TLSTrustError(tried)

    def list_folders(self) -> None:
        M = self.M
        assert M is not None
        _typ, lines = M.list()
        folders = decode_list(lines)
        if folders:
            self.delim = folders[0]["delimiter"] or "/"
        special: dict[str, str] = {}
        for f in folders:
            for fl in f["flags"]:
                if fl.lower() in {"\\sent", "\\drafts", "\\trash", "\\junk", "\\archive", "\\all", "\\flagged"}:
                    special[f["name"]] = fl
        ext: Any = None
        if "SPECIAL-USE" in self.caps and "LIST-EXTENDED" in self.caps:
            try:
                typ2, lines2 = M._simple_command("LIST", "(SPECIAL-USE)", '""', '"*"')  # noqa: SLF001
                typ2, lines2 = M._untagged_response(typ2, lines2, "LIST")  # noqa: SLF001
                ext = {f["name"]: [x for x in f["flags"] if x.startswith("\\")] for f in decode_list(lines2)}
            except Exception as e:  # noqa: BLE001
                ext = err(e)
        self.result["folders"] = {
            "count": len(folders),
            "delimiter": self.delim,
            "special_use_from_plain_list": special,
            "special_use_from_list_extended": ext,
        }
        if self.args.print_folders:
            self.result["folders"]["all_names"] = [f["name"] for f in folders]
        self.folder_names = {f["name"] for f in folders}
        self.special = special

    def status_inbox(self) -> Any:
        assert self.M
        _typ, data = self.M.status("INBOX", "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)")
        return data[0].decode() if data and data[0] else None

    def uid_search(self, *crit: str, charset: str | None = None, literal: bytes | None = None) -> dict[str, Any]:
        assert self.M
        args = list(crit)
        if charset:
            args = ["CHARSET", charset, *args]
        if literal is not None:
            self.M.literal = literal  # type: ignore[assignment]  # imaplib sends it as final literal; stubs say str
        try:
            typ, data = self.M.uid("SEARCH", *args)
        except Exception as e:  # noqa: BLE001
            self.M.literal = None
            return err(e)
        found = data[0].split() if data and data[0] else []
        return {"status": typ, "hits": len(found)}

    def count_all(self) -> int:
        assert self.M
        _typ, data = self.M.uid("SEARCH", "ALL")
        return len(data[0].split()) if data and data[0] else 0

    def create_test_folders(self) -> bool:
        assert self.M
        suffix = "Ä"  # Umlaut absichtlich: testet modifiziertes UTF-7
        a_plain = f"{self.args.folder_prefix}-{suffix}A"
        b_plain = f"{self.args.folder_prefix}-{suffix}B"
        if self.folder_names is None:
            self.result["test_folders"] = {"error": "Ordnerliste nicht verfuegbar - Abbruch (nichts veraendert)"}
            return False
        if a_plain in self.folder_names or b_plain in self.folder_names:
            self.result["test_folders"] = {"error": "Testordner existieren bereits - Abbruch (nichts veraendert)"}
            return False
        a, b = utf7_encode(a_plain), utf7_encode(b_plain)
        self.a, self.b = a, b
        res: dict[str, Any] = {}
        for label, name in (("A", a), ("B", b)):
            typ, _data = self.M.create(mb(name))
            res[f"create_{label}"] = typ
            if typ != "OK":
                self.result["test_folders"] = res
                return False
            self.created.append(name)
        self.result["test_folders"] = res
        return True

    def run_folder_tests(self) -> None:
        M = self.M
        assert M is not None
        a, b = self.a, self.b
        tests: dict[str, Any] = {}
        self.result["folder_tests"] = tests
        id1 = f"<{self.token}-1@ha-probe.invalid>"
        id2 = f"<{self.token}-2@ha-probe.invalid>"
        m1 = make_message(id1, self.user, "HA Probe Grüße Äpfel", "Testkörper mit Umlauten: Größe Übung ß")
        m2 = make_message(id2, self.user, "HA Probe plain", "plain ascii body probe")
        typ, data = M.append(mb(a), r"(\Seen)", None, m1)
        tests["append_1"] = {"status": typ, "appenduid": parse_uid(data, "APPENDUID")}
        typ, data = M.append(mb(a), None, None, m2)
        tests["append_2"] = {"status": typ, "appenduid": parse_uid(data, "APPENDUID")}

        typ, _ = M.select(mb(a))
        tests["select_a"] = typ
        pf = M.response("PERMANENTFLAGS")[1]
        tests["permanentflags"] = [x.decode("utf-8", errors="replace") for x in pf if x]
        tests["uidvalidity"] = [x.decode() for x in (M.response("UIDVALIDITY")[1] or []) if x]
        tests["count_a"] = self.count_all()

        s = {}
        s["ascii_subject_Probe"] = self.uid_search("SUBJECT", "Probe")
        s["ascii_subject_lowercase_probe"] = self.uid_search("SUBJECT", "probe")
        s["utf8_subject_Gruesse"] = self.uid_search("SUBJECT", charset="UTF-8", literal="Grüße".encode())
        s["utf8_subject_gruesse_lower"] = self.uid_search("SUBJECT", charset="UTF-8", literal="grüße".encode())
        s["utf8_body_Uebung"] = self.uid_search("BODY", charset="UTF-8", literal="Übung".encode())
        s["utf8_text_Groesse"] = self.uid_search("TEXT", charset="UTF-8", literal="Größe".encode())
        s["no_charset_body_ascii"] = self.uid_search("BODY", "probe")
        s["no_charset_text_ascii"] = self.uid_search("TEXT", "ascii")
        s["header_message_id"] = self.uid_search("HEADER", "Message-ID", self.token)
        s["larger_10"] = self.uid_search("LARGER", "10")
        s["smaller_1000000"] = self.uid_search("SMALLER", "1000000")
        s["since_yesterday"] = self.uid_search("SINCE", imap_date(time.time() - 86400))
        s["seen"] = self.uid_search("SEEN")
        s["unseen"] = self.uid_search("UNSEEN")
        tests["search"] = s
        tests["search_index_delay"] = self.search_index_delay()

        if "SORT" in self.caps:
            try:
                typ, data = M.uid("SORT", "(REVERSE DATE)", "UTF-8", "ALL")
                tests["sort"] = {"status": typ, "hits": len(data[0].split()) if data and data[0] else 0}
            except Exception as e:  # noqa: BLE001
                tests["sort"] = err(e)
        else:
            tests["sort"] = "capability missing"

        # Keywords
        uids = M.uid("SEARCH", "ALL")[1][0].split()
        first = uids[0].decode() if uids else None
        if first:
            try:
                typ, data = M.uid("STORE", first, "+FLAGS", "(haprobe)")
                kw = self.uid_search("KEYWORD", "haprobe")
                tests["custom_keyword"] = {"store": typ, "keyword_search": kw}
            except Exception as e:  # noqa: BLE001
                tests["custom_keyword"] = err(e)
            try:
                typ, data = M.uid("STORE", first, "+FLAGS", r"(\Flagged \Answered)")
                tests["system_flags_store"] = typ
            except Exception as e:  # noqa: BLE001
                tests["system_flags_store"] = err(e)

        # COPY A -> B
        try:
            typ, data = self.uid_copy_move("COPY", first or "1", b)
            tests["copy_to_b"] = {"status": typ, "copyuid_seen": self.copyuid_seen(data)}
        except Exception as e:  # noqa: BLE001
            tests["copy_to_b"] = err(e)
        # MOVE A -> B (second message)
        if "MOVE" in self.caps and len(uids) > 1:
            try:
                typ, data = self.uid_copy_move("MOVE", uids[1].decode(), b)
                tests["move_to_b"] = {"status": typ, "copyuid_seen": self.copyuid_seen(data)}
            except Exception as e:  # noqa: BLE001
                tests["move_to_b"] = err(e)
        else:
            tests["move_to_b"] = "skipped (no MOVE capability or message missing)"
        tests["count_a_after"] = self.count_all()
        typ, data = M.status(mb(b), "(MESSAGES)")
        tests["status_b"] = data[0].decode("utf-8", errors="replace") if data and data[0] else None

        # Delete semantics inside test folder B: \Deleted + UID EXPUNGE
        M.select(mb(b))
        b_uids = M.uid("SEARCH", "ALL")[1][0].split()
        if b_uids:
            M.uid("STORE", b_uids[0].decode(), "+FLAGS", r"(\Deleted)")
            tests["deleted_flag_visible"] = self.uid_search("DELETED")
            if "UIDPLUS" in self.caps:
                try:
                    typ, data = M.uid("EXPUNGE", b_uids[0].decode())
                    tests["uid_expunge"] = {"status": typ, "count_b_after": self.count_all()}
                except Exception as e:  # noqa: BLE001
                    tests["uid_expunge"] = err(e)
            else:
                tests["uid_expunge"] = "skipped (no UIDPLUS)"
        tests["trash_move"] = self.trash_move_test()
        try:
            # The selected mailbox may be the real Trash if trash_move_test failed midway -> never CLOSE here.
            self.release_mailbox()
        except Exception:  # noqa: BLE001
            pass

    def release_mailbox(self) -> None:
        """Leave the selected mailbox WITHOUT the implicit expunge of CLOSE.

        CLOSE on a read-write mailbox removes every message flagged \\Deleted - including messages that other
        clients flagged in INBOX/Trash/Sent. Only our own test folders may be closed; everything else uses UNSELECT
        (RFC 3691) or, without it, EXAMINE INBOX (switching mailboxes never expunges).
        """
        M = self.M
        assert M is not None
        if "UNSELECT" in self.caps:
            M.unselect()
        else:
            M.select("INBOX", readonly=True)

    def uid_copy_move(self, cmd: str, uid: str, target: str) -> tuple[str, Any]:
        """Run raw UID COPY/MOVE so the COPYUID response code is not lost. `target` is UTF-7 encoded."""
        M = self.M
        assert M is not None
        # imaplib keeps response codes (also of TAGGED replies) under untagged_responses["COPYUID"] and does not
        # clear that key between commands - drop it first, otherwise a stale COPYUID from an earlier COPY is reported.
        M.untagged_responses.pop("COPYUID", None)
        typ, data = M._simple_command("UID", cmd, uid, quote(target))  # noqa: SLF001 (raw: keeps COPYUID)
        return str(typ), data

    def copyuid_seen(self, data: Any) -> dict[str, Any]:
        """COPYUID may arrive in the tagged OK or as untagged '* OK [COPYUID ...]' (MOVE).

        Call directly after uid_copy_move(). imaplib stores tagged response codes under the same key, so
        'untagged' is only claimed when the tagged reply did not carry COPYUID.
        """
        assert self.M
        tagged_uids = parse_uid(data, "COPYUID")
        stored = [b"COPYUID " + x for x in (self.M.untagged_responses.get("COPYUID") or []) if isinstance(x, bytes)]
        store_uids = parse_uid(stored, "COPYUID")
        return {
            "tagged": bool(tagged_uids),
            "untagged": bool(store_uids) and not tagged_uids,
            "dest_uids": tagged_uids or store_uids,
        }

    def search_index_delay(self) -> dict[str, Any]:
        """Text search returned 0 hits right after APPEND: indexing delay or broken? Poll up to ~60 s."""
        out: dict[str, Any] = {"polls": []}
        t0 = time.monotonic()
        for wait in (0, 5, 10, 15, 30):
            if wait:
                time.sleep(wait)
            r = {
                "t": round(time.monotonic() - t0, 1),
                "body_probe": self.uid_search("BODY", "probe").get("hits"),
                "text_ascii": self.uid_search("TEXT", "ascii").get("hits"),
                "subject_probe": self.uid_search("SUBJECT", "probe").get("hits"),
            }
            out["polls"].append(r)
            if r["body_probe"] and r["text_ascii"] and r["subject_probe"]:  # SUBJECT lagged behind BODY in run 3
                break
        return out

    def find_token(self, token: str, seconds: float) -> dict[str, Any]:
        """Find OUR message by Message-ID token in the selected folder. GMX search indexes new mail with up to ~30 s
        delay, so poll; fall back to FETCH of the last 10 headers (index independent)."""
        assert self.M
        M = self.M
        t0 = time.monotonic()
        while True:
            ids = M.uid("SEARCH", "HEADER", "Message-ID", token)[1][0].split()
            if ids:
                return {"ids": [i.decode() for i in ids], "via": "search", "after_s": round(time.monotonic() - t0, 1)}
            allids = M.uid("SEARCH", "ALL")[1][0].split()[-10:]
            hits = []
            for u in allids:
                _typ, data = M.uid("FETCH", u.decode(), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                blob = b" ".join(x[1] for x in (data or []) if isinstance(x, tuple))
                if token.encode() in blob:
                    hits.append(u.decode())
            if hits:
                return {"ids": hits, "via": "fetch_headers", "after_s": round(time.monotonic() - t0, 1)}
            if time.monotonic() - t0 >= seconds:
                return {"ids": [], "via": None, "after_s": round(time.monotonic() - t0, 1)}
            time.sleep(5)

    def trash_move_test(self) -> dict[str, Any]:
        """Move one of OUR test messages to the real Trash folder, check counts, remove only that UID."""
        M = self.M
        assert M is not None
        trash = [n for n, fl in self.special.items() if fl.lower() == "\\trash"]
        if not trash:
            return {"skipped": "no \\Trash folder"}
        tname = utf7_encode(trash[0])
        out: dict[str, Any] = {}
        try:
            if M.select(mb(self.b))[0] != "OK":
                return {"skipped": "cannot select B"}
            # UNDELETED: without UIDPLUS the delete test leaves a \Deleted message in B that the CLOSE below expunges
            ids = M.uid("SEARCH", "UNDELETED")[1][0].split()
            if not ids:
                return {"skipped": "no test message left in B"}
            uid = ids[-1].decode()
            M.close()
            M.select(mb(tname), readonly=True)
            before = self.count_all()
            M.close()  # read-only (EXAMINE): CLOSE never expunges
            M.select(mb(self.b))
            typ, data = self.uid_copy_move("MOVE" if "MOVE" in self.caps else "COPY", uid, tname)
            out["move_status"] = typ
            out["copyuid"] = self.copyuid_seen(data)
            if "MOVE" not in self.caps:
                M.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                M.expunge()  # only in our own test folder B
            M.close()  # own test folder
            M.select(mb(tname))
            after = self.count_all()
            out["trash_before"] = before
            out["trash_after_move"] = after
            found = self.find_token(self.token, 60)  # Suchindex ~30 s verzoegert -> pollen + Header-Fallback
            out["our_message_in_trash"] = {"found": len(found["ids"]), "via": found["via"], "after_s": found["after_s"]}
            if "UIDPLUS" in self.caps:  # without UIDPLUS we cannot remove only our message -> leave it in Trash
                for u in found["ids"]:
                    M.uid("STORE", u, "+FLAGS", r"(\Deleted)")
                    M.uid("EXPUNGE", u)
            else:
                out["note"] = "no UIDPLUS: test message left in Trash (no mailbox-wide expunge)"
            out["trash_after_cleanup"] = self.count_all()
            self.release_mailbox()  # never CLOSE the real Trash: it would expunge foreign \Deleted messages
        except Exception as e:  # noqa: BLE001
            out["error"] = err(e)
        return out

    def inbox_search_probe(self) -> dict[str, Any]:
        """Read-only searches in INBOX: hit counts and timing only, no content."""
        M = self.M
        assert M is not None
        out: dict[str, Any] = {}
        typ, _ = M.select("INBOX", readonly=True)
        if typ != "OK":
            return {"error": "cannot examine INBOX"}
        out["total"] = self.count_all()
        for key, crit in (
            ("header_from_a", ("FROM", "a")),
            ("subject_e", ("SUBJECT", "e")),
            ("body_e", ("BODY", "e")),
            ("text_e", ("TEXT", "e")),
            ("since_30d", ("SINCE", imap_date(time.time() - 30 * 86400))),
        ):
            t = time.monotonic()
            r = self.uid_search(*crit)
            r["seconds"] = round(time.monotonic() - t, 2)
            out[key] = r
        M.close()
        return out

    def idle_probe(self) -> dict[str, Any]:
        """Hold an IDLE on INBOX for N minutes (re-issue never); report whether connection survived."""
        minutes = self.args.idle_minutes
        M = self.open_imap()
        out: dict[str, Any] = {"requested_minutes": minutes}
        try:
            M.login(self.user, self.password)
            M.select("INBOX", readonly=True)
            tag = M._new_tag()  # noqa: SLF001
            M.send(tag + b" IDLE\r\n")
            first = M.readline()
            out["continuation"] = first[:20].decode("utf-8", errors="replace")
            t0 = time.monotonic()
            lines: list[str] = []
            sock = M.sock
            while time.monotonic() - t0 < minutes * 60:
                pending = getattr(sock, "pending", lambda: 0)()
                if not pending and not select.select([sock], [], [], 30)[0]:
                    continue
                try:
                    ln = M.readline()
                except OSError as e:
                    out["closed_by_server_after_s"] = round(time.monotonic() - t0)
                    out["error"] = type(e).__name__
                    break
                if not ln:
                    out["closed_by_server_after_s"] = round(time.monotonic() - t0)
                    break
                lines.append(ln[:30].decode("utf-8", errors="replace"))
                if ln.startswith(b"* BYE"):
                    out["closed_by_server_after_s"] = round(time.monotonic() - t0)
                    break
            else:
                out["survived_s"] = round(time.monotonic() - t0)
            out["untagged_seen"] = lines[:5]
            try:
                M.send(b"DONE\r\n")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
        return out

    def cleanup_test_folders(self) -> None:
        M = self.M
        if M is None or not self.created:
            return
        res: dict[str, Any] = {}
        for name in list(self.created):
            try:
                typ, _ = M.select(mb(name))
                if typ == "OK":
                    ids = M.uid("SEARCH", "ALL")[1][0].split()
                    for u in ids:
                        M.uid("STORE", u.decode(), "+FLAGS", r"(\Deleted)")
                    if ids:
                        M.expunge()  # nur in unserem eigenen Testordner
                    M.close()
            except Exception as e:  # noqa: BLE001
                res["clear_folder"] = err(e)
        if self.a in self.created:
            try:
                new_a = self.a + "x"
                typ, _ = M.rename(mb(self.a), mb(new_a))
                res["rename_folder"] = typ
                if typ == "OK":
                    self.created[self.created.index(self.a)] = new_a
                    self.a = new_a
            except Exception as e:  # noqa: BLE001
                res["rename_folder"] = err(e)
        for i, name in enumerate(list(self.created)):
            try:
                typ, _ = M.delete(mb(name))
                res[f"delete_folder_{i + 1}"] = typ
            except Exception as e:  # noqa: BLE001
                res[f"delete_folder_{i + 1}"] = err(e)
        self.result["cleanup"] = res

    def cleanup_leftovers(self) -> dict[str, Any]:
        """Delete test folders left over from an aborted run (exact names only)."""
        M = self.M
        assert M is not None
        out: dict[str, Any] = {}
        for plain in (f"{self.args.folder_prefix}-\u00c4A", f"{self.args.folder_prefix}-\u00c4Ax", f"{self.args.folder_prefix}-\u00c4B"):
            if plain not in (self.folder_names or set()):
                out[utf7_encode(plain)] = "not present"
                continue
            try:
                typ, _ = M.delete(mb(utf7_encode(plain)))
                out[utf7_encode(plain)] = typ
            except Exception as e:  # noqa: BLE001
                out[utf7_encode(plain)] = err(e)
        return out

    def parallel_connections(self) -> dict[str, Any]:
        conns = []
        out: dict[str, Any] = {"requested_extra": min(self.args.parallel, MAX_PARALLEL - 1)}
        for i in range(out["requested_extra"]):
            try:
                if self.args.no_tls:
                    c = imaplib.IMAP4(self.imap_host, self.imap_port, timeout=20)
                else:
                    c = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=self.ctx, timeout=20)
                c.login(self.user, self.password)
                conns.append(c)
            except Exception as e:  # noqa: BLE001
                out[f"connection_{i + 2}"] = err(e)
                break
        out["parallel_ok_including_main"] = len(conns) + 1
        for c in conns:
            try:
                c.logout()
            except Exception:  # noqa: BLE001
                pass
        return out

    # ---- SMTP -------------------------------------------------------------------

    def smtp_probe(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        S = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT)
        try:
            S.ehlo()
            out["ehlo_pre_tls"] = sorted(S.esmtp_features.keys())
            S.starttls(context=self.ctx)
            S.ehlo()
            out["ehlo_post_tls"] = sorted(S.esmtp_features.keys())
            out["size_limit"] = S.esmtp_features.get("size")
            S.login(self.user, self.password)
            out["login"] = "ok"
        finally:
            try:
                S.quit()
            except Exception:  # noqa: BLE001
                pass
        return out

    def send_self_test(self) -> dict[str, Any]:
        """Sendet GENAU EINE Mail an sich selbst und prueft die Gesendet-Kopie. Loescht die Testmails danach."""
        M = self.M
        assert M is not None
        out: dict[str, Any] = {}
        send_token = f"{self.token}-send"
        msg_id = f"<{send_token}@ha-probe.invalid>"
        msg = EmailMessage()
        msg["From"] = self.user
        msg["To"] = self.user
        msg["Subject"] = "HA Probe Selbsttest"
        msg["Message-ID"] = msg_id
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg.set_content("Selbsttest, kann geloescht werden.")
        S = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT)
        try:
            S.ehlo()
            S.starttls(context=self.ctx)
            S.ehlo()
            S.login(self.user, self.password)
            refused = S.send_message(msg)
            out["smtp_send"] = {"refused": len(refused)}
        finally:
            try:
                S.quit()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(3)

        sent_names = [n for n, fl in self.special.items() if fl.lower() == "\\sent"]
        if not sent_names:
            sent_names = [n for n in (self.folder_names or set()) if n.lower() in {"gesendet", "sent"}]
        counts: dict[str, Any] = {}
        for label, name in [("INBOX", "INBOX")] + [(f"sent[{i}]", n) for i, n in enumerate(sent_names)]:
            try:
                typ, _ = M.select(mb(utf7_encode(name)) if name != "INBOX" else "INBOX")
                if typ != "OK":
                    counts[label] = {"select": typ}
                    continue
                found = self.find_token(send_token, self.args.send_wait)
                ids = found["ids"]
                counts[label] = {"copies_found": len(ids), "via": found["via"], "after_s": found["after_s"]}
                for u in ids:  # nur unsere eigenen Testmails loeschen
                    M.uid("STORE", u, "+FLAGS", r"(\Deleted)")
                    if "UIDPLUS" in self.caps:
                        M.uid("EXPUNGE", u)
                    else:
                        counts[label]["note"] = "no UIDPLUS: marked \\Deleted only (not expunged)"
                self.release_mailbox()  # never CLOSE INBOX/Sent: it would expunge foreign \Deleted messages
            except Exception as e:  # noqa: BLE001
                counts[label] = err(e)
        out["copies"] = counts
        out["interpretation"] = (
            "sent_copy_mode: 0 Kopien in Gesendet -> 'append' noetig; 1 Kopie -> 'none'/'server'; mehr -> Duplikate beachten"
        )
        return out

    # ---- main -------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.result["meta"] = {
            "script": "gmx_probe.py",
            "version": PROBE_VERSION,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": sys.version.split()[0],
            "token_prefix": self.token,
        }
        if not self.connect():
            return self.result
        assert self.M is not None
        try:
            self.step("namespace", lambda: self.M.namespace()[1][0].decode() if "NAMESPACE" in self.caps else "capability missing")  # type: ignore[union-attr]
            self.step("list", self.list_folders)
            if self.args.cleanup_leftovers:
                self.step("cleanup_leftovers", self.cleanup_leftovers)
                return self.result
            self.step("status_inbox", self.status_inbox)
            if self.step("create_test_folders", self.create_test_folders) is True:
                self.step("folder_tests_run", self.run_folder_tests)
            self.step("inbox_search_probe", self.inbox_search_probe)
            if not self.args.no_parallel:
                self.step("parallel_connections", self.parallel_connections)
            if not self.args.skip_smtp:
                self.step("smtp", self.smtp_probe)
            if self.args.send_self_test and not self.args.skip_smtp:
                self.step("send_self_test", self.send_self_test)
            self.step("manage_sieve_port_4190", self.sieve_port)
        finally:
            try:
                self.cleanup_test_folders()
            finally:
                try:
                    self.M.logout()
                except Exception:  # noqa: BLE001
                    pass
        if self.args.idle_minutes:  # eigene Verbindung, nach dem Aufraeumen (GMX trennt IDLE nach ~30 min)
            self.step("idle_probe", self.idle_probe)
        return self.result

    def sieve_port(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for host in (IMAP_HOST, SMTP_HOST):
            try:
                with socket.create_connection((host, 4190), timeout=5) as s:
                    s.settimeout(3)
                    banner = s.recv(64).decode("utf-8", errors="replace")
                res[host] = {"open": True, "banner_prefix": banner[:30]}
            except Exception as e:  # noqa: BLE001
                res[host] = {"open": False, "reason": type(e).__name__}
        return res


def main() -> int:
    ap = argparse.ArgumentParser(description="GMX Live-Probe (Metadaten only)")
    ap.add_argument("--print-folders", action="store_true", help="alle Ordnernamen ausgeben")
    ap.add_argument("--send-self-test", action="store_true", help="genau 1 Mail an sich selbst senden und Gesendet-Kopie pruefen")
    ap.add_argument(
        "--send-wait",
        type=int,
        default=60,
        help="max. Sekunden, die nach dem Senden auf die Kopien gewartet wird (Default 60; GMX-Suchindex braucht bis ~30 s)",
    )
    ap.add_argument("--no-parallel", action="store_true", help="Test paralleler Verbindungen auslassen")
    ap.add_argument("--parallel", type=int, default=3, help="zusaetzliche parallele Verbindungen (max 3)")
    ap.add_argument("--folder-prefix", default="HA-Probe", help="Praefix der Testordner")
    ap.add_argument("--imap-host", default=IMAP_HOST, help="nur fuer lokale Tests (Dovecot/GreenMail); Default imap.gmx.net")
    ap.add_argument("--imap-port", type=int, default=IMAP_PORT, help="nur fuer lokale Tests")
    ap.add_argument("--no-tls", action="store_true", help="Klartext-IMAP, nur fuer lokale Tests; nie gegen GMX")
    ap.add_argument(
        "--idle-minutes",
        type=int,
        default=0,
        help="IDLE auf INBOX so viele Minuten halten (z. B. 35) und pruefen, ob GMX die Verbindung trennt",
    )
    ap.add_argument(
        "--cleanup-leftovers",
        action="store_true",
        help="nur Testordner eines abgebrochenen Laufs loeschen (exakte Namen HA-Probe-AeA/AeAx/AeB), sonst nichts",
    )
    ap.add_argument("--skip-smtp", action="store_true", help="SMTP-Teil auslassen")
    ap.add_argument(
        "--cafile", default=None, help="eigene CA-Datei (PEM oder einzelnes DER-.cer), z. B. Root-CA des Ausstellers oder eines Proxys"
    )
    ap.add_argument("--tls-diagnose", action="store_true", help="nur Zertifikatskette und Vertrauensquellen pruefen (keine Zugangsdaten)")
    args = ap.parse_args()

    if args.no_tls and args.imap_host.endswith("gmx.net"):
        print(json.dumps({"error": "--no-tls ist gegen GMX nicht erlaubt"}), file=sys.stderr)
        return 2
    if args.tls_diagnose:
        print(json.dumps(tls_diagnose(args), indent=1, ensure_ascii=True, default=str))
        return 0
    user = os.environ.get("GMX_USER", "")
    password = os.environ.get("GMX_PASS", "")
    if not user or not password:
        print(json.dumps({"error": "GMX_USER und GMX_PASS muessen als Umgebungsvariablen gesetzt sein"}), file=sys.stderr)
        return 2

    result = Probe(user, password, args).run()
    print(json.dumps(result, indent=1, ensure_ascii=True, default=str))
    return 0 if result.get("login", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
