# GMX Mail MCP

MCP-Server für ein GMX-Postfach als Home-Assistant-Integration (in Entwicklung, Version 0.0.5: Lesen und Organisieren; Löschen und Senden folgen).

- Zugriff per IMAP und SMTP, vollumfänglich: Ordner, Suche, Lesen, **ganze Anhänge**, Flags, Verschieben, Löschen, Entwürfe, Senden.
- Schutz: Long-Lived Access Token von Home Assistant (Bearer) **und** jedes Tool einzeln ein-/ausschaltbar; Voreinstellung nur lesen.
- Entwicklung mit Claude Code: Start mit `CLAUDE.md`, `SPEC.md`, `docs/OFFENE-PUNKTE.md`.

Stand: M0–M4 (Endpunkt, Config-Flow, 9 Lese-Tools inkl. Audit-Log, 9 Organisier-Tools, 3 Lösch-Tools), siehe `docs/M1.md` bis `docs/M4.md`. Die HA-freie IMAP/SMTP-Schicht
stammt aus [`gbasedow/gmx_mail`](https://github.com/gbasedow/gmx_mail). Senden (M5) folgt nach `SPEC.md` §8.
