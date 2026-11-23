# OFFENE-PUNKTE.md – Fragen an den Nutzer

Stand 23.11.2026. Beantwortet sind bereits: Architektur (D2), Code-Basis (D1), Schutzmechanismen (D3), Zielclient (D4), Nr. 2–9 und 11–14 (D7–D16, 23.11.2026). Offen: nur Nr. 10 (Anhänge beim Senden) und Nr. 15 (Lizenz/Repo).
Zu jeder Frage steht ein Vorschlag; ohne Antwort wird der Vorschlag umgesetzt, sobald der Punkt im Meilenstein dran ist.

1. **Begriffe.** „HonorSystem“ = Home Assistant und „FTP zum Verschicken“ = SMTP? *Vorschlag: ja. FTP ist kein Mailprotokoll; GMX bietet nur IMAP und SMTP.*
2. ~~**Mehrere Konten?**~~ **ERLEDIGT – D11.**  Ein Config-Entry je GMX-Konto und ein Endpunkt je Entry, oder nur ein Konto? *Vorschlag: mehrere Entries möglich, Endpunkt `/api/gmx_mail_mcp/<entry_id>/mcp`.*
3. ~~**Ablösung von `gmx_mail`.**~~ **ERLEDIGT – siehe D7.**
   (ursprüngliche Frage: Soll die Integration `gmx_mail` nach Fertigstellung entfernt werden? Beide gleichzeitig am selben Konto stören sich (IDLE, Verbindungslimit). *Vorschlag: ablösen; bis dahin nur eine davon aktiv.*)
4. ~~**claude.ai über Nabu Casa.**~~ **ERLEDIGT – siehe D8; M0 prüft es trotzdem.**
   (ursprüngliche Frage: Der Connector in claude.ai erwartet nach meinem Kenntnisstand OAuth, nicht ein festes Bearer-Token (nicht verifiziert). Spike M0 prüft das. Wenn es nicht geht: (a) Claude Code/anderen Client nutzen, (b) OAuth-Schicht ergänzen (großer Aufwand, eigener Meilenstein), (c) Zugang über ha-mcp wie bisher. *Welche Option ist für dich akzeptabel?*)
5. ~~**Einzelschalter: wo?**~~ **ERLEDIGT – D12.**  (a) nur im Options-Flow (Voreinstellung), (b) zusätzlich als `switch`-Entitäten (schnell umschaltbar, aber ein LLM mit ha-mcp-Zugriff könnte sich Tools selbst freischalten). *Vorschlag: (a).*
6. ~~**Benutzerprüfung.**~~ **ERLEDIGT – siehe D9.**
   (ursprüngliche Frage: Genügt jedes gültige HA-Token, oder nur Administratoren oder ein bestimmter HA-Benutzer? *Vorschlag: eigener HA-Benutzer für den Zugriff, Option „erlaubter Benutzer“; Token dieses Benutzers.*)
7. **MCP-Bibliothek.** Offizielles `mcp`-Python-SDK (mehr Abhängigkeiten, Konfliktgefahr mit HA) oder schlanke eigene JSON-RPC-Schicht (nur 4 Methoden)? *Vorschlag: eigene Schicht, nach M0 entscheiden.*
8. ~~**Umfang „alle IMAP-Möglichkeiten“.**~~ **ERLEDIGT – D13.**  Zusätzlich zu den 21 Aktionen der Vorlage: `get_raw_message` (.eml), Threads, Ordner abonnieren, `.eml` hochladen (APPEND), `get_capabilities`, Keywords auflisten. Alles davon? Mailbox-weites `EXPUNGE` bleibt ausgeschlossen. *Vorschlag: ja, außer EXPUNGE.*
9. ~~**Anhänge abrufen.**~~ **ERLEDIGT – siehe D10, D14 (Limit konfigurierbar, 10 MB).**
   (ursprüngliche Frage: Rückgabe als MCP-Inhalt (Bild/Blob, base64) mit Limit (Vorschlag 10 MB), zusätzlich Textextraktion (PDF/DOCX/XLSX), Teilabruf für große Dateien? *Vorschlag: Blob + Limit + optionaler Teilabruf; Textextraktion als eigener späterer Schritt.*)
10. **Anhänge beim Senden.** Woher kommen Dateien? (a) nur Ordner `<media>/gmx_mail_mcp/outbox/` wie in der Vorlage, (b) base64 im Tool-Aufruf (Größen- und Kontextproblem), (c) Verweis auf eine Anhang-Referenz einer bestehenden Mail (weiterleiten). *Vorschlag: (a) und (c).*
11. ~~**Sensoren und Ereignisse.**~~ **ERLEDIGT – D15.**  Ungelesen-Zähler, Binary-Sensor und Ereignis „neue Mail“ aus der Vorlage übernehmen (IDLE-Verbindung bleibt dann dauerhaft offen)? *Vorschlag: ja, optional abschaltbar.*
12. ~~**Voreinstellung der Schalter.**~~ **ERLEDIGT – Voreinstellung nur Lesen, D12.**  Nur Stufe R an? Soll `trash` (reversibel) schon in der Voreinstellung an sein? *Vorschlag: nur R.*
13. ~~**Zugriff ohne Rückfrage-Schicht.**~~ **ERLEDIGT – siehe D12.**  Mit direktem MCP gibt es keine ha-mcp-Freigabe mehr. Sende-/Lösch-Tools nur zeitweise einschalten und sonst aus lassen? *Vorschlag: ja; Sende- und Lösch-Tools in der Voreinstellung aus.*
14. ~~**Python 3.14.**~~ **ERLEDIGT – D16.**  Die Vorlage setzt Python ≥ 3.14 voraus (HA 2026.11). Läuft deine Instanz darauf? *Vorschlag: vor M1 an der echten Version prüfen.*
15. **Repo und Lizenz.** Repo ist öffentlich, MIT (vorhanden). Passt das, auch wenn die Integration später Zugriff auf Postfächer vermittelt? *Vorschlag: so lassen.*
