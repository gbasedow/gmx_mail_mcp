# SPEC.md – gmx_mail_mcp

Entwurf vom 23.11.2026, abgeleitet aus Nutzervorgaben und der Vorlage `gbasedow/gmx_mail` (SPEC dort: Aktionen, Optionen, Sicherheit).
Punkte, die noch zu klären sind, stehen in `docs/OFFENE-PUNKTE.md` und sind hier mit **[offen N]** markiert.

## 1. Rahmen

- Eine HA-Integration, ein Config-Entry je GMX-Konto. Sie stellt einen **MCP-Server über HTTP** bereit (Transport „Streamable HTTP“,
  JSON-RPC 2.0; Methoden `initialize`, `ping`, `tools/list`, `tools/call`). Kein eigener Prozess, kein Add-on.
- Umfang: alles, was IMAP und SMTP hergeben. Nicht möglich: Webmail-Filterregeln (kein ManageSieve, GMX-NOTES Zeile 24), Quota (keine QUOTA-Capability).
- Zwei Schutzebenen: (1) Token, (2) Einzelschalter je Tool (§4).

## 2. Architektur

```
Client (Claude / Claude Code)
   │  HTTPS, Authorization: Bearer <HA Long-Lived Access Token>
   ▼
Home Assistant (http-Komponente: prüft das Token, setzt request["hass_user"])
   │
   ▼  HomeAssistantView  /api/gmx_mail_mcp/<entry_id>/mcp   (requires_auth = True)
 mcp/server.py     JSON-RPC, Protokollversion, tools/list (nur eingeschaltete Tools), tools/call
 mcp/tools/*.py    ein Modul je Tool-Gruppe; jedes Tool hat Name, Stufe (R/O/D/S), Schema, Handler
 mcp/guard.py      Reihenfolge je Aufruf: Benutzerprüfung → Tool eingeschaltet? → Limits → Handler → Audit
 client/*          IMAP/SMTP-Schicht (übernommen, HA-frei)
```

- **Token (geändert durch D25):** Der Endpunkt prüft das Bearer-Token selbst mit HAs eigener Prüfung; nur echte Fehlversuche zählen für die IP-Sperre (siehe `auth.py`). Ursprünglicher Entwurf: Der Endpunkt wird als `HomeAssistantView` mit `requires_auth = True` registriert. Home Assistant prüft das Bearer-Token selbst
  (Long-Lived Access Tokens eingeschlossen) und lehnt ungültige Aufrufe mit 401 ab, bevor Integrationscode läuft. Zusätzlich prüft `guard.py` den Benutzer
  (nur der im Config-Flow gewählte Benutzer, D9). Kein eigener Token-Speicher, kein Klartext-Token in der Konfiguration.
- **Transport:** nur `POST`; `GET` liefert 405 (kein Server-Push nötig). Zustandslos (keine MCP-Sitzungen). Eigene schlanke JSON-RPC-Schicht,
  kein `mcp`-Python-SDK (D20).
- **Anmeldung (D22):** Claude Code u. a. mit Long-Lived Token als Bearer; claude.ai per OAuth mit HA als Anmeldestelle (HA-Login des MCP-Benutzers). HA liefert dafür die 401-Challenge und die `.well-known`-Dokumente selbst; eigener OAuth-Code entfällt.
- **Erreichbarkeit von außen:** Nabu Casa Remote UI oder eigene HTTPS-Adresse. **Frühere Annahme (überholt durch D22):** Der Connector in claude.ai erwartet nach bisherigem
  Kenntnisstand OAuth statt eines festen Bearer-Tokens; Claude Code und andere Clients können den Header setzen. Der Nutzer betreibt ha-mcp auf diese Weise (D8); trotzdem vor allem anderen per Spike prüfen (M0).
- **Ressourcen:** `imapclient` im Executor mit einem `asyncio.Lock` je Entry (Vorlage `client/imap.py`), höchstens 2 IMAP-Verbindungen je Konto; ohne Sensoren keine dauerhafte IDLE-Verbindung (D15).

## 3. Tools (Vorschlag, Basis: 21 Aktionen der Vorlage)

Stufen: **R** lesen · **O** organisieren · **D** löschen · **S** senden. Namen im MCP: `gmx_<name>`.

| Tool | Stufe | Inhalt |
|---|---|---|
| `list_folders` | R | Ordner mit Rollen (SPECIAL-USE) und Zählern |
| `search` | R | Suche mit Feldern, Paginierung, Fallback für frische Mails (Indexverzögerung ≈ 30 s) |
| `get_message` | R | Kopfzeilen, bereinigter Text, Anhangsliste; optional HTML (nur mit Option) |
| `get_attachment` | R | **Ganze Datei** (siehe §5) |
| `get_raw_message` *(neu)* | R | Vollständige RFC-822-Nachricht (`.eml`) (D13) |
| `get_status`, `get_audit_log` | R | Verbindungs-/Kontostatus mit Capabilities (M2), Audit-Log (M4, D41) |
| `get_thread`, `list_keywords` *(neu, M2)* | R | Gesprächsverlauf (ORDEREDSUBJECT), Schlagwörter eines Ordners (D28) |
| `set_flags`, `move`, `copy`, `mark_spam`, `mark_not_spam` | O | Flags, Ordnerwechsel; Papierkorb als Ziel gesperrt (D32), Spam mit `$Junk`/`$NotJunk` (D33) – **M3** |
| `create_folder`, `rename_folder`, `subscribe_folder` | O | Ordnerverwaltung, Abo (D34) – **M3**; neue Ordner werden abonniert, Umbenennen überträgt das Abo (D44, M4) |
| `save_draft` | O | Entwurf per APPEND, Text und Anhänge (D35) – **M3** |
| `trash` | D | In den Papierkorb, ohne Code (D38) – **M4** |
| `delete_permanently`, `delete_folder` | D | endgültig nur mit Option; Ordner nur leer (D40); Vorschau + Bestätigungscode, je Tool abschaltbar (D45) – **M4** |
| `send_message`, `reply`, `forward`, `send_draft` | S | Versand-Modus `off`/`allowlist`/`any`, Vorschau + Code |

Weitere IMAP-Möglichkeiten für „vollumfänglich“ (D13): Threads (`THREAD=ORDEREDSUBJECT`), Ordner abonnieren, `append` einer hochgeladenen `.eml`,
`get_capabilities`, Keywords auflisten. Nicht aufgenommen: mailbox-weites `EXPUNGE`. `.eml` hochladen ist zurückgestellt (D34).

## 4. Sicherheit

1. **Token:** nur gültige HA-Tokens, 401 sonst. Nur der gewählte HA-Benutzer (D9); Tokens anderer Benutzer werden mit 403 abgelehnt.
2. **Einzelschalter je Tool.** Gespeichert in den Optionen des Config-Entry (`enabled_tools`), Voreinstellung: **nur Stufe R an**, alles andere aus. Gespeicherte Auswahl ist explizit; neue Tools aus Updates bleiben aus (D23), ein Reparaturhinweis und das Optionsformular nennen sie bis zum nächsten Speichern (D31).
   `tools/list` zeigt nur eingeschaltete Tools; `tools/call` auf ein ausgeschaltetes Tool wird abgelehnt, auch wenn der Client den Namen kennt.
   **Wichtig:** Schalter nicht als HA-Entitäten (`switch`) anlegen, wenn ein LLM über ha-mcp Zugriff auf `switch.turn_on` hat – es könnte sich sonst selbst
   Tools freischalten. Entscheidung D12: nur im Konfigurationsfluss.
3. **Versand:** Allowlist/Modus, Empfänger- und Stundenlimit, Vorschau + Bestätigungscode (aus der Vorlage: `client/policy.py`, `confirm.py`).
   Der Code erzwingt einen Zwischenschritt, ersetzt aber **keine** menschliche Freigabe; harte Grenze ist die Allowlist.
   Der Nutzer kann den Code **je Tool** im Optionsdialog abschalten (`tools_without_confirmation`, D45; Voreinstellung: überall an). Mit direktem MCP-Zugriff gibt es keine
   ha-mcp-Freigabe mehr – die Rückfrage übernimmt der Client (Claude fragt bei Connectoren standardmäßig vor Tool-Aufrufen).
4. **Löschen:** Papierkorb zuerst; endgültig nur mit Option, nur Papierkorb/Spam, `UID EXPUNGE` genau dieser UIDs; höchstens 50 Mails je Aufruf.
   Tageslimit optional (`max_delete_per_day`, Standard 0 = ohne, D39 ändert den ursprünglichen Entwurf).
5. **Inhalte sind Daten:** Rahmen mit Zufallskennung, `content_trust`, Bereinigung versteckter Texte und unsichtbarer Zeichen (`client/sanitize.py`),
   Angriffssammlung als Test (`tests/client/test_injection_corpus.py`). Das gilt auch für Anhänge (§5).
6. **Audit-Log** je Aufruf der Stufen O/D/S, auch Vorschau, Ablehnung, Fehler und Abbruch (Tool, Zeit, Benutzer, Ordnernamen, Anzahl, Fingerabdruck der Parameter, Ergebnis; keine Inhalte), D41; Aufbewahrung D42.
7. Keine Zugangsdaten in Logs, Diagnose und Antworten. Fehler als JSON-RPC-Fehler bzw. `isError` mit Übersetzungsschlüssel.

## 5. Anhänge (Kernanforderung)

- `get_attachment` hat den Parameter `mode`: `text` (extrahierter Text), `file` (ganze Datei) oder `both` (D10).
  Die Datei geht ungekürzt an das LLM, nicht nur Text. Rückgabeform nach MCP: Bilder als `image`-Content, andere Dateien als
  eingebettete Ressource (`resource` mit `blob`, base64) samt `mimeType`, Dateiname, Größe, SHA-256.
- Umgesetzt in M2 (`docs/M2.md`): Typ nach Inhalt, Bilder als MCP-Bild, sonst eingebettete Ressource; Client-Grenzen siehe D26 (claude.ai ≈ 110 KB).
- Größenlimit je Abruf als Option (Voreinstellung 10 MB, D14; GMX erlaubt bis ≈ 28 MiB je Nachricht, `APPENDLIMIT`). Größere Dateien: Fehler mit Größenangabe,
  Teilabruf per Offset/Länge als spätere Erweiterung.
- Textextraktion (PDF, DOCX, XLSX, reine Textformate) ist Teil des Umfangs (D10); Abhängigkeiten nur reines Python; Auswahl der Bibliotheken in M2 (D10 nennt sie als offen).
- Anhänge sind **untrusted**: kein Ausführen, keine Makros, Typ nach Inhalt prüfen, Dateiname bereinigen. Streaming per Teil-FETCH (`BODY.PEEK[n]<o.l>`),
  kein vollständiges Laden großer Nachrichten in den Speicher.
- Hochladen (D17, umgesetzt in M3 für Entwürfe, `client/upload.py`): Dateien kommen als base64 im Tool-Aufruf (`attachments[]`: Name, MIME-Typ, Inhalt, SHA-256). Eigenes Upload-Limit (konfigurierbar, Standard 10 MB),
  Gesamtgröße der Nachricht gegen das Limit der Optionen und `APPENDLIMIT` prüfen. Dateiname bereinigen, Typ nach Inhalt prüfen.

## 6. Konfiguration

- Config-Flow: E-Mail, App-Passwort, Hosts/Ports (Voreinstellung GMX), Prüfung von IMAP- und SMTP-Login. Reauth, Reconfigure, Options-Flow.
- Optionen: `enabled_tools`, Versand-Modus, Allowlist, Limits (aus `const.py`/`options.py` der Vorlage), Größenlimit Anhänge, Ordner-Overrides, Audit-Aufbewahrung.
- Verbindungs-URL (extern/Nabu Casa, sonst intern; wie ha-mcp auf dem Konfigurieren-Bildschirm) und eine Kopiervorlage für Claude Code werden im Options-Flow angezeigt (kein Token), D22.

## 7. Tests

- `tests/client`: HA-freie Client-Schicht mit In-Memory-Fakes (313 Tests, Python 3.14) – **vorhanden**.
- `tests/probe`: Probe-Skript gegen Fake-IMAP – vorhanden. `tests/integration`: GreenMail und GMX-naher Dovecot (TLS mit Test-CA) – vorhanden, für die CI noch zu verdrahten.
- Neu: `tests/mcp` (Protokoll, Schalter, Guard, Fehlerfälle), `tests/ha` (Config-Flow, Endpunkt mit gültigem/ungültigem/fehlendem Token,
  Admin/Nicht-Admin), Staging mit echtem HA im Container, Ende-zu-Ende mit einem MCP-Client (Bearer-Token).
- Pflicht: ein Test je Tool, dass es im ausgeschalteten Zustand weder gelistet noch aufrufbar ist.

## 8. Meilensteine

| M | Inhalt |
|---|---|
| M0 | **Spike:** minimaler Endpunkt `ping`/`tools/list` mit `requires_auth`; Test mit Claude Code (Bearer) und mit claude.ai über Nabu Casa. Prüft D8 (Bearer-Weg); Nr. 7 ist mit D20 entschieden. **Stand 24.11.2026:** Claude Code ✔ (lokal, HA 2026.11.3), claude.ai offen – siehe `docs/M0-SPIKE.md`. |
| M1 | Config-Flow, Optionen, Verbindungsverwaltung (aus `client/imap.py`), Endpunkt, Tool-Registry mit Einzelschaltern, Benutzerprüfung. |
| M2 | Lesetools inkl. `get_attachment` (ganze Datei). |
| M3 | Organisieren und Entwürfe (Stufe O). **Stand 24.11.2026:** gebaut (0.0.5), Live-Test bestanden – siehe `docs/M3.md`. |
| M4 | Löschen (Stufe D) mit Vorschau/Code, Audit-Log. **Stand 24.11.2026:** gebaut (0.0.6), Live-Test offen – siehe `docs/M4.md`. |
| M5 | Senden (Stufe S), Gesendet-Kopie, Limits. |
| M6 | Härtung, Anleitung, CI (hassfest, HACS), Release 0.1.0. |

Je Meilenstein: Tests grün, Bericht, Freigabe des Nutzers.

## 9. Risiken

- claude.ai-Connector und festes Bearer-Token (siehe §2). Fallback: Claude Code, anderer Client, oder OAuth-Schicht (großer Aufwand).
- Direkter MCP-Zugriff umgeht die ha-mcp-Freigabe; daher Voreinstellung nur Lesen.
- GMX: IMAP-Abschaltung bei Nichtnutzung (Frist unbekannt), Versandlimits unveröffentlicht, Suchindex ≈ 30 s verzögert (GMX-NOTES).
- Große Anhänge belasten Speicher und Kontextfenster des LLM.
