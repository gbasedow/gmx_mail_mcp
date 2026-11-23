# SOURCES.md – Quellenverzeichnis der Recherche (Stand 20.11.2026)

Hinweis: Die Quellen wurden von einem Recherche-Agenten ausgewertet; Zitate und Zahlen aus Drittquellen sind in `GMX-NOTES.md`
mit Verlässlichkeit markiert. Vor Entscheidungen mit Folgen die Originalseite prüfen.

## GMX (offiziell)
- https://hilfe.gmx.net/pop-imap/imap/imap-serverdaten.html
- https://hilfe.gmx.net/pop-imap/einschalten.html
- https://hilfe.gmx.net/pop-imap/kein-empfang-mailprogramm.html
- https://hilfe.gmx.net/pop-imap/kein-versand-mailprogramm.html (Titel sinngemäß: „Ich kann keine E-Mails in meinem externen E-Mail-Programm versenden“)
- https://hilfe.gmx.net/pop-imap/imap/livemail.html
- https://hilfe.gmx.net/sicherheit/2fa/anwendungsspezifisches-passwort.html
- https://www.gmx.net/mail/sicherheit/zwei-faktor-authentifizierung/
- https://hilfe.gmx.net/email/ordner/systemordner.html
- https://hilfe.gmx.net/email/ordner/aufbewahrungszeit-festlegen.html
- https://hilfe.gmx.net/email/emails-nicht-finden.html
- https://hilfe.gmx.net/email/verwalten-und-suchen/emails-im-papierkorb.html
- https://hilfe.gmx.net/email/empfangen-und-lesen/dateianhaenge.html
- https://hilfe.gmx.net/email/kontakte-organisieren/weiterleitung-einrichten.html

## GMX (Drittquellen)
- https://www.heise.de/ratgeber/Thunderbird-GMX-Mailkonto-laesst-sich-nicht-einbinden-7357628.html
- https://bugs.kde.org/show_bug.cgi?id=204653 (Akonadi/GMX, CAPABILITY 2009)
- https://forum.emclient.com/t/imap-unable-to-connect/85933 (CAPABILITY 2022)
- https://github.com/mailcow/mailcow-dockerized/issues/5778 (CAPABILITY 2024)
- https://community.pmail.com/index.php?u=%2Ftopic%2F12340%2Fgmx-net-started-rejecting-login-credentials%2Fpost-58347
- https://www.thunderbird-mail.de/forum/thread/66525-wieso-2-kopien-von-gesendeter-mail-im-ordner-gesendet/
- thunderbird-mail.de Threads 93637 (Abschaltung IMAP) und 83066 (Verbindungslimit)
- https://bugzilla.mozilla.org/show_bug.cgi?id=255541 (Umlaut-Papierkorb)
- pcwelt.de (Versandlimits, 05.03.2022); https://manuals.jam-software.de (SmartSerialMail-FAQ)
- https://emailumziehen.de/hilfe/gmx-app-passwort ; https://maildienste.de/gmx/ ; https://bluemail.me/help/gmx-net-account/

## Home Assistant
- https://developers.home-assistant.io/docs/dev_101_services/
- https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/action-setup/
- https://www.home-assistant.io/integrations/imap/
- https://www.home-assistant.io/actions/smtp.send_message/
- https://www.home-assistant.io/blog/2026/07/01/release-20267/
- https://www.home-assistant.io/changelogs/core-2026.7/ und /core-2026.11/
- https://github.com/home-assistant/core/pull/181749 (Add IMAP search service)
- https://github.com/home-assistant/home-assistant.io/blob/current/source/_integrations/smtp.markdown
- https://community.home-assistant.io/t/smtp-yaml-configuration-removed/1016104
- https://community.home-assistant.io/t/smtp-in-yaml-phase-out/1020067
- https://pypi.org/project/homeassistant/2026.11.2/

## ha-mcp
- https://homeassistant-ai.github.io/ha-mcp/tools/
- https://github.com/homeassistant-ai/ha-mcp/issues/2876 (kein `target`-Parameter)

## Mail-MCP- und HA-Mail-Projekte
- https://github.com/Wh1isper/mcp-email-server (früher ai-zerolab) – https://mcp-email-server.wh1isper.top/ (guides, security)
- https://github.com/jgalea/mailbox-mcp (nicht geprüft)
- https://github.com/danieldotnl/ha-mailbox-mcp (nicht geprüft)
- https://github.com/dadalouis/Home-mail (nicht geprüft)
- https://github.com/Madseason41/ha-email-mcp
- https://github.com/n24q02m/better-email-mcp ; https://github.com/codefuturist/email-mcp ; https://github.com/nikolausm/imap-mcp-server
- https://github.com/wildsurfer/your-mail-mcp

## Claude / Connectors
- https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- https://claude.com/docs/connectors/custom/remote-mcp
