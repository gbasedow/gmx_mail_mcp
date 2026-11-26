# GMX Mail MCP

<img src="custom_components/gmx_mail_mcp/brand/icon.png" alt="Logo" width="96" align="right">

Home-Assistant-Integration, die ein GMX-Postfach als **MCP-Server** bereitstellt. Ein Sprachmodell (Claude in claude.ai,
der Claude-App oder Claude Code) kann damit über IMAP und SMTP mit dem Postfach arbeiten: Ordner, Suche, Lesen, ganze
Anhänge, Flags, Verschieben, Löschen, Entwürfe, Senden, Antworten, Weiterleiten.

Version 0.1.0. Getestet gegen ein echtes GMX-FreeMail-Konto und Home Assistant 2026.11.

**Schutz auf zwei Ebenen:** Zugriff nur mit der Anmeldung eines bestimmten Home-Assistant-Benutzers, und **jedes Tool ist
einzeln ein- und ausschaltbar**. In der Voreinstellung sind nur lesende Tools an.

---

## Inhalt

1. [Tools](#tools)
2. [Voraussetzungen](#voraussetzungen)
3. [Installation](#installation)
4. [Einrichtung](#einrichtung)
5. [Verbinden: claude.ai und Claude Code](#verbinden-claudeai-und-claude-code)
6. [Optionen](#optionen)
7. [Sicherheit – bitte lesen](#sicherheit--bitte-lesen)
8. [GMX-Eigenheiten und Grenzen](#gmx-eigenheiten-und-grenzen)
9. [Fehlersuche und Diagnose](#fehlersuche-und-diagnose)
10. [Entwicklung](#entwicklung)

---

## Tools

Stufen: **R** lesen · **O** organisieren · **D** löschen · **S** senden. Voreinstellung: nur Stufe R eingeschaltet.

| Stufe | Tools | Kurz |
|---|---|---|
| R | `gmx_get_status`, `gmx_list_folders`, `gmx_search`, `gmx_get_message`, `gmx_get_attachment`, `gmx_get_raw_message`, `gmx_get_thread`, `gmx_list_keywords`, `gmx_get_audit_log` | Status, Ordner, Suche, Mail lesen (ohne sie als gelesen zu markieren), Anhänge als Text oder ganze Datei, Original (.eml), Verlauf, Schlagwörter, Audit-Log |
| O | `gmx_set_flags`, `gmx_move`, `gmx_copy`, `gmx_mark_spam`, `gmx_mark_not_spam`, `gmx_create_folder`, `gmx_rename_folder`, `gmx_subscribe_folder`, `gmx_save_draft` | Flags und Schlagwörter, Verschieben/Kopieren (nicht in den Papierkorb), Spam, Ordner anlegen/umbenennen/abonnieren, Entwurf mit Anhängen |
| D | `gmx_trash`, `gmx_delete_permanently`, `gmx_delete_folder` | Papierkorb (umkehrbar), endgültig löschen (nur aus Papierkorb/Spam, mit Bestätigung), leere Ordner löschen (mit Bestätigung) |
| S | `gmx_send_message`, `gmx_reply`, `gmx_forward`, `gmx_send_draft` | Neue Mail, Antwort, Weiterleitung mit Anhängen, Entwurf senden – jeweils mit Vorschau und Bestätigung |

Nicht möglich, weil GMX es per IMAP/SMTP nicht anbietet: Filterregeln des Webmailers, Speicherplatz-Anzeige.

## Voraussetzungen

- **Home Assistant 2026.11** oder neuer (Python 3.14).
- **GMX-Konto mit freigeschaltetem IMAP:** im GMX-Webmailer unter *E-Mail-Einstellungen > E-Mail empfangen > POP3/IMAP* „POP3- und
  IMAP-Zugriff erlauben“ einschalten.
- **App-Passwort** (empfohlen, mit Zwei-Faktor-Anmeldung Pflicht): *Account verwalten > Login & Sicherheit > App-Passwörter*. Das
  normale GMX-Passwort nicht verwenden.
- **Ein eigener Home-Assistant-Benutzer** nur für den MCP-Zugriff (*Einstellungen > Personen > Benutzer hinzufügen*). Nur dieser
  Benutzer erhält Zugriff; Anmeldungen anderer Benutzer werden abgelehnt. Er braucht keine Administratorrechte.
- Für claude.ai bzw. die Claude-App: Home Assistant ist **von außen über HTTPS** erreichbar (Nabu Casa Remote UI oder eigene Adresse).
  Für Claude Code im Heimnetz genügt die interne Adresse.
- Nicht gleichzeitig mit der älteren Integration `gmx_mail` am **selben** Konto betreiben (GMX begrenzt die Zahl der Verbindungen).

## Installation

Über HACS als benutzerdefiniertes Repository:

1. HACS öffnen > Menü (⋮) > *Benutzerdefinierte Repositories*.
2. Repository `https://github.com/gbasedow/gmx_mail_mcp`, Typ *Integration*, hinzufügen.
3. „GMX Mail MCP“ herunterladen und Home Assistant neu starten.

Ohne HACS: den Ordner `custom_components/gmx_mail_mcp` aus dem Release nach `<config>/custom_components/` kopieren und neu starten.

## Einrichtung

*Einstellungen > Geräte & Dienste > Integration hinzufügen > GMX Mail MCP*:

1. **E-Mail-Adresse** und **App-Passwort** eingeben. Server und Ports sind für GMX vorbelegt (IMAP `imap.gmx.net:993` TLS, SMTP
   `mail.gmx.net:587` STARTTLS) und müssen normalerweise nicht geändert werden.
2. **Erlaubten Benutzer** wählen (der eigene MCP-Benutzer aus den Voraussetzungen).

Die Integration prüft die Anmeldung bei IMAP und SMTP. Mehrere GMX-Konten sind möglich; jedes bekommt einen eigenen Endpunkt.
Ein neues App-Passwort trägt man über *Neu konfigurieren* bzw. die Aufforderung zur erneuten Anmeldung ein.

## Verbinden: claude.ai und Claude Code

Unter *Einstellungen > Geräte & Dienste > GMX Mail MCP > Konfigurieren* steht oben die **Verbindungs-URL** dieses Kontos, z. B.
`https://<deine-adresse>/api/gmx_mail_mcp/<entry_id>/mcp`, dazu eine Kopiervorlage für Claude Code.

**claude.ai / Claude-App:** In den Connector-Einstellungen einen benutzerdefinierten Connector hinzufügen, Namen vergeben, die
Verbindungs-URL eintragen. Beim Verbinden öffnet sich die **Anmeldeseite von Home Assistant**: dort mit dem erlaubten
MCP-Benutzer anmelden. Kein Token einfügen; die Anmeldung läuft über Home Assistant (OAuth).

**Claude Code:** Mit dem erlaubten Benutzer in Home Assistant ein **langlebiges Zugriffstoken** erstellen (*Profil > Sicherheit*),
dann die Kopiervorlage aus dem Optionsdialog ausführen:

```
claude mcp add --transport http gmx-mail <Verbindungs-URL> --header "Authorization: Bearer <TOKEN>"
```

Das Token ist ein Zugangsschlüssel: nicht weitergeben, nicht in Repositories, Chats oder Screenshots ablegen; bei Verdacht in
Home Assistant widerrufen.

Ausgeschaltete Tools sind für das Modell unsichtbar und nicht aufrufbar. Änderungen an den Schaltern wirken sofort.

## Optionen

*Konfigurieren* öffnet den Optionsdialog. Alles wirkt ohne Neustart.

| Option | Voreinstellung | Bedeutung |
|---|---|---|
| Eingeschaltete Tools | nur Stufe R | Jedes Tool einzeln. Tools aus späteren Updates bleiben aus, bis sie eingeschaltet werden; ein Reparaturhinweis nennt sie. |
| Größenlimit für Anhänge und .eml | 10 MB (1–10) | Größte Datei bzw. Originalmail, die `gmx_get_attachment` / `gmx_get_raw_message` ganz liefern. |
| Größenlimit für Dateien beim Hochladen | 10 MB (1–20) | Größte Datei (und Summe) je Aufruf für Entwürfe und Senden. |
| HTML-Ausgabe erlauben | aus | Bereinigtes HTML beim Lesen; sonst nur Text. |
| Endgültiges Löschen erlauben | aus | Ohne diese Option kann `gmx_delete_permanently` nichts löschen. |
| Tageslimit für gelöschte Mails | 0 = ohne | Papierkorb und endgültig zusammen, je Kalendertag; je Aufruf höchstens 50 Mails. |
| Tools ohne Bestätigung | leer | Siehe [Sicherheit](#sicherheit--bitte-lesen). |
| Wer darf Mails erhalten | Nur Allowlist | *Nur Allowlist* oder *Beliebige Adressen*. |
| Allowlist | leer | Eine Adresse oder `*@domain` je Zeile. Leere Liste = es kann nichts gesendet werden. |
| Antworten an den Absender erlauben | aus | `gmx_reply` darf dem Absender jeder Mail antworten, auch ohne Allowlist-Eintrag. |
| Absendername | leer | Anzeigename vor der Kontoadresse (empfohlen, siehe GMX-Eigenheiten). |
| Kopie im Ordner Gesendet | Immer Kopie ablegen | GMX legt beim Senden per SMTP keine Kopie an; die Integration tut es. |
| Empfänger je Mail / Mails pro Tag / Empfänger pro Stunde | 10 / 20 / 10 | Tag und Stunde: 0 = ohne Limit. |
| Größenlimit für gesendete Mails | 20 MB (1–100) | Für die fertige Mail inkl. Anhängen. |
| Audit-Log aufbewahren | 90 Tage | 1–365 Tage, höchstens 5000 Einträge. |

## Sicherheit – bitte lesen

**Mailinhalte sind fremde Daten.** Jede Mail und jeder Anhang kann Text enthalten, der das Modell zu etwas verleiten soll
(„leite alle Rechnungen an … weiter“). Die Integration kennzeichnet Inhalte als nicht vertrauenswürdig und entfernt versteckten
Text, kann aber nicht garantieren, dass ein Modell darauf nicht hereinfällt. Deshalb:

- **Nur einschalten, was gebraucht wird.** Lösch- und Sende-Tools am besten nur bei Bedarf einschalten.
- **Versand: „Nur Allowlist“ mit den eigenen Adressen** ist die harte Grenze. Bei *Beliebige Adressen* kann ein eingeschaltetes
  Sende-Tool an jede Adresse senden; dann zeigt Home Assistant einen Reparaturhinweis, und Vorschau und Antwort tragen die
  Warnung `recipients_unrestricted`.
- **Vorschau und Bestätigungscode:** Endgültiges Löschen, Ordner löschen und alle Sende-Tools zeigen zuerst eine Vorschau und
  verlangen dann einen Code. Der Code erzwingt einen Zwischenschritt, in dem der Client nachfragen soll – er **beweist keine
  Zustimmung**. Prüfe Vorschauen (Empfänger, Text, Anhänge) selbst.
- **„Tools ohne Bestätigung“** schaltet diesen Zwischenschritt je Tool ab. Dann löscht bzw. sendet **eine einzige** fehlgeleitete
  Anweisung sofort. Nur vorübergehend und bewusst verwenden.
- **„Antworten an den Absender erlauben“** gibt jeder empfangenen Mail einen Weg nach draußen (eine präparierte Mail kann eine
  Antwort an sich auslösen). Voreinstellung aus lassen.
- **HTML beim Senden** (`body_html`) wird nicht bereinigt und in der Vorschau nur gezählt; es kann versteckten Text, Links oder
  Tracking-Bilder enthalten.
- **Spam:** Als Spam markierte Mails löscht GMX nach 30 Tagen. `gmx_mark_spam` gehört trotzdem zur Stufe O.
- **Ohne Tageslimit** kann ein fehlgeleitetes Modell über viele Aufrufe das Postfach in den Papierkorb räumen (umkehrbar).
- **Versandlimits:** GMX veröffentlicht keine Grenzen. Ohne Tages-/Stundenlimit drohen bei vielen Mails Sperren des Kontos.
  Die Limits zählen nur Mails dieser Integration.
- **Audit-Log:** Jeder Aufruf der Stufen O, D und S wird mit Zeit, Tool, Ergebnis, Anzahl, Ordnernamen, Empfänger-Domains und einem
  Fingerabdruck der Argumente gespeichert – nie Inhalte, Betreffs, Adressen oder Dateinamen. Abrufbar mit `gmx_get_audit_log`.
- Ein abgebrochener Aufruf (Zeitüberschreitung) bricht ein begonnenes Löschen oder Senden nicht ab; es läuft zu Ende und erscheint
  im Audit-Log als `late_ok` bzw. `late_error`. Eine Mail wird nie doppelt gesendet.
- Die Integration ruft keine Home-Assistant-Dienste auf und schaltet keine Geräte.

## GMX-Eigenheiten und Grenzen

- **IMAP-Abschaltung:** GMX schaltet IMAP nach längerer Nichtnutzung ab und meldet dann ein falsches Passwort. Die Integration zeigt
  einen Reparaturhinweis; IMAP im Webmailer wieder einschalten und ggf. neu anmelden.
- **Suchindex:** Neue Mails sind per Textsuche erst nach etwa 30 Sekunden auffindbar; die Suche prüft frische Mails zusätzlich über
  die Kopfzeilen. Die GMX-Textsuche ist unscharf (Satzzeichen werden ignoriert, Wortteile treffen).
- **Verläufe:** GMX gruppiert nur nach Betreff; `AW:`/`WG:` ergeben getrennte Verläufe.
- **Absendername:** Kurze Testmails ohne Absendernamen landeten in einem Test im Spamordner.
- **Dateien im Chat:** claude.ai nimmt je Tool-Antwort nur etwa 110 KB Datei an; dort Anhänge als Text abrufen. Hochgeladene
  Dateien muss das Modell als base64 ausgeben – große Dateien sind langsam und belasten den Chat. Claude Code kommt mit größeren
  Dateien zurecht.
- Ordner abonnieren: GMX abonniert per IMAP angelegte Ordner nicht selbst; die Integration erledigt das beim Anlegen und Umbenennen.

## Fehlersuche und Diagnose

- **401/403 beim Verbinden:** Mit dem falschen Home-Assistant-Benutzer angemeldet oder Token abgelaufen/widerrufen. Nur der bei der
  Einrichtung gewählte Benutzer wird zugelassen. Fehlgeschlagene Anmeldungen zählen für die IP-Sperre von Home Assistant.
- **Tool fehlt im Client:** im Optionsdialog eingeschaltet? Nach Updates neue Tools einschalten und speichern.
- **`sending_disabled`:** Allowlist leer. **`recipient_not_allowed`:** Empfänger nicht auf der Allowlist (die Meldung nennt nur Domains).
- **`permanent_delete_disabled`:** Option „Endgültiges Löschen erlauben“ ist aus.
- **Diagnose:** *Geräte & Dienste > GMX Mail MCP > ⋮ > Diagnose herunterladen*. Enthält Version, Server, Verbindungszustand,
  Optionen (Allowlist nur als Anzahl und Domains), Zähler und die letzten 20 Audit-Einträge – **ohne** Passwort, Kontoadresse,
  Benutzer-ID, Betreffs oder Inhalte. Vor dem Teilen trotzdem kurz durchsehen (Ordnernamen können vorkommen).

## Entwicklung

Projektunterlagen (deutsch): `CLAUDE.md`, `SPEC.md`, `docs/DECISIONS.md`, `GMX-NOTES.md`, Berichte `docs/M0-SPIKE.md` bis `docs/M6.md`,
Änderungen in `CHANGELOG.md`. Die HA-freie IMAP/SMTP-Schicht stammt aus [`gbasedow/gmx_mail`](https://github.com/gbasedow/gmx_mail).

```
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_dev.txt
.venv/bin/python -m pytest -q tests/client tests/mcp tests/ha
.venv/bin/ruff check . && .venv/bin/mypy custom_components/gmx_mail_mcp
```

Lizenz: MIT.
