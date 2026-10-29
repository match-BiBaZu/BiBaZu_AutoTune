# BiBaZu AutoTune

Eigenständige Desktop-Anwendung für STL-Geometrieauswertung, Autotune-Vorbereitung
und manuell freigegebene Impulsparameter-Testreihen.
Roadmap-Auswahl, Pose-Vorschau, Parameterbearbeitung, Bandbedienung, Versuchslogik,
Winkelverlauf und Profildateien liegen in diesem Repository. Die Pressure-GUI und
das Pressure-Repository werden zum Installieren und Ausführen nicht benötigt.
Die Kamera-Anwendung stellt weiterhin ihre Aufnahme- und Messdaten-API bereit.

Versuche verwenden weiterhin manuell gewählte Varianten. Die Autotune-Vorbereitung
berechnet analytische Offset-/Druckvorschläge für einzelne Kippbewegungen.
Ein ausgewählter Vorschlag kann ausdrücklich in den lokalen Parametereditor übernommen werden.
Der Versuchserfolg wird nicht automatisch bewertet.

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
eine Transition auswählen oder manuelle Posen bestätigen und den folgenden Ablauf verwenden.

## Autotune-Einstellungen und Berechnung

Das Hauptfenster besitzt die Reiter **Hauptseite** und **Autotune-Einstellungen**.
Einstellungen und Berechnung funktionieren auch ohne SPS- oder Kameraverbindung.
Eine auf der Hauptseite ausgewählte Roadmap liefert ihre STL-Datei
und Ausgangs-/Zielpose. Über **STL auswählen / ersetzen** lassen sich fehlende
Dateien ersetzen oder neue Bauteile ohne Roadmap laden. Beim Roadmap-/Bauteilwechsel
wird die bisherige Mesh-Zuordnung verworfen.

1. STL-Koordinateneinheit wählen: standardmäßig **mm**, alternativ **cm** oder **m**.
   ASCII- und Binär-STL werden unterstützt; die Quelldatei bleibt unverändert.
2. Unter **Autotune-Einstellungen → Modell und Montage → Modellparameter** ist die Materialdichte
   zunächst mit **1200 kg/m³** vorbelegt. Für ein anderes Material dort anpassen;
   Masse und physikalische Trägheit werden neu berechnet. Die Dichte bleibt beim
   Bauteilwechsel erhalten. Die Berechnung setzt eine homogene Massenverteilung
   voraus und bildet beispielsweise unbekannte Materialfüllungen nicht aus der STL ab.
3. Unter **Autotune-Einstellungen → Geometrie prüfen** Abmessungen im STL-Koordinatensystem,
   Oberfläche, Volumen, Volumenschwerpunkt
   und geometrischen Trägheitstensor prüfen. Die Tensoren beziehen sich auf den
   Schwerpunkt und die STL-Achsen. Intern gelten SI-Einheiten: m, m², m³, m⁵
   für den geometrischen Tensor und kg·m² für den physikalischen Tensor.
   Abmessungen, Oberfläche, Volumen und Masse werden in mm, mm²,
   mm³ beziehungsweise g angezeigt; eine zweite Spalte zeigt die SI-Werte zum Vergleich.
   Geometrischer und physikalischer Trägheitstensor stehen jeweils als X/Y/Z-Matrix
   mit Einheiten daneben. Dateipfad, STL-Inhaltshash, Importeinheit und Dreieckzahl
   kennzeichnen die Quelle. **Geometriewerte kopieren** kopiert den Bericht mit
   Einheiten zum Abgleich etwa mit CAD-Werten in die Zwischenablage.
   Die Ansicht aktualisiert sich nach dem Import und bei Änderungen an Einheit oder Dichte.
   Während einer neuen Auswertung werden alte Werte ausgeblendet; nicht verfügbare
   Volumen- oder Materialdaten bleiben ausdrücklich gekennzeichnet.
4. Für neue Bauteile **Manuelle Ausgangs- und Zielpose verwenden** aktivieren,
   einen Bauteilnamen und beide Quaternionen als **x, y, z, w** eingeben.
   Vier durch Leerzeichen oder Kommas getrennte Zahlen mit Dezimalpunkt verwenden.
   Die Quaternionen werden normalisiert und als Mesh-Vorschau angezeigt.
   Erst **Manuelle Posen bestätigen** gibt den Übergang für Trainingsversuche frei.
   Änderungen an den Posen oder ein neuer STL-Import heben die Bestätigung auf.
5. Unter **Modell und Montage** die Modellparameter und Montagepositionen prüfen.
   Die Buttons zum Speichern der Modell- und Montagestandards bleiben dort verfügbar.
6. Auf der **Hauptseite** im manuellen Parametereditor die Impulsdauer je Array
   vorgeben und **Berechnung durchführen** wählen. Arrays und Düsen müssen dafür
   noch nicht aktiviert sein. Fehlende Voraussetzungen erscheinen über dem Editor;
   während der Berechnung ist der Berechnungsbutton gesperrt.
7. Unter **Autotune-Ergebnisse** einen Kandidaten auswählen. Tabelle, Trefferbild
   und Details gehören zu diesem Kandidaten. **Versuchsprotokoll** enthält separat
   Kameraverlauf und Versuchsergebnisse; der manuelle Editor bleibt sichtbar.
8. Für die Übernahme eine **Bandgeschwindigkeit größer als 0 mm/s** eintragen und
   **Ergebnisse in Ausgangswerte schreiben** wählen. Die Aktion aktualisiert den
   sichtbaren Parametersatz und die aktuell bearbeitete Variante. Ohne vorhandene
   Variante wird **Ausgangswerte** angelegt und ausgewählt.

Übernommen werden Druck und Offset des ausgewählten Arrays. Dieses Array wird
aktiviert, ausschließlich seine berechnete Düse eingeschaltet und **Offset mit
Bandsollwert** aktiviert. Andere Arrays sowie Impulsdauer, Verzögerung und
Bandgeschwindigkeit bleiben erhalten. Die ungerundeten Werte müssen endlich und
innerhalb **0–6000 mbar** sowie **0–5000 mm** liegen. Erst danach wird kaufmännisch
auf ganze mbar und 0,1 mm gerundet; Fehler brechen die gesamte Übernahme ab.
Referenzbereichs- und Strömungswarnungen verhindern eine Übernahme innerhalb der
SPS-Grenzen nicht und bleiben in den Ergebnissen sichtbar.

Berechnung und Ergebnisübernahme ändern nur lokale Werte. Erst **Parameter übernehmen**
schreibt separat auf die SPS. Lokal übernommene Varianten bleiben auch bei einem
anschließenden SPS-Verbindungsaufbau erhalten. Während eines Versuchs sind
Berechnung und Übernahme gesperrt. Reiterwechsel erhalten Eingaben und Ergebnisse;
Änderungen an STL, Einheit, Dichte, Posen, Modell-/Montagewerten oder Impulsdauern
verwerfen die bisherigen Ergebnisse. Anschließend ist neu zu berechnen.

Die STL-Auswertung läuft getrennt von der Netzwerkverarbeitung im Hintergrund.
Überholte Importergebnisse werden verworfen. Offene oder inkonsistent orientierte
Netze liefern verfügbare Oberflächendaten; Volumen, Volumenschwerpunkt und Trägheit
bleiben ausdrücklich nicht verfügbar. Löcher und Orientierung werden nicht
automatisch repariert.

`AutotunePipeline` besitzt austauschbare Schnittstellen für Impulsdauerschätzung,
Offsetberechnung, analytischen Druckvorschlag und ML-Korrektur. Die Impulsdauer
stammt aus dem Editor; ihre automatische Schätzung und die ML-Korrektur bleiben
Platzhalter. Die Berechnung selbst erzeugt keine Variante und schreibt keine Parameter
auf die SPS. Geometrie- und Modellberechnungen laufen in eigenen Workern; geänderte
Eingaben verwerfen überholte Ergebnisse.

### Analytischer First Guess

Das Modell berechnet alle passenden Kandidaten unabhängig von den Aktivierungen
im Editor. Die vier Arrays bleiben mit jeweils sechs schaltbaren Düsen bestehen;
die acht Strahlöffnungen einer Düse werden gemeinsam gespeist.

| Arrays | Düse 1: Kippachse | Düse 4: Kippachse | Luftstrom |
| --- | --- | --- | --- |
| 1, 3 | Z | X | +Y, normal zu Y=0 |
| 2, 4 | Y | X | +Z, normal zu Z=0 |

Diese Zuordnung und die Reihenmitte relativ zur Rinnenkante sind unter
**Montage je Array/Düse** einstellbar. Beim ersten Start besitzen die Montagepositionen
keinen erfundenen Standardwert: **Nicht angegeben** muss durch einen gemessenen Wert in mm
ersetzt werden. Für Y/Z bezeichnet er die Mitte der quer zur Fahrtrichtung liegenden
Lochreihe. Für X bezeichnet er den konstanten Y- beziehungsweise Z-Abstand der
längs zur Fahrtrichtung liegenden Lochreihe von der Rinnenkante.

Unter **Modellparameter** gelten zunächst acht Öffnungen mit 0,7 mm Durchmesser,
vorläufig 24 mm Außenbreite, 25 mm Düse–Bauteil-Abstand, 20° Strahlöffnungswinkel,
1 mm zusätzlichem Randabstand, c_d=1, c_m=0,47, c_c=1, κ=1,4, 1,013 bar absolutem
Umgebungsdruck und g=9,81 m/s². Außenbreite und Teilung werden gekoppelt editiert;
die anfängliche Teilung beträgt (24−0,7)/7 ≈ 3,329 mm. Der Konzentrizitätsfaktor
c_c ist ein expliziter Faktor und wird nicht aus einem einzelnen Bauteilradius abgeleitet.

**Startwerte speichern:** Unter **Modellparameter** übernimmt **Aktuelle Modellparameter
als Standard speichern** die physikalischen Modellparameter einschließlich Dichte
als Vorgaben für den nächsten Programmstart. Unter **Montage je Array/Düse** speichert
**Aktuelle Montagewerte als Standard speichern** die Reihenmitten und Achszuordnungen.
Beide Aktionen sind unabhängig; das Speichern der Montage überschreibt keine
Modellparameter und umgekehrt. Änderungen ohne Speichern gelten nur in der Sitzung.
Manuelle Achsen-/Drehwinkelkorrekturen gelten für den aktuellen Übergang und werden
nicht als allgemeine Startwerte gespeichert. Die Vorgaben liegen unter Windows in
`%APPDATA%\BiBaZuAutoTune\autotune_defaults.json`. Fehler beim Laden oder Speichern
werden im jeweiligen Reiter angezeigt; eine beschädigte Datei wird nicht stillschweigend
überschrieben. Weder das Speichern noch das Laden schreibt SPS-Parameter.

X zeigt entlang des Bandes bergauf (Standardneigung α=2,2°); die Y-/Z-Querneigung
beträgt β=45°. In Anlagenkoordinaten gilt:

```text
g_vec = -g * (sin(α), cos(α)*sin(β), cos(α)*cos(β))
```

Das vollständige Mesh wird in die Ausgangspose gedreht und an Y=0/Z=0 angelegt.
Das Modell bestimmt die Kontakt-/Kippkante, transformiert Schwerpunkt und
Schwerpunkttensor und ergänzt für das Trägheitsmoment J um die Kippkante den
Steiner-Anteil. Die maximale potentielle Energie entlang der gewünschten Drehung
liefert Δh. Eine fehlende geradlinige Kippkante, eine Durchdringung der Auflageflächen
vor dem Kipppunkt oder ein vor dem Kipppunkt endender Drehwinkel sperren den Kandidaten.
Mehrstufige Routen werden noch nicht berechnet. Bei mehrdeutigen manuellen Posen
müssen Achse und vorzeichenbehafteter Winkel bestätigt werden; sie müssen zur Zielpose passen.

Jede Öffnung erhält ein Treffergewicht **0, 0,5 oder 1**, abhängig davon, welcher
Anteil ihres Strahlquerschnitts von der projizierten Bauteilfläche überdeckt wird.
Die Standardgrenzen sind **unter 25 % → 0**, **25 bis unter 75 % → 0,5** und
**ab 75 % → 1**. Die beiden Grenzen sind unter **Modellparameter** als Prozentwerte
einstellbar und können mit den Modellstandards gespeichert werden. Die Überdeckung
wird aus der Schnittfläche eines numerisch mit 512 Seiten angenäherten Strahlkreises
und der Bauteilprojektion bestimmt. Eine bloße Tangentialberührung zählt nicht;
Aussparungen werden aus dem vollständigen Mesh berücksichtigt.
Der zusätzliche Randabstand dient der Offsetauslegung und wird nicht von der
Trefferfläche abgezogen. Für Strahlradius r_j und wirksame Fläche gilt:

```text
r_j = y * tan(θ_j/2) + d_0/2
A_eff = sum(w_i * π * r_j²)
```

Gewicht 0,5 trägt die halbe Strahlfläche bei; die Druckformel bleibt bestehen.
Die dreistufige Bewertung ist eine Näherung der Flächenüberdeckung und keine
kontinuierliche Strömungssimulation. Der BiBaZu-Offset
beginnt bei der ersten Berührung der Strahlhülle mit der Vorderkantenebene. Bei
einer quer zu X liegenden Reihe gilt **x_Treffer = x_Vorderkante + r_j − Offset**.
Bei einer längs zu X liegenden Reihe wird zusätzlich die Position jeder Öffnung
berücksichtigt. Der Schwerpunktabstand und der tatsächliche Hebelarm zur Kippkante
werden getrennt ausgewiesen. Für Y/Z wird die äußerste Position mit positivem
Hebelarm und mindestens einem vollständig abgedeckten Strahl einschließlich
Randabstand gewählt. Für X wird die Mitte des längsten Offsetbereichs mit der
maximalen Zahl solcher vollständig abgedeckten Strahlen gewählt. Der anschließend
angezeigte Treffervektor und die Kraftberechnung verwenden die dreistufigen Gewichte.

Mit Impulsdauer Δt und Hebelarm ℓ wird die erforderliche mittlere Kraft berechnet:

```text
F_tip = sqrt(2 * J * m * g * Δh) / (Δt * ℓ)
K = c_d*c_m*c_c*A_eff * κ/(κ−1)*p_a * (d_0/(2*r_j))²
p_0 = p_a * (1 + F_tip/K)^(κ/(κ−1))
p_g = p_0 − p_a
```

Alle Rechnungen verwenden SI-Einheiten. Die Kandidatentabelle zeigt Offset in mm,
Überdruck p_g in mbar und absoluten Druck p_0 in bar. Die Detailansicht zeigt
Kippkante, J, Δh, Kraft, Treffervektor, die Auftreffpunkte und beide Mittelpunktbezüge.
Grüne Strahlkreise bedeuten Gewicht 1, orangefarbene Gewicht 0,5 und rote Gewicht 0.
Die Tabelle zeigt die Summe der Treffergewichte einschließlich halber Beiträge.
Die Details zeigen zusätzlich die tatsächliche Flächenüberdeckung je Öffnung und
den Vektor der vollständig
abgedeckten Strahlen einschließlich Offset-Randabstand aus.

**Druckbereiche:** 0,05–0,80 bar Überdruck ist der im Ausgangstext genannte
Referenzbereich der Modellparameter. Die SPS-Einstellgrenze liegt separat bei
**6 bar beziehungsweise 6000 mbar Überdruck**. Ein Wert kann innerhalb der SPS-Grenzen
liegen und trotzdem den Referenzbereich überschreiten. Außerdem setzt die verwendete
Strömungsgleichung nicht abgeschnürte Strömung voraus; bei den Standardwerten liegt
deren Grenze bei etwa 0,905 bar Überdruck. Die SPS-Freigabe bis 6 bar erweitert diese
Modellannahme nicht. Theoretische Werte außerhalb des Referenzbereichs, außerhalb der SPS-Grenzen oder
oberhalb der kritischen Strömungsgrenze werden unverändert mit Status angezeigt.
Der kritische Überdruck ist p_a * (((κ+1)/2)^(κ/(κ−1)) − 1). Es erfolgt keine Kappung.
Die acht Strahlen werden als additive Einzelstrahlen bei gemeinsamem Druck
angenommen. Strahlwechselwirkungen, Reibung und die Band-/Kippbewegung während
des Impulses sind nicht zusätzlich modelliert: Treffer und Hebelarm gelten am Impulsbeginn.

Geometrieauswertung, Einheit und Ergebnisse bleiben in der Sitzung; allgemeine
Modell- und Montagevorgaben können ausdrücklich als Startwerte gespeichert werden.
Testreihen und Pressure-Profile erhalten dadurch keine zusätzlichen Modellfelder.
Manuelle Pose-Kontexte werden für reproduzierbare Trainingsversuche
mitprotokolliert: Bauteilname, STL-SHA-256 und normalisierte Ausgangs-/Zielorientierung.
Ein anderes Bauteil, ein anderer STL-Inhalt oder andere Orientierungen beginnen
einen neuen Run. Eine Neuberechnung der Roadmap findet nicht statt.

## Eine Testreihe durchführen

1. Kamera-Anwendung starten, Kamera oder Demo verbinden und ROI, Hintergrund,
   Winkelreferenz sowie Aufnahmestart einstellen. Eine manuell scharfgeschaltete
   Aufnahme zuerst entschärfen.
2. AutoTune starten. **Roadmap auswählen** öffnet JSON-/YAML-Roadmaps mit
   Ausgangs-/Zielposen und direkten oder gruppierten Übergängen. Alternativ ein
   vorhandenes **Profil laden** oder eine **Testreihe laden**. Das ist bereits
   ohne Verbindung möglich und schreibt keine Werte auf die SPS. Für ein neues
   Bauteil alternativ STL und manuelle Posen wie oben vorbereiten und bestätigen.
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

Testreihen behalten `schema_version: 1` und `variants` mit je `name` und vollständigem
`parameters`. Roadmap-Dateien verwenden weiterhin `roadmap_transition`; manuelle
Trainingsübergänge verwenden stattdessen `trial_context` mit `kind: "manual"`.
Genau ein Kontext ist erlaubt. Profile bleiben bei Version 13; Run-, Trial- und
Kamera-Metadaten verwenden dieselbe Kontextunterscheidung. Bei bereits ausgewählter Transition
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
Abnahme. `Strategy` und `RecipeStrategy` bleiben für Variantenreihen erhalten;
`AutotunePipeline` ergänzt die unabhängigen Schnittstellen für die spätere Modellkette.

Die portierten Roadmap-, Vorschau-, ADS- und Prozess-Lock-Bausteine stammen aus
BiBaZu Pressure Control und werden hier unter derselben MIT-Lizenz gepflegt.
