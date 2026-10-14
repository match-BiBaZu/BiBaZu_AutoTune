# BiBaZu AutoTune

Manuell freigegebene Parameter-Testreihen für Pressure Control und die
Baumer-Kamera-GUI. V1 verwendet vorgegebene Varianten. Sie enthält noch keinen
Optimierungsalgorithmus und bewertet den Erfolg einer Roadmap-Transition nicht automatisch.

## Installation und Start

Die drei Repositories liegen standardmäßig nebeneinander:

```text
Workspace/
  BiBaZu_Big_Boi/
  USBaZu_Baumer_Kamera_image_processing/
  BiBaZu_AutoTune/
```

In diesem Repository `Setup.ps1` ausführen. Benötigt werden Python 3.11+ oder uv.
Auch die Abhängigkeiten der beiden anderen Projekte aktualisieren:

```powershell
# Im Repository BiBaZu_Big_Boi:
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
# Im Kamera-Repository:
./Setup.ps1
# Im AutoTune-Repository:
./Setup.ps1
./Start.cmd
```

Alternativ öffnet **AutoTune öffnen** in Pressure Control das Fenster direkt.
Der Button verwendet die AutoTune-Umgebung im benachbarten Repository.
`BIBAZU_AUTOTUNE_PYTHON` überschreibt den vollständigen Pfad zu dessen Python.

## Eine Testreihe durchführen

1. Pressure Control starten, SPS verbinden und eine Roadmap-Transition auswählen.
2. Kamera-GUI starten und Kamera oder Demo verbinden. ROI, Hintergrund,
   Winkelreferenz und Aufnahmestart in der Kamera-GUI einstellen. Eine eventuell
   manuell scharfgeschaltete Aufnahme zuerst entschärfen.
3. AutoTune öffnen. Die Anwendung liest nach Sitzungsbeginn die tatsächlichen
   SPS-Einstellungen. Die Parameterbedienung in Pressure Control ist während
   der Sitzung gesperrt; das Band bleibt dort bedienbar.
4. Varianten kopieren und bearbeiten oder eine gespeicherte JSON-Testreihe laden.
   Die vier Arrays haben gemeinsame Parameter je Array und je sechs einzeln
   schaltbare Düsen. Die Bandgeschwindigkeit gilt global.
5. Band ausschalten, Bauteil in Ausgangslage bringen und **Versuch freigeben**
   wählen. Erst nach bestätigtem Schreiben und Rücklesen wird die Kamera scharf.
6. Band in Pressure Control manuell bedienen. Im Kamera-Modus **Manuell** die
   Aufnahme zusätzlich in AutoTune auslösen; bei Bewegung oder Lichtschranke
   erfolgt der Aufnahmestart entsprechend der Kamerakonfiguration.
7. Nach dem Speichern erscheint das Ergebnis. Das Band manuell ausschalten und
   das Bauteil für den nächsten Versuch zurücklegen. Jeder Versuch benötigt eine
   neue Freigabe; es gibt keine automatische Versuchsfolge.
8. Ein Ergebnis auswählen und **Ergebnis übernehmen und Profil speichern**
   anklicken. Nach Übernahme öffnet sich der bekannte Speicherdialog in Pressure
   Control. Abbrechen des Dialogs speichert keine Datei.

Der Status `completed` bedeutet, dass ein Durchlauf mit Endgrund `part_left`
aufgezeichnet und gespeichert wurde. Er sagt nicht aus, dass die Zielpose erreicht
ist. Die Kamera misst einen 2D-Achswinkel modulo 180°; Winkelqualität und fehlende
Messwerte bleiben in den Daten sichtbar.

Die APIs steuern das Band nicht automatisch. Auch bei Abbruch oder Verbindungsfehler
bleiben Bandstart und Bandstopp manuell. Übernommene Parameter bleiben bestehen.
Bei Fehlern wird kein weiterer Versuch gestartet. Neu verbinden erfolgt ausdrücklich
über **Verbinden**; Varianten bleiben für dieselbe Transition erhalten.

## Dateien und Schnittstellen

Testreihen enthalten `schema_version: 1`, `roadmap_transition` und `variants`
mit je `name` und vollständigem `parameters`. Das Fenster prüft die Zuordnung zur
ausgewählten Transition vor dem Laden. Ein Beispiel steht in
[examples/testreihe.json](examples/testreihe.json).

Ergebnisse liegen standardmäßig unter `data/runs/<run_id>/`:

- `run.json`: Transition, tatsächliche Ausgangsparameter und Erstellungszeit.
- `<trial_id>.json`: Versuchsparameter, Status, Kameraeinstellungen, vollständige
  Messreihe und Verweis auf den Kamera-Clip. Dateien werden atomar ersetzt.

Originalbilder bleiben im Aufnahmeordner der Kamera-GUI. Die AutoTune-Dateien
verweisen auf diesen lokalen Ordner; sie kopieren die Bilder nicht.

```powershell
./Start.ps1 --pressure-url http://127.0.0.1:8765 --vision-url http://127.0.0.1:8766 --output-dir D:\AutoTuneRuns
```

API-Dokumentation der laufenden Anwendungen:

- Pressure Control: `http://127.0.0.1:8765/docs`
- Kamera: `http://127.0.0.1:8766/docs`
- Maschinenlesbares Schema jeweils unter `/openapi.json`.

Die Clients in `src/bibazu_autotune/clients.py` sind unabhängig vom Fenster
verwendbar. Alle schreibenden Aufrufe benötigen den Header `X-Session-Id`.
Heartbeat alle fünf Sekunden; nach 15 Sekunden ohne Heartbeat verfällt die
Sitzung. `Strategy` und `RecipeStrategy` in `engine.py` bilden die Schnittstelle
für eine spätere Parametersuche.

## Entwicklung und Tests

```powershell
uv run --extra dev pytest --basetemp .test-tmp
uv run --extra dev ruff check src tests
```

Die Tests nutzen HTTP-Testtransporte und eine Oberfläche ohne Hardware.
`test_workspace_integration.py` prüft zusätzlich beide echten HTTP-Server mit
Fake-SPS und synthetischer Kamera, wenn die beiden Nachbarprojekte samt deren
Entwicklungsabhängigkeiten verfügbar sind. Beispiel aus dem Kamera-Repository:

```powershell
$env:PYTHONPATH = (Resolve-Path ../BiBaZu_AutoTune/src).Path
./.venv/Scripts/python.exe -m pytest ../BiBaZu_AutoTune/tests/test_workspace_integration.py --basetemp .diagnostics/autotune-integration
```

Ein realer Anlagenversuch einschließlich Lichtschrankensignal ist eine gesonderte
Abnahme. Die automatisierten Tests öffnen keine echte ADS- oder Kameraverbindung.
