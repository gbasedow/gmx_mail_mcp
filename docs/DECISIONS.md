# DECISIONS.md – Entscheidungen

| Nr. | Datum | Entscheidung | Begründung |
|---|---|---|---|
| D1 | 2026-11-23 | Client-Code aus `gbasedow/gmx_mail` wird **kopiert**; dieses Repo ist eigenständig. | Wahl des Nutzers. Kein gemeinsames Paket, kein Umbau der Vorlage. Folge: Fehlerkorrekturen müssen bei Bedarf in beiden Repos erfolgen. |
| D2 | 2026-11-23 | Der MCP-Server läuft als **eigene HA-Integration** mit HTTP-Endpunkt (`HomeAssistantView`, `requires_auth`). | Wahl des Nutzers. HA prüft Bearer-Tokens (auch Long-Lived) selbst; kein zusätzlicher Prozess. |
| D3 | 2026-11-23 | Schutzmechanismen der Vorlage werden übernommen (Allowlist, Vorschau + Code, Limits, Papierkorb zuerst) und **zusätzlich** je Tool schaltbar. Voreinstellung: nur lesende Tools an. | Wahl des Nutzers. Der Tool-Schalter allein begrenzt nicht, wohin ein eingeschaltetes Sende-Tool schreibt. |
| D4 | 2026-11-23 | Zugriff von außen über Nabu Casa mit claude.ai/App als Ziel-Client. | Wahl des Nutzers. Risiko Bearer-Token vs. OAuth, siehe SPEC §2 und OFFENE-PUNKTE Nr. 4; Spike M0 klärt das vor jeder weiteren Arbeit. |
| D5 | 2026-11-23 | Übernommen wurde nur die HA-freie Schicht (`client/`, `const.py`, `options.py`, `envelope.py`, `confirm.py`) samt 313 Tests; HA-gebundene Module (Aktionen, Coordinator, Flows) nicht. | Sie hängen an `hass.services` und müssen für MCP neu geschrieben werden. Importpfade auf `custom_components.gmx_mail_mcp` umgestellt, Tests laufen unter Python 3.14 ohne HA-Plugin. |
| D6 | 2026-11-23 | Übernommene Entscheidungen der Vorlage gelten weiter, außer „kein eigener MCP-Server“ (Vorlage Nr. 1) und die ha-mcp-Freigabe (Nr. 9): Nr. 3, 6, 10, 19–24, 36, 40 (Details in `gbasedow/gmx_mail`, `docs/DECISIONS.md`). | Gleiche GMX-Eigenheiten, gleiche Sicherheitsüberlegungen. |
