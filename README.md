# GMX Mail MCP

MCP-Server für ein GMX-Postfach als Home-Assistant-Integration (in Entwicklung, **noch nicht lauffähig**).

- Zugriff per IMAP und SMTP, vollumfänglich: Ordner, Suche, Lesen, **ganze Anhänge**, Flags, Verschieben, Löschen, Entwürfe, Senden.
- Schutz: Long-Lived Access Token von Home Assistant (Bearer) **und** jedes Tool einzeln ein-/ausschaltbar; Voreinstellung nur lesen.
- Entwicklung mit Claude Code: Start mit `CLAUDE.md`, `SPEC.md`, `docs/OFFENE-PUNKTE.md`.

Stand: Gerüst. Die HA-freie IMAP/SMTP-Schicht stammt aus [`gbasedow/gmx_mail`](https://github.com/gbasedow/gmx_mail) und ist getestet;
Config-Flow, MCP-Endpunkt und Tools folgen nach `SPEC.md` §8.
