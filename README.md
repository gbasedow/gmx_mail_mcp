# GMX Mail MCP

MCP-Server für ein GMX-Postfach als Home-Assistant-Integration (in Entwicklung, Version 0.0.7: Lesen, Organisieren, Löschen, Senden).

- Zugriff per IMAP und SMTP, vollumfänglich: Ordner, Suche, Lesen, **ganze Anhänge**, Flags, Verschieben, Löschen, Entwürfe, Senden.
- Schutz: Long-Lived Access Token von Home Assistant (Bearer) **und** jedes Tool einzeln ein-/ausschaltbar; Voreinstellung nur lesen.
- Entwicklung mit Claude Code: Start mit `CLAUDE.md`, `SPEC.md`, `docs/OFFENE-PUNKTE.md`.

Stand: M0–M5 (Endpunkt, Config-Flow, 9 Lese-Tools inkl. Audit-Log, 9 Organisier-Tools, 3 Lösch-Tools, 4 Sende-Tools), siehe `docs/M1.md` bis `docs/M5.md`.
Die HA-freie IMAP/SMTP-Schicht stammt aus [`gbasedow/gmx_mail`](https://github.com/gbasedow/gmx_mail). Härtung und Release 0.1.0 folgen (M6, `SPEC.md` §8).
