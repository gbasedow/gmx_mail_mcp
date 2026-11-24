# M0 – Spike: MCP-Endpunkt mit HA-Token (24.11.2026)

Ziel laut SPEC §8: minimaler Endpunkt (`initialize`, `ping`, `tools/list`, `tools/call`) mit `requires_auth`, Test mit Claude Code (Bearer-Token).
Ausgeführt **nur lokal**: frisches, wegwerfbares Home Assistant im Entwicklungscontainer. Keine produktive Instanz, kein GMX-Konto, keine echten Zugangsdaten.

## Gebaut

| Datei | Inhalt |
|---|---|
| `mcp/protocol.py` | Eigene JSON-RPC-Schicht (D20), HA-frei. Protokollversionen `2025-11-25`, `2025-06-18`, `2025-03-26`; unbekannte Version → neueste wird angeboten. Keine Batches. Tool-Liste wird **je Nachricht** neu ermittelt (Grundlage für die Einzelschalter in M1). Tool-Ausnahmen → `isError` ohne Details, Traceback nur im Log. Parameter werden nie geloggt. |
| `http.py` | `HomeAssistantView` `/api/gmx_mail_mcp/<entry_id>/mcp`, `requires_auth = True`, nur `POST`. Reihenfolge: HA-Token (HA selbst, 401) → Entry geladen? (404) → erlaubter Benutzer (403, D9; System-Benutzer und deaktivierte nie) → `Origin` (403 bei fremdem Origin) → `MCP-Protocol-Version` (400 bei unbekannter) → `Content-Type` (415) → `Accept` (406) → JSON (400 Parse-Error) → JSON-RPC. Notifications → 202. |
| `config_flow.py` | Nur Auswahl des erlaubten Benutzers (aktive, nicht system-generierte). Kontodaten folgen in M1. Schema mit `probatio` (D21). |
| `mcp/spike.py` | Tool `gmx_spike_status`: liest keine Mail, nur Verbindungstest. Wird mit M2 entfernt. |
| `tools/spike_bootstrap.py` | Richtet ein **frisches** Test-HA ein (Besitzer, Benutzer „MCP Client“, je ein Long-Lived Token, Entry über den echten Config-Flow per REST). Bricht ab, wenn die Instanz schon eingerichtet ist. Tokens nur in Dateien mit Modus 600, nie in der Ausgabe. |

## Prüfungen

### Automatisierte Tests (391 grün)

- `tests/client`: 313 (unverändert). `tests/mcp/test_protocol.py`: 38 (Nachrichtenformen, Fehlercodes, ungültige IDs, Versionsaushandlung, Schalter wirkt sofort, keine Fehlerdetails an den Client).
- `tests/ha` (HA 2026.10.4 aus dem Test-Plugin): 40. Fehlendes/ungültiges/leeres/Basic-Token → 401; Token als Query-Parameter → 401; **signierter Pfad (`authSig`) authentifiziert POST nicht** (Kontrolle: dieselbe Signatur wirkt bei GET auf einer Test-View); widerrufenes Token → 401; Token eines anderen (Admin-)Benutzers → 403; System-Benutzer → 403; deaktivierter Benutzer → abgelehnt; zwei Entries mit je eigenem Benutzer (D11); unbekannter/fremder/entladener Entry → 404; ohne Token kein Unterschied zwischen vorhandenem und unbekanntem Entry (401); GET/DELETE/PUT → 405; Header-Regeln; Token nie im Log. Config-Flow: Auswahl, keine System-/inaktiven Benutzer, Abbruch ohne Benutzer.
- `tests/probe`: 29 grün mit `-p no:homeassistant` (das HA-Test-Plugin blockiert Sockets).
- `ruff check`, `ruff format --check`, `mypy --strict`: sauber.

### Ende-zu-Ende mit echtem Home Assistant 2026.11.3 (Zielversion, D16)

Python 3.14.6, `hass` aus PyPI, Konfiguration nur `http` (127.0.0.1), `onboarding`, `config`, `logger`. Integration per Symlink.

| Prüfung | Ergebnis |
|---|---|
| Config-Flow per REST **nach** dem Start von HA (View wird nachträglich registriert) | ✔ Entry angelegt, Endpunkt erreichbar |
| curl ohne Token / mit ungültigem Token | ✔ 401 / 401 |
| curl mit Long-Lived Token des Besitzers (anderer Benutzer) | ✔ 403 |
| curl mit Long-Lived Token von „MCP Client“: `initialize`, Notification, `tools/list`, `tools/call` | ✔ 200 / 202 / 200 / 200 |
| GET, unbekannter Entry, fremder `Origin` | ✔ 405 / 404 / 403 |
| **Claude Code 2.1.331** (`claude -p`, `--mcp-config` mit `type: http` und Header `Authorization: Bearer ${GMX_MCP_TOKEN}`) | ✔ Server `connected`, Tool `mcp__gmx__gmx_spike_status` gelistet und aufgerufen, Antworttext korrekt |
| Claude Code mit Token des Besitzers (Gegenprobe) | ✔ Server `failed` (403 „User not allowed“), keine Tools |
| Token im HA-Log oder im Claude-Code-Transkript | ✔ nicht enthalten |

Danach: Test-HA beendet, Tokens und `.storage` gelöscht.

## Nicht geprüft / offen

1. **Nachtrag 24.11.2026 – entschieden mit D22:** claude.ai meldet sich per OAuth über HA an (HA-Login), Claude Code mit Long-Lived Token. Test `test_401_points_to_ha_oauth_metadata` bestätigt die OAuth-Discovery an unserem Endpunkt. Live mit claude.ai noch zu prüfen. Ursprünglicher Text:
   **claude.ai über Nabu Casa (D4, D8):** in diesem Container nicht prüfbar (keine erreichbare Instanz; Vorgabe: nichts gegen die produktive Instanz). Nach meinem nicht verifizierten Kenntnisstand bietet der Connector-Dialog von claude.ai OAuth oder keine Authentifizierung, aber kein Feld für einen festen `Authorization`-Header. Der Test muss auf deiner Instanz erfolgen; bis dahin ist der Weg für claude.ai **unbestätigt**. Mögliche Auswege, falls es scheitert: (a) Claude Code / anderer Client mit Header, (b) OAuth über HA (HA ist selbst OAuth-/IndieAuth-Server; so arbeitet die Core-Integration `mcp_server` mit claude.ai), (c) Zugriff über ha-mcp.
2. **hassfest und HACS-Action:** kein Docker-Daemon im Container; laufen in der CI (`validate.yml`).
3. **HA-Version der echten Instanz** (D16) weiterhin unbestätigt.
4. `iot_class` im Manifest ist `cloud_push`; ohne IDLE (D15) passt `cloud_polling` besser – in M1 anpassen. `aioimaplib` im Manifest wird in v1 evtl. nicht gebraucht.
5. Anfragegröße: HA begrenzt Request-Bodies (Standard 16 MiB). Für base64-Anhänge (D17, 10 MB → ≈ 13,4 MB base64) reicht das; in M5 mit Test absichern.
6. `GET` liefert 405 auch ohne Token (aiohttp prüft die Methode vor der Anmeldung). Verrät nur, dass die Integration installiert ist, nicht welche Entries es gibt.
