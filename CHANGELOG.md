# Changelog

Alle nennenswerten Änderungen. Versionen vor 0.1.0 wurden nicht als GitHub-Release veröffentlicht (D57).

## 0.1.0 – 2026-11-26

Erstes Release (M6: Härtung, Anleitung, Release). Details: `docs/M6.md`.

- **Löschen läuft nach einem Abbruch zu Ende** wie das Senden: Ein Zeitlimit des Tool-Aufrufs bricht ein begonnenes Löschen nicht
  mehr mittendrin ab; Zähler und Audit-Log (`late_ok`/`late_error`) stimmen (D61).
- **Versandmodus sichtbar:** Vorschau und Antwort der Sende-Tools nennen `send_mode`; bei „Beliebige Adressen“ zusätzlich die
  Warnung `recipients_unrestricted` und ein Reparaturhinweis in Home Assistant, solange der Modus gilt (D60).
- **Uploads bis zum Upload-Limit:** Dateien über ≈ 11,9 MB scheiterten an der 16-MiB-Grenze von Home Assistant (HTTP 413). Für den
  angemeldeten, zugelassenen Benutzer passt der Endpunkt die Grenze jetzt an das Upload-Limit an (D65).
- **Diagnose** ohne Zugangsdaten, Kontoadresse, Benutzer-ID und Inhalte (D63).
- Audit-Log: Der Fingerabdruck enthält den Tool-Namen (**ältere Einträge sind mit neuen nicht direkt vergleichbar**); Sende-Einträge
  nennen den Ordner „Gesendet“; Vorschauen zählen einheitlich (D62).
- `gmx_rename_folder` meldet `subscribed`; Vorschau von `gmx_send_draft` mit einheitlichen Zeilenenden.
- Beim Entfernen eines Kontos verschwinden auch seine Reparaturhinweise.
- Tote Reste der Vorlage (alte Optionsprüfung, Abschnitte, `permissions`, Abfrage-/IDLE-/Ereignis-Optionen) entfernt (D64).
- README als deutsche Nutzeranleitung (D66); CI prüft, dass Manifest-Version, CHANGELOG und Tag übereinstimmen.
- Logo aus `gmx_mail` übernommen (`brand/icon.png`, `icon@2x.png`; weißer Briefumschlag auf GMX-Blau, ohne GMX-Schriftzug, D69).

## Frühere Stände (ohne Release)

- 0.0.7 (M5): Sende-Tools (Stufe S, Voreinstellung aus) `gmx_send_message` (Text, optional HTML, Anhänge als base64 mit SHA-256), `gmx_reply` (optional an alle, Zitat), `gmx_forward` (inline, Anhänge des Originals standardmäßig dabei), `gmx_send_draft` (Entwurf danach aus „Entwürfe“ entfernt); alle mit Vorschau + Bestätigungscode, je Tool unter „Tools ohne Bestätigung“ wählbar. Optionen: Versandmodus „Nur Allowlist“ (Standard, leere Liste = kein Versand) oder „Beliebige Adressen“, Allowlist (Adressen, `*@domain`), Antworten an den Absender (aus), Absendername, Gesendet-Kopie (immer/auto/keine), Empfänger je Mail (10), Mails pro Tag (20) und Empfänger pro Stunde (10) – beide mit 0 abschaltbar –, Größenlimit gesendeter Mails (20 MB). Versandzähler in `.storage`, Empfänger-Domains (nie Adressen) im Audit-Log, Reparaturhinweis bei abgelehnter SMTP-Anmeldung. Sende-Tools tragen `destructiveHint`.
- 0.0.6 (M4): Lösch-Tools (Stufe D, Voreinstellung aus) `gmx_trash` (Papierkorb, ohne Code), `gmx_delete_permanently` (nur Papierkorb/Spam, nur mit Option, Vorschau + Bestätigungscode), `gmx_delete_folder` (nur leere Ordner, Vorschau + Code); je Aufruf höchstens 50 Mails. Optionen: endgültiges Löschen erlauben, Tageslimit (0 = ohne, Standard), Tools ohne Bestätigung (je Tool), Aufbewahrung des Audit-Logs (1–365 Tage, Standard 90). Audit-Log aller Aufrufe der Stufen O/D (ohne Inhalte) in `.storage`, Tool `gmx_get_audit_log` (Stufe R). `gmx_create_folder` abonniert neue Ordner, `gmx_rename_folder` überträgt das Abo. Beschreibung von `gmx_list_keywords` präzisiert (nur benutzte Schlagwörter).
- 0.0.5 (M3): Organisier-Tools (Stufe O, Voreinstellung aus) `gmx_set_flags`, `gmx_move`, `gmx_copy` (Papierkorb als Ziel gesperrt), `gmx_mark_spam`/`gmx_mark_not_spam` (`$Junk`/`$NotJunk`), `gmx_create_folder`, `gmx_rename_folder`, `gmx_subscribe_folder`, `gmx_save_draft` (Text, Anhänge als base64 mit SHA-256). Option Upload-Limit (1–20 MB, Standard 10). Reparaturhinweis und Liste im Optionsformular für Tools, die ein Update neu hinzufügt. `gmx_list_folders` meldet `subscribed`. **Geändert:** Anhangsliste von `gmx_get_message` mit `size_estimated`, `size_encoded`, `encoding` statt `size`. Beschreibungen von `gmx_search` (unscharfe Textsuche) und `gmx_get_thread` (`AW:`/`WG:`) ergänzt.
- 0.0.4 (M2): Lese-Tools `gmx_list_folders`, `gmx_search`, `gmx_get_message`, `gmx_get_attachment` (Text aus PDF/DOCX/XLSX oder ganze Datei), `gmx_get_raw_message`, `gmx_get_thread`, `gmx_list_keywords`; Capabilities in `gmx_get_status`; Optionen Größenlimit für Anhänge (1–10 MB) und HTML-Ausgabe. Neue Abhängigkeit `pypdf==7.1.1`.
- 0.0.3: Anmeldung am MCP-Endpunkt löst keine IP-Sperre mehr durch die Anmeldeaufforderung des Connectors oder abgelaufene Zugriffstoken aus; falsche oder gefälschte Token zählen weiter (D25).
- M1: Config-Flow mit GMX-Konto (Prüfung von IMAP- und SMTP-Login), erlaubtem Benutzer, Reauth und Reconfigure; Options-Flow mit Verbindungs-URL, Kopiervorlage für Claude Code und Tool-Schaltern (wirken sofort); IMAP-Verbindungsverwaltung mit Repair-Hinweis bei abgelehnter Anmeldung; Tool-Registry; Tool `gmx_get_status` ersetzt das Spike-Tool. Manifest: `iot_class` `cloud_polling`, ohne `aioimaplib`.
- M0 (Spike): MCP-Endpunkt `/api/gmx_mail_mcp/<entry_id>/mcp` mit eigener JSON-RPC-Schicht, HA-Token (`requires_auth`) und Prüfung des erlaubten Benutzers; Config-Flow nur mit Benutzerauswahl; Test-Tool `gmx_spike_status` (ohne Mailzugriff). Ende-zu-Ende mit HA 2026.11.3 und Claude Code geprüft, siehe `docs/M0-SPIKE.md`.
- Gerüst (Manifest mit `config_flow: false`, bis der Config-Flow existiert): Client-Schicht, Tests und Werkzeuge aus `gbasedow/gmx_mail` 0.9.3 übernommen; Entwurf von `SPEC.md` und offene Fragen.
