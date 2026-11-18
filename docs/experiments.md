# Experimentelle Datenerfassung und Trainingsdaten

`Start-Training.cmd` startet die separate PyQt6-GUI im bestehenden AutoTune-Repo.
`Start.cmd` bleibt die normale AutoTune-Anwendung. Das Repository
`BiBaZu_AutoTune_Training` wird noch nicht verwendet.

```powershell
./Start-Training.cmd
./Start-Training.cmd --demo
# Alternativ nach erneutem Setup / Installation:
uv run bibazu-autotune-training
```

Nach dem Update einmal `Setup.ps1` ausführen; OpenCV wird nun auch für die
Videoauswertung der Trainings-GUI benötigt. Der normale Start
verwendet dieselbe exklusive SPS-API und denselben Prozess-Lock wie AutoTune.
AutoTune, Pressure Control und Drucktest vorher schließen. Öffnen und Verbinden
starten weder das Band noch einen Versuch. Bei Sitzungsende oder Verbindungsfehler
greift der vorhandene SPS-Dienst mit seinem Bandstopp ein.

CLI-Optionen: `--pressure-url`, `--vision-url`, `--plc-ip`, `--ams-net-id`,
`--ads-port`, `--output-dir`, `--demo`. Die Simulation verbindet keine Hardware
und benötigt keine laufende Kamera-GUI; ihre Kampagnen sind als simuliert markiert.
Eine Demo-Kamera wird für eine reale SPS-Messkampagne abgelehnt.

## 1. Bauteil, Kandidat und Raster vorbereiten

Über **Standardordner …** neben den Ladebuttons lassen sich getrennte Startordner
für Roadmap und STL festlegen. Sie bleiben für den nächsten Programmstart
gespeichert und gelten auch für **STL zuordnen**. Leere Felder setzen die jeweilige
Vorgabe zurück. Ist ein gespeicherter Ordner nicht erreichbar, verwendet die
Dateiauswahl den bisherigen Standardordner.

1. Unter **Versuche aufnehmen → Bauteil und Raster** eine Roadmap auswählen,
   oder eine STL laden und im bestehenden Pose-Dialog Ausgangs-/Zielpose festlegen.
   Eine fehlende Roadmap-STL kann über **STL zuordnen** ergänzt werden.
2. Unter **Modell und Geometrie** Dichte, Montage und Modellparameter prüfen.
   Die gespeicherten AutoTune-Modellstandards stehen auch hier zur Verfügung.
3. Unter **Ausgangswerte (lokal)** Bandgeschwindigkeit, Impulsdauer und
   Verzögerung festlegen. Diese Eingaben übertragen keine SPS-Werte.
4. **Berechnung durchführen**, Kandidaten auswählen und **Kandidat als realen
   Aufbau festlegen** bedienen. Array, aktive Düsen und Offset bleiben während
   der Kampagne fest; andere Arrays sind im Versuch deaktiviert. Änderungen an
   der Vorbereitung erfordern eine erneute Kandidatenwahl.
5. Auf derselben ersten Seite Druck und Impulsdauer jeweils mit
   Minimum, Maximum und Schrittweite festlegen. Ein einzelner Wert ist ebenfalls
   möglich. Der Endwert muss mit der Schrittweite erreichbar sein.

Neben der Bauteil- und Kandidatenauswahl liegen Raster, Wiederholungszahl und
Ablaufvorschau. Nach **Kandidat als realen Aufbau festlegen** zeigt die Tabelle
für jede Impulsdauer des Rasters den **analytischen Überdruck [mbar]** und den
**skalierten SPS-Sollwert [mbar]**. Die Tabelle aktualisiert sich beim Ändern des
Impulsrasters. So lässt sich der SPS-Druckbereich direkt anhand der Sollwerte
festlegen. Modellwarnungen und Werte außerhalb der SPS-Grenzen stehen in der
Spalte **Hinweise**. Bei Änderungen an Bauteil oder Modell werden alte Werte
ausgeblendet, bis erneut ein berechneter Kandidat festgelegt wurde.

Die Reihenfolge ist schlangenförmig: Druck aufsteigend bei erster Impulsdauer,
danach die nächste Impulsdauer und Druck absteigend. Zwischen zwei Kombinationen
ändert sich nur eine der beiden Versuchsgrößen. Die Vorschau zeigt die Anzahl
der Kombinationen, Wiederholungen und Versuche.

Vorgaben: fünf Wiederholungen, 100 mbar Druckschritt, 10 ms Dauerschritt,
fünf Sekunden Einschwingzeit, 60 Sekunden Aufnahmezeitlimit. Alle sind einstellbar.
Ein noch mit 0/0 belegter Druckbereich wird bei Kandidatenübernahme mit dessen
SPS-Sollwert vorbelegt. Bereits eingestellte Druck- und Impulsbereiche bleiben
erhalten. Zulässig sind 0–6000 mbar und 1–500 ms. Der Druck ist der SPS-Sollwert,
kein sensorisch gemessener Luftdruck.

## 2. Kamera einrichten und aufnehmen

Die bestehende Baumer-Kamera-GUI starten und die echte Kamera verbinden. Dort
Messbereich, Hintergrund, Rutschenreferenz und Nullpunkt für das Bauteil in der
Ausgangslage einrichten. Die Aufnahme muss zunächst beendet und entschärft sein.
Die API läuft normalerweise unter `http://127.0.0.1:8766`.

Den Messbereich so wählen, dass Ausgangslage, vollständige Flipbewegung und eine
stabile Endlage sichtbar bleiben. Die Kamera misst eine 2D-Achse modulo 180°.
Die GUI-Einstellung **Kameradrehsinn** beschreibt, welches Vorzeichen der
Kameradrehung in die Zielrichtung führt; dies kann vom räumlichen STL-Drehsinn
abweichen. Erst nach Prüfung die Beobachtbarkeit bestätigen. Ohne diese
Bestätigung werden Aufnahmen gespeichert und zur Prüfung markiert.

1. **SPS und Kamera verbinden**. Aktuelle Werte werden gelesen; es entsteht
   noch kein Kampagnenordner.
2. Den realen Aufbau und das Raster bestätigen, den Ausgabeordner wählen und
   **Kampagne anlegen**. Die Versuchsarrays werden deaktiviert.
3. Unter **Durchführung → Reihe starten** die erste Kombination freigeben.
   Dies ist auch vor jeder weiteren Kombination erforderlich. Die Leertaste
   funktioniert auf der Durchführungsseite; Texteingaben und Tastenwiederholungen
   geben keine zusätzliche Reihe frei.
4. Die Parameter werden übertragen, zurückgelesen und die eingestellte
   Einschwingzeit abgewartet. Danach zeigt **BEREIT** die scharfe Kamera an.
5. Das Band ausdrücklich über **Band starten** bedienen und das Bauteil in
   Ausgangslage zuführen. Nach vollständiger Speicherung wird die nächste
   Aufnahme derselben Kombination automatisch vorbereitet. Erst bei **BEREIT**
   das nächste Bauteil zuführen. Die manuelle Rückführung richtet das Bauteil
   jedes Mal wieder in der Ausgangspose aus.

Neue Kampagnen verwenden unter **Kamera und Kampagne → Durchlauferkennung**
standardmäßig **Bauteil im Bild · ohne Hardwaretrigger**. Die Kamera zeichnet
damit auch Durchläufe ohne Drehbewegung auf: bestätigtes Bauteil startet die Aufnahme,
ein wieder freier Messbereich beendet sie nach der Ruhezeit. Die aktuelle
Kamera-GUI muss diese API-Erweiterung unterstützen und nach einem Update neu
gestartet werden. Nach jedem Versuch wird ihre vorherige Aufnahmekonfiguration
wiederhergestellt. Der Messbereich muss zwischen zwei Bauteilen frei sein.

**Bauteilbestätigung** verlangt bei neuen Kampagnen standardmäßig 100 ms
(einstellbar 50–500 ms) mit mindestens drei aufeinanderfolgenden, gültigen Bildern:
vollständig sichtbare Kontur und Bounding Box, gültiger Winkel, Qualitätswert
mindestens 0,2, höchstens 5° Winkelspanne sowie Konturfläche und Boxseiten innerhalb
±25 % des ersten Kandidatenbilds. Die Position darf sich mit dem Band bewegen.
Ungültige Bilder oder Zeitlücken über 50 ms setzen die Bestätigung zurück.
Kurze Handeingriffe lassen den Versuchsplatz scharf und erzeugen keinen Clip.
Der Vorlaufpuffer umfasst mindestens Bestätigungsdauer plus 50 ms, damit die
Ausgangspose erhalten bleibt. Eine länger passende sichtbare Hand wird dadurch
nicht sicher als Hand erkannt; es gibt keinen STL-Bildabgleich.

Alternativ gilt **Startmodus der Kamera-GUI verwenden**. Bewegungs- oder
Hardwaretrigger werden dann in der Kamera-GUI eingestellt. Im
manuellen Kameramodus ist zusätzlich **Manueller Kameratrigger** verfügbar.
Der Kamera-/Messstand wird für die Kampagne festgehalten; Änderungen erfordern
eine neue Kampagne. Livewerte dienen der Anzeige, die Klassifizierung verwendet
die vollständige gespeicherte Aufnahme.

Zwischen Kombinationen und bei Fehlerpausen sind die Arrays aus. Das Band bleibt
im normalen Ablauf separat bedienbar, auch am Kampagnenende. **Pause / Aufnahme
abbrechen** beendet die laufende Aufnahme und erhält ihre Daten. **Band stoppen**
fordert über eine separate HTTP-Verbindung einen priorisierten SPS-Stopp an und
pausiert die Versuche. Fenster schließen beendet Aufnahme und Sitzung.

## 3. Klassen und Messqualität

Auf der **Durchführungsseite** stehen Ausgangs- und Zielpose direkt über den
Versuchsaktionen. Bei laufenden und wiederaufgenommenen Kampagnen werden dafür
die archivierte STL und die gespeicherten Kampagnenposen verwendet.

**Kampagne abbrechen / löschen** pausiert zuerst den Versuch. Im Dialog kann man
die Kampagne mit erhaltenen Daten abbrechen, nach Abschluss der Aufnahme und
Archivierung ihren gesamten Kampagnenordner löschen oder nur pausieren.
Beim endgültigen Abbruch wird die Sitzung beendet und der SPS-Bandstopp angefordert.
Der Dialog nennt den genauen Datenordner; die Vorgabe erhält die Daten.
Originalaufnahmen im Kamera-Repo und andere Kampagnen werden nicht gelöscht.
Gelöscht werden nur Kampagnenordner, deren Name ihrer gespeicherten UUID entspricht;
Verknüpfungen innerhalb eines Ordners verhindern das Löschen.

Die Klassifizierung orientiert sich an der erreichten Enddrehung, nicht am
größten Zwischenwinkel. Bei zunächst ±10° Toleranz gilt:

| Ergebnis | Bedeutung |
| --- | --- |
| `not_flipped` | Enddrehung unter dem Zielband, auch nach Zurückfallen |
| `flipped` | Enddrehung innerhalb des Zielbands einschließlich Grenzen |
| `overflipped` | Enddrehung oberhalb des Zielbands |
| Zu prüfen | Keine ausreichend sichere automatische Entscheidung |

Ausgangs- und Endwinkel stammen aus stabilen Abschnitten mit mindestens drei
Messpunkten über mindestens 50 ms und höchstens 5° Winkelspanne. Diese Werte und
die maximal zulässige Zeitlücke (anfangs 100 ms) sind einstellbar.
Zeitliche Kontinuität führt die Rohwinkel über ±90° hinaus fort. Fehlende Frames,
ungültige Messungen und uneindeutige 90°-Schritte unterbrechen diese Fortführung.
Die Bewegung muss vollständig und mit ausreichender Bildrate beobachtet werden;
eine zwischen Bildern versteckte große Drehung kann aus der Achsmessung allein
nicht ausgeschlossen werden. Das Verfahren bestätigt keine beliebige 3D-Pose.

Neue Kampagnen verwenden Auswertungsversion 2. Kurze innere Lücken werden nur in
bestätigten Ruhephasen toleriert: höchstens 20 ms (und nicht über der eingestellten
Zeitlückengrenze), höchstens zwei fehlende Bilder, davor und danach jeweils
mindestens 50 ms lückenlose stabile Winkel und zusammen höchstens 5° Winkelspanne.
Fehlende Winkel werden nicht interpoliert. Lücken während einer Drehung, größere
Lücken und Zeitstempelrücksprünge bleiben prüfpflichtig. Ungültiger Vor-/Nachlauf
außerhalb stabiler Messabschnitte wird nicht als unterbrochene Drehung gewertet;
gültige, noch bewegte Randabschnitte werden nicht weggeschnitten.

Ein Abbruch mit `memory_limit` kann trotz unverändertem Kamera-Fehlerstatus
automatisch verwendbar sein: stabile Ausgangspose, durchgehend beobachtete
Winkelbewegung über 5° und anschließend mindestens 200 ms lückenlose stabile
Endpose bis zum letzten gespeicherten Bild. Höchstens das eine beim Abbruch
verworfene Schlussbild darf fehlen. Flache abgeschnittene Messungen, andere
Abbrüche, externe Stopps und Schreibfehler bleiben prüfpflichtig.
Blockierende Gründe und tolerierte Hinweise werden getrennt gespeichert und in
der Prüfung angezeigt; Exportfelder sind `quality_reasons`, `quality_warnings`
und `evaluation_version`.

Bestehende Kampagnen ohne Versionsangabe behalten Version 1 und den bisherigen
Aufnahmestart. Sie werden nicht automatisch neu ausgewertet. Originalmessungen,
Kamera-Abbruchgründe und manuelle Gut-/Schlecht-Entscheidungen bleiben erhalten.

Unsichere Winkelauswertung, fehlende stabile Abschnitte und unvollständige
Kameraaufnahmen werden mit Grund gespeichert und zur Wiederholung vorgemerkt.
**Die laufende Versuchsreihe wird dadurch nicht pausiert:** Sie führt zunächst
alle eingestellten Wiederholungen ihrer Druck-/Impulsdauer-Kombination aus.

Unter **Kamera und Kampagne → Unsichere Versuche wiederholen** wählt man:

- **Nach jeder Versuchsreihe anbieten** (Vorgabe): Nach der Reihe können deren
  unsichere Versuche über **Unsichere Versuche wiederholen** nachgeholt werden.
- **Am Kampagnenende gesammelt anbieten**: Alle regulären Reihen werden zuerst
  abgearbeitet; danach werden die offenen unsicheren Versuche gemeinsam angeboten.

**Vorerst ohne Wiederholung weiter** verschiebt die Wiederholung. Am Ende bleibt
der Wiederholungsbutton verfügbar. Jede Wiederholungsrunde arbeitet die zu Beginn
vorgemerkten Versuche genau einmal ab. Erneut unsichere Ergebnisse bleiben offen
und können anschließend nochmals wiederholt werden; es gibt keine automatische
Endlosschleife. Beim Wechsel zu einer anderen Druck-/Impulsdauer-Kombination
bleibt die ausdrückliche Reihenfreigabe erforderlich. Offene Aufgaben und eine
begonnene Wiederholungsrunde werden für die Wiederaufnahme gespeichert.

Wiederholungen bekommen neue Kennungen und verweisen über `replaces_trial_id`
auf die ursprüngliche Aufnahme; Rohdaten und alte Bewertungen bleiben erhalten.
Eine manuelle Pause oder ein Verbindungs-, SPS- oder Speicherfehler unterbricht
weiterhin den Ablauf. Nach einer manuellen Pause startet **Versuch wiederholen**
den betroffenen Einzelversuch erneut; **Ohne Ersatz fortsetzen** markiert ihn als
übersprungen.
Nach Verbindungsverlust erneut verbinden und **Kampagne wiederaufnehmen** wählen;
ohne erneute Reihenfreigabe beginnt keine Aufnahme.

## 4. Speicherung und Datenprüfung

Neue Versuche verwenden standardmäßig **kompakte Prüfvideos**. Die Kamera schreibt
genau ein MP4 (`mp4v`) direkt in den reservierten Versuchsordner. Es gibt weder
unkomprimierte NPY-Einzelbilder noch eine zweite Kopie im Kamera-Repo. Der
Messbereich wird für das Video auf höchstens 960 Pixel an der längsten Seite
verkleinert; alle verarbeiteten Bilder bleiben enthalten. Winkelberechnung,
Klassifizierung und Export verwenden die ursprünglichen Messwerte und Zeitstempel,
nicht die komprimierten Videobilder. Die Video-Bildrate dient nur der Wiedergabe;
die genaue Zuordnung erfolgt über Zeitstempel und Frameindex im Manifest.

Die Videoaufzeichnung läuft bereits während des Durchlaufs im Hintergrund.
Geschriebene Bilder werden sofort aus dem begrenzten RAM-Puffer entfernt.
Encoderfehler werden separat gemeldet; Messwerte werden weiter protokolliert.
Es erfolgt kein automatischer Rückfall auf große Rohbilddateien. Die Anzeige zeigt
freien Speicher und eine ungefähre Kampagnengröße. Unter 1 GiB freiem Speicher
wird kein weiterer Versuch gestartet; es werden keine Daten automatisch gelöscht.

Standard: `data/experiments/<campaign_id>/`. Ein Kampagnenordner kann vollständig
verschoben werden; interne Referenzen bleiben relativ.

```text
campaign.json                Versionierter Plan, Metadaten, SI-Geometrie, Modellstand
progress.json                Anzeigezustand; abgeschlossene Versuche sind maßgeblich
assets/                      Kopien von STL, Roadmap und vorhandenem Consumer-Sidecar
trials/<trial_id>/
  trial.json                 Parameter, Rücklesen, Analyse, Status und Klassifizierung
  measurements.csv           Rohwinkel, fortgeführte Winkel, Qualität und Lückengründe
  capture-request.json       Reserviertes Aufnahmeziel mit Kampagnen-/Versuchskennung
  clip/manifest.json         Kameraformat v2: Zeitstempel, Messwerte und Videometadaten
  clip/review.mp4            Kompaktes Prüfvideo, nach Prüfung separat löschbar
  clip/measurements.jsonl    Fortlaufend geschriebenes Messjournal zur Wiederherstellung
  clip/capture.json          Metadaten für eine unterbrochene Aufnahme
  annotations.json           Versionierte manuelle Prüfentscheidungen
```

`campaign.json` enthält `schema_version=1`, Posen, STL-Hash und Importeinheit,
Material, Geometrie, Montage, Modellparameter, Kandidaten und analytische
Druckvorhersagen für jede Impulsdauer. Analytischer Druck und skalierter
SPS-Sollwert sind getrennt. Jeder Versuch enthält angeforderte und zurückgelesene
Parameter. Kamerazeiten sind monotone Hostzeiten und werden nicht als
synchronisierte SPS-Zeitbasis ausgegeben.

Speicherung und Abschluss laufen außerhalb des Qt-Threads. Metadaten werden
atomar veröffentlicht; unvollständige Versuche und temporäre Aufnahmeordner
werden nicht als abgeschlossen gewertet. Eine Wiederaufnahme prüft die
archivierten Assets und die Kamera-Konfiguration. Bereits abgeschlossene oder
bewusst übersprungene Versuchsplätze werden nicht erneut ausgeführt.

Unter **Daten prüfen** eine Kampagne öffnen oder die aktuelle Kampagne aus der
Durchführung übernehmen. Die Tabelle lässt sich nach Klassen, Prüfbedarf oder
Ausschluss filtern. Winkelkurve und Prüfvideo sind über den Frame-Regler
gemeinsam durchsuchbar. Eine Korrektur oder ein Ausschluss benötigt eine
Begründung. Wiedereinschließen ist ebenfalls möglich. Ursprüngliche Messwerte
und automatische Klassen bleiben unverändert.

Rechts in jeder Versuchszeile stehen **Gut** und **Schlecht** für die Datenqualität.
**Gut** gibt die Daten nach Sichtprüfung frei; fehlt eine Flipklasse, muss diese
im Auswahldialog angegeben werden. **Schlecht** schließt den Versuch vom Training
aus. Eine spätere Freigabe ist mit **Gut** möglich. Beide Aktionen speichern eine
Prüfentscheidung mit Begründung; Originalmesswerte und automatische Auswertung
bleiben erhalten. **Gut** bedeutet verwendbare Daten und kann auch die Flipklasse
`not_flipped` haben. Die Spalte **Grund** erläutert die automatische Unsicherheit
und zeigt etwa bei `memory_limit`, dass der Aufnahmepuffer voll war.

Wiederholtes Bestätigen derselben Bewertung mit unveränderter Klasse und
Begründung erzeugt keinen zusätzlichen Historieneintrag. Beim Wechsel zwischen
**Gut** und **Schlecht** bleibt nur der aktuelle Standardtext in der Begründung;
eigene Notizen und die Historie früherer Entscheidungen bleiben erhalten.
Bereits angehäufte Standardtexte alter Prüfungen werden in Anzeige und Export
zusammengefasst.

**Alle automatisch gültigen bestätigen** bestätigt alle noch ungeprüften Versuche
mit gültiger automatischer Auswertung und Flipklasse in der geöffneten Kampagne.
Der Tabellenfilter begrenzt diese Sammelaktion nicht. Bereits manuell korrigierte,
bestätigte oder ausgeschlossene Versuche werden übersprungen. Wiederholtes Klicken
erzeugt keine doppelten Bestätigungen; Originalmesswerte und automatische
Auswertungen bleiben unverändert.

Gespeicherte Entscheidungen **Gut** oder **Schlecht** nehmen den jeweiligen
unsicheren Versuch aus der Wiederholungsliste. Anzahl und Freigabestatus in der
Durchführung werden direkt aktualisiert, auch bei bereits bestehenden Kampagnen.
Eine Prüfung mit Klasse **Zu prüfen** ohne Ausschluss bleibt offen. Bereits
vorgemerkte Wiederholungen werden vor dem Start erneut geprüft und beim
Wiederaufnehmen einer Kampagne bereinigt. Ein laufender Versuch wird durch die
Prüfung nicht unterbrochen. Eine neue Druck-/Impulsdauer-Kombination benötigt
weiterhin ihre eigene Freigabe.

Nach einer gespeicherten Prüfung kann **Prüfvideo löschen** das ausgewählte Video
entfernen. **Geprüfte Videos dieser Kampagne löschen** entfernt die Videos aller
bereits geprüften Versuche. Die Bestätigung zeigt Anzahl, Speicherbedarf und
Kampagnenordner. Messwerte, Klassifizierung, Geometrie, Prüfentscheidungen und
Trainingsdaten bleiben erhalten; Winkelkurven und CSV-Export funktionieren weiter.
Eine fehlgeschlagene Löschung wird angezeigt und kann erneut versucht werden.

Vorhandene Aufnahmen im Kameraformat v1 mit NPY-Einzelbildern bleiben lesbar und
werden weder konvertiert noch durch die Video-Löschbuttons entfernt. Neue
Kampagnen benötigen die aktualisierte Kamera-GUI mit den Fähigkeiten
`review_video_v1` und `direct_trial_output`. Beide Programme nach dem Update neu
starten. Bei einem unterbrochenen Schreibvorgang wird beim Wiederaufnehmen der
Kampagne das Messjournal wiederhergestellt und der Versuch als abgebrochen
markiert. Kurzzeitige Windows-Dateisperren beim Abschluss werden erneut versucht;
bei anhaltender Sperre bleibt die Aufnahme im lesbaren Ordner `clip.tmp` erhalten.

**CSV exportieren** erzeugt eine Zeile pro Versuch, einschließlich
Ausschluss-/Prüfstatus. `input_*` bezeichnet Versuchsparameter, `feature_*`
geometrische und Modellmerkmale, `outcome_*` gemessene Ergebnisse. Kennungen und
Gruppenschlüssel verbinden Export und Originaldaten. Ein fehlender Messwert
wird nicht durch eine Nullmessung ersetzt.

## 5. Modelltraining vorbereiten

Die GUI bietet bereits die Auswahl Neural Network, Gaussian Process und Random
Forest, jeweils als Klassifikator oder Regressor, sowie das Einlesen der geprüften
Daten. `DatasetReader` liefert nicht ausgeschlossene Versuche; für Klassifikation
werden gültige automatische oder ausdrücklich manuell geprüfte Klassen verwendet.

Das eigentliche Training ist noch nicht implementiert. Geplant sind die drei
Flipklassen sowie geeigneter Druck bzw. Druckkorrektur bei vorgegebener
Impulsdauer. Die Zielwertbildung aus mehreren Erfolgs-/Fehlversuchen erfolgt im
späteren Trainingsschritt; ein einzelner erfolgreicher Versuch wird nicht bereits
als optimaler Druck deklariert. Bei inverser Druckvorhersage darf der gemessene
Versuchsdruck nicht als Eingangsmerkmal verwendet werden. Ergebnisse und Klassen
sind ebenfalls keine Eingangsmerkmale.

Spätere Trainings-/Testaufteilungen verwenden Bauteil-, Transition- und
Kampagnengruppen, damit Wiederholungen nicht als unabhängige Testbeispiele
erscheinen. Rohdaten und alle Einzelversuche bleiben für unterschiedliche
Merkmalssätze und Zielwertregeln verfügbar.

## Prüfung

```powershell
./.venv/Scripts/python.exe -m pytest -q tests/test_experiments.py tests/test_experiments_gui.py
./.venv/Scripts/python.exe -m pytest -q tests/test_experiments_faults.py
./.venv/Scripts/python.exe -m pytest -q tests/test_compact_experiments.py
./.venv/Scripts/python.exe -m ruff check src/bibazu_autotune/experiments
```

Die Tests verwenden Simulationen und berühren keine reale Anlage. Vor der ersten
Messkampagne ein kleines Raster mit mehreren Wiederholungen an der Anlage
aufnehmen und die Klassen anhand der Prüfvideos prüfen.
