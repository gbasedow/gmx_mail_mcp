# Changelog

## Unreleased
- 0.0.3: Anmeldung am MCP-Endpunkt löst keine IP-Sperre mehr durch die Anmeldeaufforderung des Connectors oder abgelaufene Zugriffstoken aus; falsche oder gefälschte Token zählen weiter (D25).
- M1: Config-Flow mit GMX-Konto (Prüfung von IMAP- und SMTP-Login), erlaubtem Benutzer, Reauth und Reconfigure; Options-Flow mit Verbindungs-URL, Kopiervorlage für Claude Code und Tool-Schaltern (wirken sofort); IMAP-Verbindungsverwaltung mit Repair-Hinweis bei abgelehnter Anmeldung; Tool-Registry; Tool `gmx_get_status` ersetzt das Spike-Tool. Manifest: `iot_class` `cloud_polling`, ohne `aioimaplib`.
- M0 (Spike): MCP-Endpunkt `/api/gmx_mail_mcp/<entry_id>/mcp` mit eigener JSON-RPC-Schicht, HA-Token (`requires_auth`) und Prüfung des erlaubten Benutzers; Config-Flow nur mit Benutzerauswahl; Test-Tool `gmx_spike_status` (ohne Mailzugriff). Ende-zu-Ende mit HA 2026.11.3 und Claude Code geprüft, siehe `docs/M0-SPIKE.md`.
- Gerüst (Manifest mit `config_flow: false`, bis der Config-Flow existiert): Client-Schicht, Tests und Werkzeuge aus `gbasedow/gmx_mail` 0.9.3 übernommen; Entwurf von `SPEC.md` und offene Fragen.
