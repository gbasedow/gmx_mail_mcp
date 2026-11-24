# GMX Mail MCP

MCP-Server für ein GMX-Postfach als Home-Assistant-Integration (in Entwicklung, Version 0.0.5: Lesen und Organisieren; Löschen und Senden folgen).

- Zugriff per IMAP und SMTP, vollumfänglich: Ordner, Suche, Lesen, **ganze Anhänge**, Flags, Verschieben, Löschen, Entwürfe, Senden.
- Schutz: Long-Lived Access Token von Home Assistant (Bearer) **und** jedes Tool einzeln ein-/ausschaltbar; Voreinstellung nur lesen.
- Entwicklung mit Claude Code: Start mit `CLAUDE.md`, `SPEC.md`, `docs/OFFENE-PUNKTE.md`.

Stand: M0–M3 (Endpunkt, Config-Flow, 8 Lese-Tools, 9 Organisier-Tools), siehe `docs/M1.md` bis `docs/M3.md`. Die HA-freie IMAP/SMTP-Schicht
stammt aus [`gbasedow/gmx_mail`](https://github.com/gbasedow/gmx_mail). Löschen (M4) und Senden (M5) folgen nach `SPEC.md` §8.
