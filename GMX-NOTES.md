# GMX-NOTES.md – GMX-Anforderungen und -Eigenheiten

Stand der Recherche: 20.11.2026. Quellen: siehe `docs/SOURCES.md`.

Verlässlichkeit: **O** = offiziell (GMX-Hilfe/GMX-Seite), **D** = Drittquelle (Forum, Issue, Log, Presse), **U** = unbestätigt.
**LIVE** = nur durch Test gegen ein echtes GMX-Konto klärbar. Spalte „Ergebnis“ nach M0 ausfüllen.

GMX bietet nur IMAP und SMTP. Eine dokumentierte Mail-API, ManageSieve oder IMAP-Zugriff auf die Webmail-Filterregeln
wurde nicht gefunden. „Vollständiges Management“ bedeutet hier: alles, was IMAP und SMTP hergeben.

## 1. Anforderungen und Eigenheiten

| # | Thema | Befund | Quelle | Verl. | LIVE | Ergebnis |
|---|---|---|---|---|---|---|
| 1 | IMAP | `imap.gmx.net:993`, implizites TLS; Fallback Port 143 mit STARTTLS (sonst unverschlüsselt) | hilfe.gmx.net/pop-imap/imap/imap-serverdaten.html | O | – | ✔ 993/TLS funktioniert (Zertifikat CN `mail.gmx.net`, passt zu `imap.gmx.net`; Kette Telekom Security → T-TeleSec GlobalRoot Class 3, siehe Abschnitt 4). Port 143 nicht getestet. |
| 2 | SMTP | `mail.gmx.net:587` STARTTLS; alternativ 465 SSL | dto. | O | – | ✔ 587: EHLO vor TLS nur `8BITMIME SIZE STARTTLS`, nach TLS `8BITMIME AUTH SIZE`; Login ok. 465 nicht getestet. |
| 3 | TLS | nur TLS 1.2/1.3; 1.0/1.1 nicht mehr unterstützt | dto. | O | Zertifikat/CN prüfen | ✔ TLS 1.3, `TLS_AES_256_GCM_SHA384`. |
| 4 | Login | Benutzername = volle E-Mail-Adresse (auch bei @gmx.de) | GMX-Hilfe (Outlook/Live Mail), Drittquellen | O/D | – | ✔ Login mit voller Adresse und App-Passwort ok (0,4 s). |
| 5 | Aktivierung | POP3/IMAP ab Werk aus. Einschalten: Initialen > E-Mail-Einstellungen > E-Mail empfangen > POP3/IMAP > „POP3- und IMAP-Zugriff erlauben“ | hilfe.gmx.net/pop-imap/einschalten.html | O | – | ✔ (IMAP war freigeschaltet). |
| 6 | Auto-Abschaltung | „Wenn Sie das E-Mail-Programm längere Zeit nicht verwenden, wird der Abruf über POP3 bzw. IMAP … automatisch ausgeschaltet“. Keine Frist genannt. Wiedereinschalten: „die zwei angezeigten Symbole übereinander schieben“ (interaktiv, nicht automatisierbar). Symptom beim Client: „Kennwort falsch“. Forum: Abschaltung „alle 4 Wochen“ trotz täglichem Abruf; GMX-Mail „Abschaltung POP/IMAP zum 09.01.2024“ mit Hinweis, man müsse in der GMX Mail App angemeldet sein. Ob ein Dauer-IDLE als Nutzung zählt, ist zweifelhaft. | hilfe.gmx.net/pop-imap/kein-versand-mailprogramm.html; heise; thunderbird-mail.de Thread 93637 | O/D | Frist unbekannt | Offen: Frist weiter unbekannt; Langzeitbeobachtung nötig. |
| 7 | App-Passwort | Account verwalten > Login & Sicherheit > App-Passwörter (für externe Apps). Einmalige Anzeige, löschbar, neu erstellbar. Gilt auch für CalDAV/CardDAV/WebDAV (Scope nicht nur Mail). | hilfe.gmx.net/sicherheit/2fa/anwendungsspezifisches-passwort.html; gmx.net/mail/sicherheit/zwei-faktor-authentifizierung/ | O | Anzahl, Fehlertext | ✔ App-Passwort „probe“ funktioniert für IMAP und SMTP. Maximalzahl/Fehlertext nicht getestet. |
| 8 | 2FA | Mit aktiver 2FA ist für POP3/IMAP nur ein App-Passwort möglich | gmx.net 2FA-Seite | O | – | |
| 9 | Länder | Zugriff außerhalb Europas/USA/Kanada nur mit App-Passwort | hilfe.gmx.net/pop-imap/kein-versand-mailprogramm.html | O | – | |
| 10 | CAPABILITY (vor Login) | `IMAP4rev1 CHILDREN ENABLE ID IDLE LIST-EXTENDED LIST-STATUS LITERAL- MOVE NAMESPACE SASL-IR SORT SPECIAL-USE THREAD=ORDEREDSUBJECT UIDPLUS UNSELECT WITHIN` plus `STARTTLS LOGINDISABLED` (143) bzw. `AUTH=LOGIN AUTH=PLAIN` (TLS) | eM-Client-Forum 11/2022; mailcow Issue #5778 (03/2024); Pegasus-Mail-Forum 12/2025 (nur Snippet) | D | Liste nach Login | ✔ bestätigt, identisch (vor Login `AUTH=LOGIN AUTH=PLAIN`, `LOGINDISABLED`-Frage bei 143 offen). |
| 11 | Fehlende Capabilities | kein CONDSTORE/QRESYNC, QUOTA, ESEARCH, UTF8=ACCEPT, COMPRESS, THREAD=REFERENCES, X-GM-EXT | dto. (Abwesenheit im Banner) | D | ja | ✔ Nach Login: `APPENDLIMIT=29360128 CHILDREN ENABLE ID IDLE IMAP4REV1 LIST-EXTENDED LIST-STATUS LITERAL- MOVE NAMESPACE SASL-IR SORT SPECIAL-USE THREAD=ORDEREDSUBJECT UIDPLUS UNSELECT WITHIN`. Kein CONDSTORE/QRESYNC/QUOTA/ESEARCH/UTF8=ACCEPT/COMPRESS. **Neu:** `APPENDLIMIT` ≈ 28 MiB (Obergrenze für APPEND/Entwürfe mit Anhang). |
| 12 | Historie | 2009: nur `IDLE UNSELECT LITERAL+`; UIDPLUS funktionierte, wurde aber nicht gemeldet | bugs.kde.org 204653 | D | – | ✔ UIDPLUS wird heute gemeldet; `APPENDUID` und `UID EXPUNGE` funktionieren. |
| 13 | Server-Software | Begrüßung früher „GMX IMAP4 StreamProxy“, heute „IMAP server ready H migmx…“ | KDE-Bug, Foren | D | – | |
| 14 | Ordner (IMAP) | `Gesendet`, `Entwürfe`, `Gelöscht`, `Spamverdacht` (Live-Mail-Anleitung); neuere Hilfe spricht von „Papierkorb“/„Spam“; Thunderbird legte früher zusätzlich `Sent` an. **Quellen widersprechen sich.** | hilfe.gmx.net/pop-imap/imap/livemail.html; hilfe.gmx.net/email/ordner/systemordner.html; heise | O (Widerspruch) | SPECIAL-USE + Trennzeichen | ✔ `Entwürfe` (\Drafts, \NoInferiors), `Gelöscht` (\Trash), `Gesendet` (\Sent, \NoInferiors), `Spamverdacht` (\Junk, \NoInferiors); Konto hat 7 Ordner; Trennzeichen `/`. Die ältere Benennung gilt also weiterhin; SPECIAL-USE wird per `LIST` und `LIST (SPECIAL-USE)` geliefert. |
| 15 | Umlaute in Ordnernamen | `Entwürfe` = modifiziertes UTF-7 `Entw&APw-rfe` (RFC 3501 5.1.3) | RFC 3501; bugzilla.mozilla.org 255541 | D | ja | ✔ Testordner `HA-Probe-ÄA/ÄB` (modifiziertes UTF-7) anlegen, umbenennen, löschen funktioniert. |
| 16 | Papierkorb | Löschen -> Papierkorb; Löschen im Papierkorb = endgültig; Aufbewahrung „standardmäßig unbegrenzt“ (älteres Drittzitat: 1 Tag) | hilfe.gmx.net/email/verwalten-und-suchen/emails-im-papierkorb.html | O | IMAP-Verhalten von `\Deleted` | ✔ `\Deleted` + `UID EXPUNGE` im Testordner funktioniert. `UID MOVE` nach `Gelöscht` funktioniert (Zähler 5 → 6). Expunge dort ungetestet (Suche wegen Indexverzögerung leer, Skript v3c behoben). |
| 17 | Spam | Aufbewahrung 30 Tage; Löschen dort „sofort endgültig“ | GMX-Hilfe „E-Mails nicht finden“, Papierkorb-Seite | O | – | |
| 18 | Ordnerfristen | pro Ordner einstellbar (Posteingang, Papierkorb, Spam, Gesendet, Entwürfe, eigene) | hilfe.gmx.net/email/ordner/aufbewahrungszeit-festlegen.html | O | – | |
| 19 | Anhänge | FreeMail 20 MB, ProMail 50 MB, TopMail 100 MB | hilfe.gmx.net/email/empfangen-und-lesen/dateianhaenge.html | O | SMTP-`SIZE` prüfen | ✔ SMTP `SIZE` 141557760 (≈ 135 MiB, Server-Limit; Kontolimit gilt trotzdem). |
| 20 | Versandlimits | Nicht veröffentlicht (PC-Welt 03/2022: „keine Auskunft“). JAM-Software (SmartSerialMail-FAQ): „dynamisch vom MailSecurity-Team angepasst“, „Maximal 11 Empfänger pro Stunde bei Verwendung einer E-Mail Client Anwendung“ – von GMX nicht bestätigt | pcwelt.de; manuals.jam-software.de | D/U | **nicht testen** (Sperrrisiko) | |
| 21 | Absender | Nur nachweislich eigene Adressen als Absender verwenden | imap-serverdaten.html | O | Alias-Versand | |
| 22 | Gesendet-Kopie | Unklar: Foren berichten fehlende **und** doppelte Kopien | heise; thunderbird-mail.de Thread 66525 | D/U | **Kernfrage** (`--send-self-test`) | ✔ **Lauf 3:** Eine per SMTP an die eigene Adresse gesendete Mail kam im Posteingang an (per Header-Abgleich nach 0,8 s gefunden), aber **0 Kopien in `Gesendet`** nach 64 s (Suche und Abgleich der letzten 10 Header). GMX legt über SMTP **keine** Gesendet-Kopie an → Client muss sie per IMAP `APPEND` mit `\Seen` ablegen. Einmaliger Test mit einer Selbstmail; Verhalten bei fremden Empfängern nicht getestet. |
| 23 | Verbindungslimit | Keine offizielle Zahl. Forum (2019): Standard von 5 Verbindungen funktioniert auch bei GMX | thunderbird-mail.de Thread 83066 | D/U | ja (vorsichtig, max. 4 parallel) | ✔ 4 parallele Verbindungen (Hauptverbindung + 3) gleichzeitig angemeldet. Höhere Zahlen nicht getestet; Entwurf bleibt bei max. 2. |
| 24 | Filterregeln/Sieve | Keine API/ManageSieve gefunden; Webmail-Filter per IMAP nicht sichtbar | keine Quelle | U | Port 4190 prüfen | ✔ Port 4190 auf `imap.gmx.net` und `mail.gmx.net`: Timeout → kein ManageSieve. |
| 25 | SEARCH | Verhalten bei `CHARSET UTF-8`, BODY/TEXT, Groß-/Kleinschreibung, Performance: keine Quelle. Warnbeispiel: Strato-IMAP lieferte seit Mitte 2026 für jede SEARCH ein leeres Ergebnis (imap-mcp-server Issue #138) | – | U | **ja** | ✔ **Geklärt (Lauf 2):** GMX indexiert neue Nachrichten verzögert. In frisch per APPEND angelegten Mails traten Textsuchen erst nach ca. 30 s zuverlässig auf (bei 5 s und 15 s wechselhaft 0/1 Treffer, bei 31 s alle Treffer). Im Bestand (3418 Mails im Posteingang) funktioniert die Serversuche einwandfrei und schnell: FROM 1,1 s, SUBJECT 1,2 s, BODY 0,5 s, TEXT 1,5 s, SINCE 0,04 s. Konsequenz: neue Mails sind kurz nach Eingang per Textsuche evtl. nicht auffindbar → Fallback über FETCH/Header oder Datumssuche. |
| 26 | Flags | 2009: `PERMANENTFLAGS (\Answered \Flagged \Draft \Deleted \Seen)`, also kein `\*` = keine eigenen Keywords | bugs.kde.org 204653 | D (alt) | **ja** | ✔ `PERMANENTFLAGS (\Answered \Flagged \Draft \Deleted \Seen \*)`; eigenes Keyword `haprobe` per STORE gesetzt und per `SEARCH KEYWORD` gefunden. Die Angabe von 2009 ist veraltet. |
| 27 | Quota | Keine QUOTA-Capability -> Speicherfüllstand per IMAP nicht abfragbar | siehe Zeile 11 | D | ja | ✔ bestätigt: keine QUOTA-Capability. |
| 28 | MOVE/COPY | `UID MOVE` und `UID COPY` in Testordner funktionieren. `COPYUID` kommt bei COPY im getaggten OK, bei MOVE **nicht** im getaggten OK, sondern ungetaggt (`* OK [COPYUID …]`). Belastbar belegt nur durch **einen** Messpunkt (Lauf 3, `trash_move`): Der Wert `move_to_b` war wegen eines imaplib-Effekts nicht aussagekräftig (Response-Codes getaggter Antworten landen im selben Speicher und werden nicht geleert; in v3d behoben). | Probe 20.11.2026 | – | ✔ (2 Messpunkte) | COPYUID aus getaggter **oder** ungetaggter Antwort lesen; fehlt beides, Ziel-UID per Message-ID bzw. FETCH-Header-Abgleich ermitteln. **Live-Check 21.11.2026:** MOVE liefert COPYUID (zweiter Messpunkt, mit dem Code der Integration), COPY ebenfalls. |
| 29 | Flags/Datumssuche | `SEEN`, `UNSEEN`, `LARGER`, `SMALLER`, `SINCE`, `KEYWORD` funktionieren; `SORT (REVERSE DATE)` funktioniert. | Probe 20.11.2026 | – | ✔ | |
| 30 | Verbindungsaufbau | Login 0,4 s; TLS-Vertrauen unter Windows mit python.org-Python ohne `certifi` nicht gegeben (Zwischenzertifikate fehlen im Windows-Speicher). | Probe, tls-diagnose | – | ✔ | Integration läuft in HA (certifi vorhanden). |
| 31 | IDLE | IDLE funktioniert (`+ idling`). Nach **1812 s (≈ 30 min)** ohne Aktivität sendet GMX `* BYE timeout` und trennt. | Probe Lauf 2 | – | ✔ | IDLE spätestens alle 20–25 min neu starten; gilt auch für Hauptverbindung ohne Aktivität (NOOP). |
| 32 | Idle-Timeout allgemein | Eine ruhende Verbindung (während des 30-min-IDLE-Tests nicht genutzt) wurde ebenfalls getrennt (`EOF`). | Probe Lauf 2 | – | ✔ | Arbeitsverbindung bei Fehler automatisch neu aufbauen, nie Dauerverbindung voraussetzen. |
| 33 | Teil-FETCH | `BODY.PEEK[1]<0.n>` wird korrekt beantwortet und von imapclient gelesen (anders als GreenMail). | Live-Check 21.11.2026 | – | ✔ | Rückfall auf ganzen Teil bleibt für andere Server. |
| 34 | `\Recent` | Ein Read-Write-SELECT (IDLE-Verbindung der Integration) löscht `\Recent` auch für andere Sitzungen (STATUS RECENT 3 → 0). | Live-Check 21.11.2026 | – | ✔ | Integration nutzt `\Recent` nicht; Nebenwirkung nur für fremde Clients, die neue Mails über `\Recent` erkennen (selten). |
| 35 | IDLE-Push / Entwurf | Push nach APPEND in den Posteingang nach ≈ 10 s; APPEND in `Entwürfe` mit APPENDUID; eigene Keywords per STORE. | Live-Check 21.11.2026 | – | ✔ | 10 s Verzögerung ist GMX-seitig; Coordinator-Debounce (5 s) kommt hinzu. |
| 36 | SEARCH nach Message-ID | `SEARCH HEADER Message-ID <Teil>` fand die ≈ 2 min zuvor per APPEND abgelegte Mail nicht (3 Läufe). Ursache unklar: Header nicht im Suchindex oder Verzögerung > 2 min. | Live-Check 21.11.2026 | – | U | Integration sucht Message-IDs nur per FETCH der Header, nie per SEARCH; das Prüfskript ebenso (seit 21.11.2026). |
| 37 | IDLE-Erneuerung | Mit dem IDLE-Watcher der Integration (Erneuerung nach 24 min per DONE + IDLE) blieb die Push-Verbindung 35 min ohne `BYE` und ohne Neuverbindung bestehen; Push weiter ≈ 10 s. | Live-Check 21.11.2026 (35 min) | – | ✔ | Die Erneuerung verhindert die Trennung aus Zeile 31. Der Neuverbindungsweg nach `BYE` ist nur gegen Dovecot (`doveadm kick`) geprüft. |
| 38 | Endgültig löschen im Papierkorb | MOVE in `Gelöscht`, dann `\Deleted` + `UID EXPUNGE` genau dieser UID: Mail ist danach weg, keine Warnung. | Live-Check 22.11.2026 (M5) | – | ✔ | Löschweg der Integration (`trash`, `delete_permanently`) gegen GMX bestätigt. |
| 39 | Versand mit dem Code der Integration | SMTP über `mail.gmx.net:587` (STARTTLS, Zertifikatsprüfung) an die eigene Adresse: angenommen, nach 3,3 s im Posteingang. Gesendet-Kopie im Modus `auto`: GMX hatte **keine** eigene Kopie → `appended`. | Live-Check 22.11.2026 (M6, `--send-self-test`) | – | ✔ | Bestätigt Zeile 22 mit dem Produktionscode; `sent_copy_mode=append` bleibt Standard. |
| 40 | UID-Bereich mit `*` | `UID SEARCH UID 17:*` lieferte bei höchster UID 2 die Mail mit UID 2 (RFC 3501 6.4.8: `17:*` = `*:17`). Eigenschaft von IMAP, nicht GMX-spezifisch. | Test auf der Nutzerinstanz 22.11.2026 | RFC 3501 | ✔ | Die Integration filtert Treffer unterhalb der Untergrenze heraus (Version 0.9.2, DECISIONS Nr. 78). |
| 41 | Spamfilter bei Testmails | Eine kurze Testmail an die eigene Adresse ohne Absendernamen landete nach mehreren Testmails am selben Tag in `Spamverdacht`. | Test auf der Nutzerinstanz 22.11.2026 | – | U | Einzelbeobachtung; für den Alltag einen Absendernamen setzen. Live-Checks prüfen ggf. auch den Spamordner. |
| 42 | Textsuche mit Satzzeichen | `SEARCH SUBJECT "Re:"` (über `gmx_search`, Feld `subject`) lieferte auch eine Mail, deren Betreff die Zeichenfolge „Re:“ nicht enthält. Vermutung: Der Suchindex zerlegt in Wörter, ignoriert Satzzeichen und findet „re“ auch als Wortteil (z. B. in „Rechnung“, „Ihre“). Der clientseitige Rückfall für frische Mails vergleicht dagegen wörtlich. | Live-Test 24.11.2026 (M2) | – | U | Ursache unbestätigt. Textsuche als unscharf behandeln; für exakte Prüfung Betreff aus den Treffern vergleichen. |
| 43 | FLAGS-Antwort und Schlagwörter | M2: EXAMINE auf den Posteingang ohne gesetzte Schlagwörter → keine gemeldet, `PERMANENTFLAGS` enthält `\*`. **M3-Live-Test:** Nach `STORE +FLAGS ($MCPTest)` meldet GMX `$MCPTest` in `FLAGS` des Ordners; nach dem Verschieben der Mail meldet der Posteingang `$MCPTest`, `$NotJunk`, der verlassene Spamordner und der leere Testordner melden **keine**. `SEARCH KEYWORD $MCPTest` findet die Mail. | Live-Test 24.11.2026 (M2, M3) | – | ✔ | GMX meldet in `FLAGS` die Schlagwörter, die **aktuell an Mails des Ordners** hängen (kein dauerhaftes Verzeichnis). `gmx_list_keywords` zeigt damit „benutzte“ Schlagwörter; ein leerer Ordner meldet keine. |
| 44 | THREAD und deutsche Präfixe | `UID THREAD ORDEREDSUBJECT` lieferte für „AW: X“ und „AW: AW: X“ im selben Ordner zwei getrennte Threads. Passt zu RFC 5256 Abschnitt 2.1: Der Basis-Betreff entfernt nur `Re`, `Fw`, `Fwd` (und `[…]`-Blöcke), nicht `AW:`/`WG:`. | Live-Test 24.11.2026 (M2, Runde 4) | RFC 5256 | U | Ein Messpunkt. Verläufe mit deutschen Mailprogrammen werden zerteilt; Tool-Beschreibung von `gmx_get_thread` muss das nennen (Vorschlag M3). |
| 45 | Abo neuer Ordner | Ein per `CREATE` angelegter Ordner ist danach **nicht** abonniert (`LSUB` ohne ihn); `UNSUBSCRIBE`/`SUBSCRIBE` (nach `RENAME`) wirken sofort; der Abo-Zustand direkt nach `RENAME` wurde nicht abgefragt. | Live-Test 24.11.2026 (M3) | – | ✔ | Ein Messpunkt. Mailprogramme, die nur abonnierte Ordner zeigen, sehen neue Ordner erst nach `gmx_subscribe_folder`. Ob der Webmailer abonnierte Ordner berücksichtigt: nicht geprüft. |
| 46 | `$Junk`/`$NotJunk` | Beide Schlagwörter bleiben über `UID MOVE` erhalten (Testordner → Spamverdacht → Posteingang); `$Junk` wird per `STORE -FLAGS` entfernt; andere Schlagwörter (`$MCPTest`) und `\Seen` bleiben. | Live-Test 24.11.2026 (M3) | RFC 9051 §2.3.2 | ✔ | Bestätigt D33 für die Speicherung. Ob GMX' Filter daraus lernt, bleibt U. Beobachtungsdauer nur Minuten. |
| 47 | UIDVALIDITY | Posteingang, `Spamverdacht` und `Entwürfe` haben denselben UIDVALIDITY-Wert, der neue Testordner einen anderen (größeren). UIDs sind je Ordner getrennt. | Live-Test 24.11.2026 (M3) | – | U | Beobachtung ohne Folgen: Referenzen enthalten immer Ordner **und** UIDVALIDITY. Vermutlich Zeitstempel der Ordneranlage. |
| 48 | Entwurf per APPEND | `APPEND` in `Entwürfe` mit `\Draft` und `\Seen` (setzt die Integration): Referenz geliefert (APPENDUID oder Rückfall über Message-ID – die Antwort unterscheidet das nicht; APPENDUID siehe Zeile 35), Anhang (PNG, base64) unverändert zurücklesbar (SHA-256 gleich); `In-Reply-To` gesetzt; am Original wird kein `\Answered` gesetzt. | Live-Test 24.11.2026 (M3) | – | ✔ | Bestätigt Zeile 35 mit dem Code von `gmx_save_draft`. |

### Widersprüche zwischen Quellen (nicht aufgelöst)

- Ordnernamen: `Gelöscht`/`Spamverdacht` (ältere Hilfe) gegen `Papierkorb`/`Spam` (neuere Hilfe). -> SPECIAL-USE nutzen, nie hart verdrahten.
- Papierkorb-Aufbewahrung: „unbegrenzt“ (offiziell, aktuell) gegen „1 Tag“ (älteres Drittzitat).
- Einführung von `smtp.send_message`: Aufgabenstellung nannte 2026.7, die HA-Aktionsdoku sagt „seit 2026.8“ (UI-Flow für SMTP seit 2026.7).

## 2. Konsequenzen für die Implementierung

1. Login-Fehler = „IMAP evtl. abgeschaltet **oder** Passwort falsch“: gemeinsamer Reauth-Pfad + Repair-Issue `imap_possibly_disabled` mit Anleitung.
2. Ordner über SPECIAL-USE erkennen; Heuristik und Options-Override als Rückfallebenen; Ordnernamen immer in modifiziertem UTF-7 kodieren/dekodieren.
3. SEARCH: Neue Mails sind per Textsuche erst nach ≈ 30 s auffindbar, Bestand wird schnell durchsucht (Zeile 25). Daher: Suchaktion mit Feldern `from`, `to`, `subject`, `since`, `before`, `flags`, `size` implementieren, aber **Selbsttest/Fallback**
   einbauen: liefert die Serversuche 0 Treffer, zusätzlich die jüngsten Nachrichten (Datum der letzten ~2 Minuten) per `FETCH` der Header clientseitig prüfen und
   `search_mode` in der Antwort melden. Kein stilles „keine Treffer“ für sehr frische Mails.
4. Kein QUOTA, kein CONDSTORE: kein Speichersensor, Änderungserkennung über UID/UIDVALIDITY und IDLE.
5. Eigene Keywords sind erlaubt (`\*` vorhanden), trotzdem nur wenn PERMANENTFLAGS `\*` enthält prüfen und sonst abschalten.
5a. `COPYUID` nach MOVE nicht voraussetzen; Ziel-UID bei Bedarf per Message-ID suchen. `APPENDLIMIT` (≈ 28 MiB) für Entwürfe/Anhänge beachten.
6. Gesendet-Kopie: GMX legt per SMTP keine Kopie an (Zeile 22) → Standard `sent_copy_mode: append` (IMAP `APPEND` in den `\Sent`-Ordner mit `\Seen`); Optionen `auto` (nach kurzer Prüfung per Header-Abgleich, nur anhängen wenn fehlend) und `none`.
7. Nie mailbox-weites EXPUNGE; nur UID EXPUNGE mit UIDPLUS.
8. Verbindungen: IDLE alle ≤ 25 min neu starten, ruhende Verbindungen werden nach ≈ 30 min getrennt → automatischer Reconnect.
9. Konservative Versandlimits als Standard (Tageslimit, Empfänger pro Mail), unabhängig von GMX-Angaben.

## 3. Live-Testplan (M0)

Voraussetzungen: separates GMX-Testkonto (bevorzugt) oder eigenes Konto mit Vorsicht; POP3/IMAP freigeschaltet;
eigenes App-Passwort „probe“ (nach dem Test löschen); Python 3.10+ (nur Standardbibliothek).

```
export GMX_USER='testkonto@gmx.de'
export GMX_PASS='<App-Passwort>'
python3 tools/gmx_probe.py > gmx_probe_result.json
python3 tools/gmx_probe.py --send-self-test > gmx_probe_result_send.json   # optional, sendet genau 1 Mail an sich selbst
```

Stand der Prüfung des Skripts (v3d): Hilfsfunktionen (`tests/probe/test_gmx_probe_helpers.py`) und der Ablauf (`tests/probe/test_gmx_probe_flow.py`,
vier Capability-Profile) wurden gegen einen lokalen Fake-IMAP-Server (`tests/probe/fake_imap.py`) mit gestubbtem SMTP geprüft (Kommandosyntax, Parsing, Aufräumen, keine Ausgabe von Zugangsdaten). **Nicht** geprüft ist das
Verhalten gegen den echten GMX-Server; dort können Abweichungen auftreten (z. B. Fehlertexte, Antwortformate).

Das Skript:
- legt nur zwei eigene Testordner an (Name mit Umlaut) und bricht ab, falls sie schon existieren; es verändert keine vorhandenen Mails
  (Ausnahme `--send-self-test`: es löscht die beiden selbst erzeugten Testmails über ihre eindeutige Message-ID);
- gibt **nur Metadaten** aus: Capabilities, Statuscodes, Zähler, Zeiten, Namen der Spezialordner; alle Ordnernamen nur mit `--print-folders`;
- sendet im Standardlauf **keine** Mail (SMTP nur EHLO, STARTTLS und Login);
- druckt nie Passwort, Adressen, Betreffs oder Inhalte.

Manuelle Zusatztests (nicht vom Skript abgedeckt):
1. IDLE länger als 29 Minuten halten (Verbindung bleibt offen?).
2. Mail aus Spam per IMAP `MOVE` in den Papierkorb verschieben; Entwurf per APPEND mit `\Draft` anlegen und im Webmailer prüfen.
3. Port 4190 (ManageSieve) auf `mail.gmx.net` bzw. `imap.gmx.net` ansprechen.
4. Versand von einer Alias-Adresse testen (falls vorhanden).
5. Nach einigen Wochen prüfen, ob die IMAP-Abschaltung eintritt (Datum der letzten Nutzung notieren).

## 4. Ergebnis M0 (Lauf vom 20.11.2026, Probe v2)

- Datei: `docs/probe-2026-11-20.json` (bereinigt: ohne Postfachzähler und UIDVALIDITY). **Hinweis 20.11.2026:** Diese Datei und
  `docs/probe-2026-11-20-idle.json` sind nicht im Repository; eingecheckt ist nur `docs/probe-2026-11-20-v4.json` (Lauf 3).
- Probe v3d (nach Lauf 3): `CLOSE` auf echten Ordnern (Papierkorb, Posteingang, Gesendet) durch `UNSELECT` ersetzt – `CLOSE` hätte dort
  fremde, von anderen Clients mit `\Deleted` markierte Mails endgültig gelöscht. Prüfung offline gegen einen Fake-IMAP-Server
  (`tests/probe/test_gmx_probe_flow.py`). Ob in den Läufen 1–3 dadurch fremde Mails verloren gingen, ist nicht feststellbar
  (nur wenn ein anderer Client zu dem Zeitpunkt Mails als `\Deleted` markiert, aber nicht expunged hatte – bei GMX-Webmailer/App unüblich).
- TLS-Kette: `mail.gmx.net` → Telekom Security OV RSA CA 26A → TLS RSA Root 2023 (cross-signiert von T-TeleSec GlobalRoot Class 3).
  Python mit leerem Windows-Zertifikatsspeicher scheitert; `certifi` löst es. Verifikation wird nie abgeschaltet.
- Alle Kernfunktionen (Ordner, APPEND, MOVE, COPY, Flags, Keywords, UID EXPUNGE, parallele Verbindungen, SMTP-Login) funktionieren.
- Lauf 2 (`docs/probe-2026-11-20-idle.json`): Textsuche = Indexverzögerung ≈ 30 s bei neuen Mails (Zeile 25); IDLE/Verbindung wird nach ≈ 30 min getrennt (Zeile 31).
  Skriptfehler im Papierkorb-Test und im Aufräumen sind behoben (v3b).
- Lauf 3 (`docs/probe-2026-11-20-v4.json`): SMTP legt **keine** Gesendet-Kopie an (Zeile 22); `MOVE` in `Gelöscht` funktioniert (Zeile 16); Aufräumen sauber.
- Live-Check M4 (21.11.2026, `tools/gmx_live_check.py`, Testkonto FreeMail, Python 3.15, Ergebnis `docs/live-check-2026-11-21.json`):
  Teil-FETCH ok (Zeile 33), COPYUID bei MOVE und COPY (Zeile 28), SELECT löscht `\Recent` (Zeile 34), Push ≈ 10 s, Entwurf mit APPENDUID (Zeile 35),
  Aufräumen der Ordner und Entwürfe sauber; die Testmail im Posteingang blieb liegen (Zeile 36, Skriptfehler, behoben).
  Der 35-Minuten-Lauf brach am Ende ab: Die ungenutzte Arbeitsverbindung des Skripts war nach ≈ 30 min getrennt (Zeile 32),
  Aufräumen und Abmelden liefen über die tote Verbindung. Behoben: Arbeitsverbindung wird vor der Wartezeit geschlossen,
  Aufräumen über eine neue Verbindung; `--cleanup-only` entfernt Reste früherer Läufe. Wiederholung am selben Abend:
  `--cleanup-only` entfernte alle Reste (2 Ordner, 3 Mails, 1 Entwurf); der 35-Minuten-Lauf lief durch, ohne Trennung (Zeile 37),
  Aufräumen vollständig (`docs/live-check-2026-11-21-idle.json`).
- Live-Check M5 (22.11.2026, `docs/live-check-2026-11-22-m5.json`): Löschweg bestanden (Zeile 38); alle übrigen Prüfungen wie am Vortag, Aufräumen vollständig.
- Live-Check M6 (22.11.2026, `docs/live-check-2026-11-22-m6.json`): Versand bestanden (Zeile 39); alle übrigen Prüfungen wie zuvor, Aufräumen vollständig.
- Noch offen: Dauer der IMAP-Abschaltung (Langzeitbeobachtung).

### Ergebnisfeld (Vorlage für Folgeläufe)

```
Datum:
Konto-Typ (FreeMail/ProMail/TopMail):
CAPABILITY nach Login:
Spezialordner (Name -> Attribut):
Trennzeichen:
SEARCH UTF-8 Betreff / Body / TEXT:
PERMANENTFLAGS:
Gesendet-Kopie bei SMTP (0/1/2):
Parallele Verbindungen (OK bis):
Sonstiges:
```
