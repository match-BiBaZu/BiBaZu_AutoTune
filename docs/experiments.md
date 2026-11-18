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

Es werden keine zusätzlichen Python-Abhängigkeiten benötigt. Der normale Start
verwendet dieselbe exklusive SPS-API und denselben Prozess-Lock wie AutoTune.
AutoTune, Pressure Control und Drucktest vorher schließen. Öffnen und Verbinden
starten weder das Band noch einen Versuch. Bei Sitzungsende oder Verbindungsfehler
greift der vorhandene SPS-Dienst mit seinem Bandstopp ein.

CLI-Optionen: `--pressure-url`, `--vision-url`, `--plc-ip`, `--ams-net-id`,
`--ads-port`, `--output-dir`, `--demo`. Die Simulation verbindet keine Hardware
und benötigt keine laufende Kamera-GUI; ihre Kampagnen sind als simuliert markiert.
Eine Demo-Kamera wird für eine reale SPS-Messkampagne abgelehnt.

## 1. Bauteil, Kandidat und Raster vorbereiten

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

Bewegungs- oder Hardwaretrigger werden in der Kamera-GUI eingestellt. Im
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

Messfehler, fehlende stabile Abschnitte und abgebrochene Aufnahmen werden mit
Grund gespeichert und pausieren die Reihe. **Versuch wiederholen** erzeugt eine
neue Kennung mit Verweis auf den vorherigen Versuch. **Ohne Ersatz fortsetzen**
markiert den Versuchsplatz als übersprungen. Alle Versuche bleiben erhalten.
Nach Verbindungsverlust erneut verbinden und **Kampagne wiederaufnehmen** wählen;
ohne erneute Reihenfreigabe beginnt keine Aufnahme.

## 4. Speicherung und Datenprüfung

Standard: `data/experiments/<campaign_id>/`. Ein Kampagnenordner kann vollständig
verschoben werden; interne Referenzen bleiben relativ.

```text
campaign.json                Versionierter Plan, Metadaten, SI-Geometrie, Modellstand
progress.json                Anzeigezustand; abgeschlossene Versuche sind maßgeblich
assets/                      Kopien von STL, Roadmap und vorhandenem Consumer-Sidecar
trials/<trial_id>/
  trial.json                 Parameter, Rücklesen, Analyse, Status und Klassifizierung
  measurements.csv           Rohwinkel, fortgeführte Winkel, Qualität und Lückengründe
  clip/manifest.json         Unverändertes Manifest der Kameraaufnahme
  clip/*.npy                 Verlustlose Originalbilder der Kamera
  annotations.json           Versionierte manuelle Prüfentscheidungen
```

`campaign.json` enthält `schema_version=1`, Posen, STL-Hash und Importeinheit,
Material, Geometrie, Montage, Modellparameter, Kandidaten und analytische
Druckvorhersagen für jede Impulsdauer. Analytischer Druck und skalierter
SPS-Sollwert sind getrennt. Jeder Versuch enthält angeforderte und zurückgelesene
Parameter. Kamerazeiten sind monotone Hostzeiten und werden nicht als
synchronisierte SPS-Zeitbasis ausgegeben.

Speicherung und Clipübernahme laufen außerhalb des Qt-Threads. Metadaten werden
atomar veröffentlicht; unvollständige Versuche und temporäre Aufnahmeordner
werden nicht als abgeschlossen gewertet. Eine Wiederaufnahme prüft die
archivierten Assets und die Kamera-Konfiguration. Bereits abgeschlossene oder
bewusst übersprungene Versuchsplätze werden nicht erneut ausgeführt.

Unter **Daten prüfen** eine Kampagne öffnen oder die aktuelle Kampagne aus der
Durchführung übernehmen. Die Tabelle lässt sich nach Klassen, Prüfbedarf oder
Ausschluss filtern. Winkelkurve und Originalbilder sind über den Frame-Regler
gemeinsam durchsuchbar. Eine Korrektur oder ein Ausschluss benötigt eine
Begründung. Wiedereinschließen ist ebenfalls möglich. Ursprüngliche Messwerte
und automatische Klassen bleiben unverändert.

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
./.venv/Scripts/python.exe -m ruff check src/bibazu_autotune/experiments
```

Die Tests verwenden Simulationen und berühren keine reale Anlage. Vor der ersten
Messkampagne ein kleines Raster mit mehreren Wiederholungen an der Anlage
aufnehmen und die Klassen anhand der Originalbilder prüfen.
