# BiBaZu AutoTune

Eigenständige Desktop-Anwendung für manuell freigegebene Impulsparameter-Testreihen.
Roadmap-Auswahl, Pose-Vorschau, Parameterbearbeitung, Bandbedienung, Versuchslogik,
Winkelverlauf und Profildateien liegen in diesem Repository. Die Pressure-GUI und
das Pressure-Repository werden zum Installieren und Ausführen nicht benötigt.
Die Kamera-Anwendung stellt weiterhin ihre Aufnahme- und Messdaten-API bereit.

V1 verwendet vorgegebene Varianten. Sie enthält keinen Optimierungsalgorithmus
und bewertet den Erfolg einer Roadmap-Transition nicht automatisch.

## Installation und Start

Python 3.11+ oder uv installieren, dann in diesem Repository:

```powershell
./Setup.ps1
./Start.cmd
```

Der normale Start betreibt eine eigene, fensterlose PLC-API auf einem freien
Loopback-Port. Die Oberfläche spricht ausschließlich über HTTP mit dieser API;
nur der API-Dienst greift über ADS auf die SPS zu. Start und Verbindungsaufbau
übernehmen keine Maschinen-Standardwerte und starten das Band nicht.

Die Pressure-GUI vorher schließen: Der API-Dienst verwendet denselben Windows-
Prozess-Lock `Local\BiBaZuPlcControl` wie die vorhandenen Maschinenanwendungen.
Er verweigert Schreibzugriffe bei aktiver Reorientation-Control-SPS-Ownership.

Die SPS-Verbindung ist konfigurierbar:

```powershell
./Start.ps1 --plc-ip 192.168.10.23 --ams-net-id 10.145.4.14.1.1 --ads-port 851
./Start.ps1 --vision-url http://127.0.0.1:8766 --output-dir D:\AutoTuneRuns
```

Die angegebenen PLC-Werte sind die Vorgaben der bestehenden Anlage. Eine passende
ADS-Route und die vorhandene SPS-/Tandem-Konfiguration werden vorausgesetzt.
Kalibrierung, Lichtschranken-Einrichtung und Roboterwinkel-Verstellung bleiben
in ihren bisherigen Werkzeugen. AutoTune liest deren Werte nur als Metadaten.

## Manuelle Array- und Bandsteuerung

Eine Roadmap und die Kamera-Anwendung sind für die manuelle Bedienung optional.
Ohne ausgewählte Transition erinnert **Keine Roadmap geladen** im Fenster daran.

1. **SPS verbinden** wählen.
2. Array-Aktivierung, Düsen, Impulsparameter und Bandgeschwindigkeit einstellen.
3. Bei stehendem Band **Parameter übernehmen** wählen. AutoTune schreibt die
   Werte über die API und bestätigt erst nach erfolgreichem Rücklesen.
4. **Band starten** und **Band stoppen** manuell bedienen. Das Band verwendet
   die zuletzt bestätigten SPS-Werte; weitere Editoränderungen müssen zuerst
   mit **Parameter übernehmen** angewendet werden.

Das Schreiben von Parametern startet weder das Band noch eine Kameraaufnahme.
Während das Band fährt oder eine Schreiboperation läuft, ist die Übernahme
gesperrt. Diese Bedienung erzeugt keine Versuchsdateien. Für Kamera-Testreihen
weiterhin eine Transition auswählen und den folgenden Ablauf verwenden.

## Eine Testreihe durchführen

1. Kamera-Anwendung starten, Kamera oder Demo verbinden und ROI, Hintergrund,
   Winkelreferenz sowie Aufnahmestart einstellen. Eine manuell scharfgeschaltete
   Aufnahme zuerst entschärfen.
2. AutoTune starten. **Roadmap auswählen** öffnet JSON-/YAML-Roadmaps mit
   Ausgangs-/Zielposen und direkten oder gruppierten Übergängen. Alternativ ein
   vorhandenes **Profil laden** oder eine **Testreihe laden**. Das ist bereits
   ohne Verbindung möglich und schreibt keine Werte auf die SPS.
3. **SPS verbinden** wählen. AutoTune liest die tatsächlichen SPS-Ausgangswerte;
   lokal ausgewählte Transitionen und geladene Varianten bleiben erhalten. Die
   Kamera-API wird anschließend separat verbunden. Ist sie nicht erreichbar,
   bleibt die SPS verbunden und die Kamera-Zeile zeigt den genauen Fehler.
   Nach dem Start der Kamera-Anwendung **Kamera verbinden** wählen. Versuche
   werden erst freigegeben, wenn auch die Kamera-API verbunden ist.
4. Varianten kopieren und bearbeiten. Vier Arrays enthalten jeweils gemeinsame
   Impulsparameter und sechs einzeln schaltbare Düsen. Bandgeschwindigkeit gilt
   global. Ein Wechsel der Transition beginnt einen neuen Run und leert die
   angezeigten Ergebnisse. Während eines Versuchs ist die Auswahl gesperrt.
5. Band stoppen, Bauteil in Ausgangslage bringen und **Versuch freigeben** wählen.
   Erst nach bestätigtem Schreiben und Rücklesen wird die Kamera scharfgeschaltet.
6. **Band starten** in AutoTune wählen. Während der Versuch vorbereitet wird,
   wartet der Bandstart auf die scharfe Kamera. Im Kamera-Modus **Manuell** zusätzlich die Aufnahme in AutoTune
   auslösen. **Band stoppen** bleibt auch während eines Bandstarts verfügbar
   und bricht dessen laufende Startsequenz ab.
7. Nach dem Ergebnis das Band stoppen und das Bauteil zurücklegen. Jeder Versuch
   benötigt eine neue Freigabe; es gibt keine automatische Versuchsfolge.
8. Ein Ergebnis auswählen und **Ergebnis übernehmen und Profil speichern** wählen.
   AutoTune öffnet seinen eigenen Dateidialog, übernimmt die Parameter nach dessen
   Bestätigung, prüft das Rücklesen und speichert lokal. Abbrechen des Dialogs
   schreibt weder Parameter noch eine Datei. Schlägt das Speichern nach der
   Parameterübernahme fehl, meldet AutoTune diesen Unterschied ausdrücklich.

Ein Aufzeichnungsstatus `completed` mit Endgrund `part_left` bestätigt eine
gespeicherte Durchfahrt, nicht das Erreichen der Zielpose. Die Kamera misst einen
2D-Achswinkel modulo 180°; fehlende Messwerte bleiben in den Daten sichtbar.

Ein Versuchsabbruch beendet die Kameraaufnahme; das Band kann weiter manuell
gestoppt werden. Sitzungsende, Sitzungsablauf oder Beenden des PLC-Dienstes lösen
einen Bandstopp aus, soweit ADS erreichbar ist. Bei Verbindungsfehlern gibt es
keinen automatischen Wiederanlauf; erneut **SPS verbinden** wählen. Ein Ausfall
der Kamera-API im Leerlauf trennt die SPS-Sitzung nicht.

## Roadmaps, Profile und Ergebnisse

`BIBAZU_ROADMAPS_DIR` legt den Startordner des Roadmap-Dialogs fest. Ohne diese
Variable beginnt er unter `Documents/BiBaZu_Roadmaps` im Benutzerverzeichnis;
ein benachbartes Roadmap-Repository ist nicht erforderlich. Ein kleines Beispiel
steht in [examples/roadmap.json](examples/roadmap.json). Vorhandene Roadmap-
Dateien, Meshes, Vorschaubilder und Consumer-Sidecars können unverändert verwendet
werden. Gespeicherte Transitionsmetadaten bleiben auch bei fehlender Roadmap erhalten.

AutoTune lädt und speichert Pressure-Profile der Version 13. Exportierte Profile
können weiterhin in Pressure Control geladen werden. Maschinenkalibrierung,
Lichtschrankenwerte und Roboterwinkel werden nicht aus einem Profil auf Hardware
übertragen. Bestehende Metadaten bleiben für die Dateikompatibilität erhalten;
die Oberfläche zeigt keine Roboterangaben an.

Testreihen behalten `schema_version: 1`, `roadmap_transition` und `variants`
mit je `name` und vollständigem `parameters`. Bei bereits ausgewählter Transition
wird die Zuordnung geprüft. Beispiel: [examples/testreihe.json](examples/testreihe.json).

Ergebnisse liegen standardmäßig unter `data/runs/<run_id>/`:

- `run.json`: Transition, tatsächliche Ausgangsparameter und Erstellungszeit.
- `<trial_id>.json`: Parameter, Status, Kameraeinstellungen, Messreihe und Clip-Verweis.

JSON-Dateien werden atomar ersetzt. Originalbilder bleiben im Aufnahmeordner der
Kamera-Anwendung; AutoTune kopiert sie nicht. Der Profil-Dateidialog beginnt unter
`data/profiles/` beziehungsweise im zuletzt gewählten Profilordner.

## PLC-API separat betreiben

```powershell
./StartApi.ps1 --port 8765
# In einem zweiten Terminal:
./Start.ps1 --pressure-url http://127.0.0.1:8765
```

Alternativ: `bibazu-autotune-api` oder
`python -m bibazu_autotune.pressure_service`. Der externe Dienst bleibt beim
Schließen der Oberfläche aktiv; die Freigabe der Sitzung fordert einen Bandstopp
an. Die alte API der Pressure-GUI wird für diesen Workflow nicht benötigt.

Der Dienst bindet ausschließlich `127.0.0.1`. API-Dokumentation liegt auf seinem
Port unter `/docs`, das Schema unter `/openapi.json`. Kamera-Vorgabe ist
`http://127.0.0.1:8766`.

- `GET /api/v1/health`, `/capabilities`, `/state`
- `POST /api/v1/sessions`, `/sessions/heartbeat`; `DELETE /api/v1/sessions/current`
- `PUT /api/v1/parameters`; `GET /api/v1/operations/{operation_id}`
- `POST /api/v1/conveyor/start`, `/api/v1/conveyor/stop`

API-Version und Parameterformat bleiben bei Version 1. Sitzungen benötigen
keine Roadmap-Transition. Schreibaufrufe erfordern `X-Session-Id`; Heartbeats
erfolgen alle fünf Sekunden, die Sitzung verfällt nach 15 Sekunden. Operationen
laufen asynchron und prüfen ihre Sitzung erneut vor Schreibzugriffen. Ein
Parameteraufruf startet das Band nicht. Transitionen und Dateidialoge gehören
zur Oberfläche; es gibt keinen API-Endpunkt `profile/save-dialog`.

## Entwicklung und Tests

```powershell
uv run --extra dev pytest --basetemp .test-tmp
uv run --extra dev ruff check src tests
```

Alle Tests laufen ohne Nachbar-Repositories und ohne echte ADS-/Kameraverbindung.
Sie umfassen Memory-PLC, beide HTTP-Server mit synthetischer Kamera-API, GUI,
Roadmap-Auswahl, Profile sowie den isolierten Programmstart. Ein echter
Anlagenversuch einschließlich Kamera-/Lichtschrankensignal bleibt eine gesonderte
Abnahme. `Strategy` und `RecipeStrategy` bilden weiterhin die Schnittstelle für
eine spätere Parametersuche.

Die portierten Roadmap-, Vorschau-, ADS- und Prozess-Lock-Bausteine stammen aus
BiBaZu Pressure Control und werden hier unter derselben MIT-Lizenz gepflegt.
