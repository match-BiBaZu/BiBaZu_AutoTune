# BiBaZu AutoTune

Desktop-Anwendung zum Berechnen und Übernehmen von Parametervorschlägen für
einzelne Kippbewegungen. Bauteile kommen aus einer Roadmap oder einer manuell
geladenen STL. Die Hauptseite zeigt Ausgangs-/Zielpose, Parametereditor und
Berechnungsergebnisse; die Autotune-Einstellungen enthalten Geometrieprüfung
und Modell-/Anlagenparameter.

Die Anwendung arbeitet für Auswahl und Berechnung vollständig offline.
SPS-Verbindung, Live-Parameterübertragung und manuelle Bandbedienung sind optional.
Kamera, Variantenreihen und Versuchsdurchführung gehören nicht mehr zur normalen
Oberfläche. Ein separates Programm zur experimentellen Datenerfassung und zum
ML-Training wird später entwickelt und ist hier noch nicht begonnen.
Bestehende Versuchsbausteine und aufgezeichnete Daten bleiben erhalten.

## Forschungshintergrund

Das [analytische Modell](src/bibazu_autotune/analytical_model.py) in AutoTune schätzt den Düsendruck für Kippbewegungen. Den Forschungsansatz beschreiben Dasha Shieff et al. in [Just Add Air: Flexible Part Feeding Through Controlled Aerodynamics](https://match-bibazu.github.io/#paper-just-add-air) (WGP-Jahreskongress 2026, publikationsfertiges Manuskript).

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
```

Die angegebenen PLC-Werte sind die Vorgaben der bestehenden Anlage. Eine passende
ADS-Route und die vorhandene SPS-/Tandem-Konfiguration werden vorausgesetzt.
Kalibrierung, Lichtschranken-Einrichtung und Roboterwinkel-Verstellung bleiben
in ihren bisherigen Werkzeugen. AutoTune liest deren Werte nur als Metadaten.

## Manuelle Array- und Bandsteuerung

Für die manuelle SPS-Bedienung ist keine Bauteilauswahl erforderlich.

1. **SPS verbinden** wählen. Ohne lokale Änderungen werden nur die aktuellen
   SPS-Werte gelesen; es werden weder Standardwerte geschrieben noch das Band gestartet.
2. Array-Aktivierung, Düsen, Impulsparameter und Bandgeschwindigkeit einstellen.
3. Gültige Änderungen werden nach **250 ms Eingabepause** automatisch geschrieben
   und zurückgelesen. **Parameter übernehmen** löst die Übertragung sofort aus.
4. **Band starten** und **Band stoppen** ausdrücklich bedienen. Ein neuer Bandstart
   wartet auf bestätigte Parameterwerte. Während normaler Bandfahrt bleibt der
   Editor bedienbar.

Berechnen und Bauteil-/Poseauswahl schreiben keine SPS-Werte. Erst die ausdrückliche
Kandidatenübernahme ändert den Editor und löst bei verbundener SPS dessen Live-Übertragung aus.
Dasselbe gilt für Änderungen im Editor und das Laden eines Parametersatzes.
Neuere Eingaben während einer Schreiboperation bleiben erhalten; älteres Rücklesen
überschreibt sie nicht. Ungültige Eingaben werden gemeldet und nicht übertragen.
Bei Verbindungs- oder Rücklesefehlern erfolgt keine automatische Wiederholung.

Ohne Verbindung bleiben Änderungen lokal und werden beim späteren Verbinden
abgeglichen. **Parameter sperren** sperrt Editor, Parameterdatei-Laden und
Ergebnisübernahme. Noch nicht gesendete Änderungen warten auf das Entsperren;
ein laufender Schreibauftrag wird abgeschlossen. Bandbedienung, Berechnung und
Ergebnisansicht bleiben verfügbar. Die Sperre bleibt über Verbindungswechsel
innerhalb derselben Sitzung erhalten.

Die SPS-Verbindung benötigt keine Kamera-API. Verbinden, Bauteilauswahl und
Berechnung legen keine experimentellen Run-Dateien an. Sitzungsende fordert
über den vorhandenen SPS-Dienst einen Bandstopp an.

## Separater Drucktest

`Start-Drucktest.cmd` oder `./Start-Drucktest.ps1` öffnet ein eigenständiges Testfenster.
AutoTune und Pressure Control vorher schließen, da der Test denselben exklusiven
SPS-Zugriff verwendet. Öffnen und **SPS verbinden** starten keinen Luftstoß.
**Testreihe starten** führt ausschließlich an **Array 2, Düse 1 / OpenValve5** aus:
20 bis 100 ms in 10-ms-Schritten, jeweils 1000–6000 mbar in 1000-mbar-Schritten
und fünf Wiederholungen. Das sind 270 Stöße, nach Impulsdauer, Druck und Wiederholung
sortiert. Die Reihe wird erst über **Testreihe starten** ausgelöst.
Zwischen Ventilschluss und dem nächsten Stoß liegen mindestens drei Sekunden.
Vor dem ersten Stoß jeder neuen Druck-/Impulsdauer-Kombination wird nach bestätigter
Parametereinstellung mindestens **fünf Sekunden Einschwingzeit** gewartet, auch zu
Beginn des Laufs und beim Wechsel der Impulsdauer zurück auf 1000 mbar.
Innerhalb der fünf Wiederholungen beträgt die Einschwingzeit mindestens eine Sekunde
und liegt innerhalb der Stoßpause. Die Wartezeiten werden in `run.json` dokumentiert.
Band und andere Düsen bleiben aus. Lichtschranke 4 muss frei sein.

**Abbrechen** verwendet eine separate HTTP-Verbindung und einen priorisierten SPS-Auftrag.
Jede Startanforderung besitzt eine ID; auch ein vor dem Start empfangener Abbruch sperrt
genau diese Anforderung. Fenster schließen oder Sitzung verlieren bricht die Reihe ab.
Einzelstöße werden SPS-seitig begrenzt; sie hängen nicht von einem Windows-Schließtimer ab.
Nach Abschluss werden Parameterwerte zurückgeschrieben, aber Arrays und Band bleiben aus.
Die frühere Array-Aktivierung wird nicht automatisch wiederhergestellt.

Aufzeichnung und Speicherung erfolgen im SPS-Dienst. Der Aufruf abonniert beide Rohwerte,
beide skalierten Druckwerte und den Ventilkommandostatus als zyklische ADS-Notifications
mit 1-ms-Serverzeitstempeln. **100 ms Vorlauf und 200 ms Nachlauf** werden gespeichert.
Das ist kein neuer SPS-Puffer: Die EL3068- und Sensor-Aktualisierung begrenzen zusätzlich
die physikalische Messauflösung. Vor dem ersten Stoß werden gemeinsame, lückenlose
Zeitstempel geprüft. Fehlende Werte werden nicht interpoliert; unvollständige Messungen
werden gespeichert, aber von Mittelwerten ausgeschlossen und die Reihe wird angehalten.

Unter `data/pressure_tests/<Anforderungs-ID>/` liegen `run.json`, `summary.csv`,
die gemeinsame Rohdatendatei `samples.csv`,
`combinations.csv`, pro Stoß `pulse_NNN.csv` und die unveränderten ADS-Ereignisse
`pulse_NNN_notifications.csv`. Die Tabelle zeigt Spitzenwert und Mittelwert während
des Öffnungskommandos. Die skalierte Druckkurve ist nach jedem Stoß auswählbar.
Die SPS-Skalierung `Rohwert * 10 / 32767` bleibt vorläufig; sie ist noch nicht anhand
des Sensortyps bestätigt. Es wird keine Korrektur im analytischen Modell vorgenommen.

Die SPS benötigt nur die optionale Kurzimpuls-Erweiterung ihrer bestehenden
Kalibrierfunktion: drei zusätzliche Variablen und eine begrenzte Ergänzung von
Validierung und automatischem Ventilschluss. Normaler Kalibrierbetrieb, bestehende
Watchdogs, E/A-Zuordnung und die normale Anlagenlogik bleiben bestehen.
`tools/install_pressure_test_plc.py <MAIN.TcPOU>` zeigt den Quelltextpatch;
`--apply` schreibt ihn mit Sicherungskopie (`.TcPOU.before-pressure-test-v2`).
Er unterstützt auch die bisherige Kurzimpuls-Version 1 (20/50/100 ms) und erweitert
sie auf Version 2; vorhandene Sicherungen und Messläufe bleiben erhalten.
Der Installer lädt nichts auf die SPS.
Die neue Quelle anschließend in TwinCAT bauen und laden. Das Testfenster verweigert
den Start ohne `ProbeCalibrationTimedPulseVersion = 2`.

Nur für ausdrücklich angeforderte automatische Ausführung steht
`./Start-Drucktest.ps1 --auto-run` zur Verfügung; dieser Aufruf verbindet und startet
die Reihe im sichtbaren Fenster. Der normale Aufruf wartet auf den Startbutton.

## Bauteilauswahl und Berechnung

1. Auf der **Hauptseite** den Einstieg wählen:
   - **Roadmap laden:** vorhandene Auswahl von Bauteil und Transition. Ausgangs-/Zielpose
     und Drehkommando werden aus der Roadmap übernommen. Mehrstufige Bewegungsfolgen
     bleiben analytisch nicht unterstützt und werden mit einem Grund angezeigt.
   - **Aus STL laden:** eine STL wählen und den Dialog **Bauteil und Posen** öffnen.
     Bauteilname ist mit dem Dateinamen vorbelegt, Importeinheit standardmäßig mm
     (alternativ cm oder m).
2. Bei manueller STL die Ausgangslage mit **±X/±Y/±Z** ausrichten. Der einstellbare
   Drehschritt beträgt anfangs **90°**. **STL-Lage zurücksetzen** stellt die originale
   STL-Orientierung her. Die Achsen beziehen sich auf die dargestellte Rinne.
3. Das Kippziel über **Achse, Drehsinn +/− und Winkel in Grad** festlegen;
   Vorgabe **Y, positiv, 90°**. Ein einzelner Winkel muss größer 0 und höchstens
   360° sein. Die Zielpose entsteht durch diese Drehung aus der Ausgangspose.
   Beide Vorschauen aktualisieren sich sofort; Auflage an Y=0 und Z=0 wird
   automatisch dargestellt. Quaternionen werden nur intern verwendet.
4. Mit **Bauteil übernehmen** bestätigen. Abbrechen erhält die bisherige Auswahl
   samt Ergebnissen. **Bauteil und Posen bearbeiten** auf der Hauptseite öffnet den
   Dialog erneut. **STL zuordnen / ersetzen** ordnet eine fehlende oder andere
   Datei zu. Bei einer Roadmap bleiben dabei deren Posen und Transition erhalten.
   Für einen geladenen manuellen Parametersatz ohne STL muss die zugeordnete Datei
   den gespeicherten Inhaltshash besitzen; ein neues Bauteil über **Aus STL laden** wählen.
5. Unter **Autotune-Einstellungen → Geometrie prüfen** Abmessungen, Fläche,
   Volumen, Schwerpunkt, Masse und Trägheit mit CAD-Werten vergleichen. Die Tabellen
   zeigen praktische Einheiten und SI-Werte; beide Tensoren stehen als X/Y/Z-Matrix
   daneben. **Geometriewerte kopieren** kopiert den Bericht mit Einheiten.
   Nicht verfügbare Werte sind gekennzeichnet, alte Ergebnisse werden beim
   Neuimport ausgeblendet. STL-Auswertung und Modellberechnung laufen im Hintergrund.
6. Unter **Modell und Anlage** die Bereiche prüfen:

   | Bereich | Einstellungen |
   | --- | --- |
   | **Material und Kippen** | Dichte (Standard 1200 kg/m³), Schwerkraft, Neigung und Kontakt-Toleranz |
   | **Düsen und Luftstrahl** | Öffnungen, Durchmesser, Teilung, Breiten, Abstand und Strahlöffnungswinkel |
   | **Treffer und Offset** | Treffergewichte, Randabstand und Offsetreserve |
   | **Analytischer Druck** | Strömungsbeiwerte, Isentropenexponent und Umgebungsdruck |
   | **SPS-Druckskalierung** | Koeffizienten a/b für den SPS-Sollwert |
   | **Montage je Array/Düse** | Reihenmitten, Achsen und X-Versätze, nach Array filterbar |

   Bereichswechsel erhalten Eingaben und Ergebnisse. Modell- und Montagestandards
   werden weiterhin getrennt gespeichert. Datei- und Poseauswahl befinden sich
   ausschließlich auf der Hauptseite, nicht in den Einstellungen.
7. Auf der Hauptseite **Impulsdauer je Array** im Parametereditor einstellen und
   **Berechnung durchführen** wählen. Arrays und Düsen müssen dafür nicht aktiviert
   sein; eine SPS-Verbindung ist nicht erforderlich. Fehlende Voraussetzungen werden angezeigt.
8. Einen Kandidaten in den **Autotune-Ergebnissen** auswählen. Tabelle, Trefferbild
   und Details zeigen analytischen Druck, skalierten SPS-Sollwert, erforderliche
   Kraft und Offset. Bei positiver Bandgeschwindigkeit übernimmt
   **Ergebnisse in Ausgangswerte schreiben** den skalierten Druck und Offset in den
   aktuellen Parametersatz. Das Array wird aktiviert, genau die automatisch gewählten
   Düsen eingeschaltet und **Offset mit Bandsollwert** aktiviert. Andere Arrays,
   Impulsdauer, Verzögerung und Bandgeschwindigkeit bleiben erhalten.

Die ungerundeten Übernahmewerte müssen endlich und innerhalb **0–6000 mbar** und
**0–5000 mm** liegen. Erst danach wird kaufmännisch auf ganze mbar und 0,1 mm
gerundet. Fehler brechen die gesamte Übernahme ab; fehlende Skalierung wird nicht
durch den analytischen Druck ersetzt. Referenzbereichs- und Strömungswarnungen
bleiben sichtbar und verhindern eine Übernahme innerhalb der SPS-Grenzen nicht.

**Datei → Parametersatz laden/speichern** verwaltet genau einen Parametersatz im
Pressure-Profilformat Version 13. Das Speichern schreibt keine SPS-Werte.
Auch ältere Pressure-Profile ohne Bauteilkontext werden unterstützt; beim Laden
bleibt eine bereits gewählte Roadmap oder STL erhalten. Ein Parametersatz kann
auch ohne Bauteilauswahl gespeichert werden.
Bauteilkontext und Posen werden als Metadaten gespeichert, STL-Datei, geometrische
Auswertung und Modellparameter nicht eingebettet. Eine STL kann beim Laden erneut
zugeordnet werden. Alte manuelle Posen ohne Drehkommando bleiben lesbar; bei einer
mehrdeutigen Orientierung, insbesondere 180°, muss der Drehsinn im Pose-Dialog
bestätigt werden. +180° und −180° bleiben danach getrennt gespeichert.

Geometrie, Importeinheit und Berechnungsergebnisse sind Sitzungsdaten. Modell- und
Montagestandards werden nur mit ihren ausdrücklich beschrifteten Schaltflächen
gespeichert. Die analytische Rechnung ist weiterhin eine Näherung; ML-Korrektur
ist noch nicht implementiert.

### Analytischer First Guess

Das Modell berechnet einen Vorschlag je passender Array-/Dreiergruppe unabhängig
von den Aktivierungen im Editor. Die vier Arrays besitzen jeweils sechs schaltbare
Düsen. Düsen **2/3 verlängern die acht Öffnungen von Düse 1**, Düsen **5/6** entsprechend
die von Düse **4**. Jede Düse hat weiterhin acht gemeinsam gespeiste Öffnungen.

| Arrays | Düsen 1–3: Kippachse | Düsen 4–6: Kippachse | Luftstrom |
| --- | --- | --- | --- |
| 1, 3 | Z | X | +Y, normal zu Y=0 |
| 2, 4 | Y | X | +Z, normal zu Z=0 |

Diese Zuordnung und die Reihenmitte relativ zur Rinnenkante sind unter
**Montage je Array/Düse** einstellbar. Die bestätigten Montagevorgaben sind
**12,44 / 37,32 / 62,20 mm** für die Reihenmitten der Düsen 1–3. Düsen 4–6
liegen jeweils **12,44 mm** quer zur Fahrtrichtung von der Rinnenkante entfernt.
Die Außenkante des Düsenblocks ist mit der inneren Rinnenkante ausgerichtet;
die Maße verwenden somit denselben Nullpunkt wie die Bauteilpositionierung.
Für Y/Z bezeichnet die Reihenmitte die Mitte der quer zur Fahrtrichtung liegenden
Lochreihe. Für X bezeichnet er den konstanten Y- beziehungsweise Z-Abstand der
längs zur Fahrtrichtung liegenden Lochreihe von der Rinnenkante.

Alle **24 Montagepositionen** werden einzeln erfasst. Für die entlang X liegenden
Reihen zusätzlich den **X-Versatz der Reihenmitte zur Referenzdüse 4** eingeben;
negative Werte sind erlaubt. Die gemessenen Vorgaben für Düse 4/5/6 sind
**0 / 24,88 / 49,76 mm**; derselbe Düsenmittenabstand gilt für alle vier Arrays.
Die Referenzdüse hat Versatz 0. Bei angepassten
Achszuordnungen gilt entsprechend Düse 1 als Referenz der ersten Gruppe.
**Fehlt ein Montagemaß einer Dreiergruppe, bleibt deren Arrayvorschlag gesperrt.**
Achsen und Strahlrichtung müssen innerhalb einer Gruppe übereinstimmen.

**Automatische Zuschaltung:** An jeder untersuchten Offsetposition wird geprüft,
welche Düsen mindestens einen vollständigen Strahltreffer **einschließlich Randabstand**
besitzen. Nur diese Düsen werden eingeschaltet. Bei einer 20 mm breiten bestrahlten
Fläche kann das ausschließlich Düse 1 sein; bei größeren Flächen kommen Düse 2 und 3
hinzu. Entscheidend sind die tatsächlichen Montagepositionen und die Mesh-Projektion
einschließlich Aussparungen. Eine Düse mit ausschließlich Teiltreffern bleibt aus.
Auch **nur Düse 2** ist möglich: Vorgänger werden nicht zwangsweise mitgeschaltet.
Für eingeschaltete Düsen werden alle acht Öffnungen mit ihren Gewichten 0/0,5/1
berücksichtigt. Die Kandidatentabelle nennt die automatisch ausgewählten Düsen.

Unter **Düsen und Luftstrahl** gelten acht Öffnungen mit gemessenem **Radius 0,4 mm**,
also **Durchmesser 0,8 mm**, und **2,8 mm Lochmittenabstand**.
Der Abstand von erster zu letzter Lochmitte ist damit **7 × 2,8 = 19,6 mm**;
die Außenbreite über die Öffnungskanten beträgt **19,6 + 0,8 = 20,4 mm**.
Beide Breiten werden in der GUI getrennt angezeigt. Sie bezeichnen die Öffnungen,
während **24,88 mm** der Abstand zwischen zwei Düsenmitten ist.
Die weiteren Vorgaben bleiben 25 mm Düse–Bauteil-Abstand, 20° Strahlöffnungswinkel,
1 mm zusätzlichem Randabstand und **5 mm zusätzlicher Offsetreserve entlang X**,
c_d=1, c_m=0,47, c_c=1, κ=1,4, 1,013 bar absolutem
Umgebungsdruck und g=9,81 m/s². Außenbreite und Teilung werden gekoppelt editiert;
der Abstand zwischen erster und letzter Lochmitte wird daraus berechnet. Der Konzentrizitätsfaktor
c_c ist ein expliziter Faktor und wird nicht aus einem einzelnen Bauteilradius abgeleitet.

**Startwerte speichern:** **Modellwerte aller Bereiche als Standard speichern**
übernimmt die physikalischen Modellparameter einschließlich Dichte und SPS-Skalierung
als Vorgaben für den nächsten Programmstart. Unter **Montage je Array/Düse** speichert
**Montagewerte aller Arrays als Standard speichern** die Reihenmitten und Achszuordnungen
aller Arrays, auch bei gefilterter Anzeige. Im Montagebereich wird nur diese
Montageschaltfläche angezeigt; in den übrigen Bereichen die gemeinsame Modellschaltfläche.
Beide Aktionen sind unabhängig; das Speichern der Montage überschreibt keine
Modellparameter und umgekehrt. Änderungen ohne Speichern gelten nur in der Sitzung.
Manuelle Achsen-/Drehwinkelkorrekturen gelten für den aktuellen Übergang und werden
nicht als allgemeine Startwerte gespeichert. Die Vorgaben liegen unter Windows in
`%APPDATA%\BiBaZuAutoTune\autotune_defaults.json`. Fehler beim Laden oder Speichern
werden im jeweiligen Bereich angezeigt; eine beschädigte Datei wird nicht stillschweigend
überschrieben. Weder das Speichern noch das Laden schreibt SPS-Parameter.
Alte Standarddateien (Version 1, bisher acht Montagepositionen) werden beim Laden
im Speicher auf 24 Positionen ergänzt. Gespeicherte Maße für Düse 1/4 bleiben erhalten;
fehlende Positionen verwenden die bestätigten Montagevorgaben. Gespeicherte persönliche
Modellparameter haben weiterhin Vorrang vor den Programmvorgaben. Erst bewusstes
Speichern schreibt das erweiterte Standardformat Version 2. Testreihen,
SPS-API und Pressure-Profilversion 13 bleiben unverändert.

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
werden Achse und Drehsinn im Dialog auf der Hauptseite bestätigt; sie müssen zur Zielpose passen.

Jede Öffnung erhält ein Treffergewicht **0, 0,5 oder 1**, abhängig davon, welcher
Anteil ihres Strahlquerschnitts von der projizierten Bauteilfläche überdeckt wird.
Die Standardgrenzen sind **unter 25 % → 0**, **25 bis unter 75 % → 0,5** und
**ab 75 % → 1**. Die beiden Grenzen sind unter **Treffer und Offset** als Prozentwerte
einstellbar und können mit den Modellstandards gespeichert werden. Die Überdeckung
wird aus der Schnittfläche eines numerisch mit 512 Seiten angenäherten Strahlkreises
und der Bauteilprojektion bestimmt. Eine bloße Tangentialberührung zählt nicht;
Aussparungen werden aus dem vollständigen Mesh berücksichtigt.
Der zusätzliche Randabstand dient der Offsetauslegung und wird nicht von der
Trefferfläche abgezogen. Unter **Treffer und Offset → Zusätzliche Offsetreserve entlang X**
werden standardmäßig weitere **5 mm in beiden X-Richtungen** berücksichtigt.
Ein Volltreffer für die Düsenfreigabe muss damit auch bei einer Verschiebung innerhalb
dieser Reserve vollständig einschließlich Randabstand auf derselben Fläche liegen;
Aussparungen dürfen dabei nicht überquert werden. Die Reserve wirkt nur längs zum Band,
damit schmale Bauteile nicht wegen eines vergrößerten seitlichen Randabstands ausfallen.
Treffergewichte und Hebelarme werden weiterhin an der tatsächlich gewählten Startposition
berechnet. Die Einstellung wird mit den Modellstandards gespeichert; alte Standarddateien
ohne dieses Feld erhalten beim Laden 5 mm, ohne dabei die Datei zu verändern.
Mit **0 mm** gilt die bisherige Offsetauswahl.
Passt die Reserve bei einem kurzen Bauteil nicht auf die Fläche, bleibt der Vorschlag
mit einer Meldung gesperrt; die Reserve kann für dieses Bauteil reduziert werden.
Die Reserve ist ein räumlicher Sicherheitsabstand, keine Korrektur eines abweichenden
SPS-Nullpunkts oder eine Berechnung von Ventil- und Bandverzögerungen.
Für Strahlradius r_j und wirksame Fläche gilt:

```text
r_j = y * tan(θ_j/2) + d_0/2
A_eff = sum(w_i * π * r_j²)  # Nur Öffnungen eingeschalteter Düsen.
ℓ_eff = sum(w_i * π * r_j² * ℓ_i) / A_eff
```

Gewicht 0,5 trägt die halbe Strahlfläche bei; die Druckformel bleibt bestehen.
Für mehrere Düsen ersetzt der flächengewichtete Hebelarm ℓ_eff den Einzelhebelarm.
Die Einzelhebelarme ℓ_i folgen aus Auftreffpunkt, Kippkante und Strahlrichtung.
Der feste Offsetbezug bleibt bei Düse 1 beziehungsweise 4, auch wenn diese aus ist;
die erste aktive Öffnung wird nicht als neuer Nullpunkt verwendet.
Die dreistufige Bewertung ist eine Näherung der Flächenüberdeckung und keine
kontinuierliche Strömungssimulation. Der BiBaZu-Offset
beginnt bei der ersten Berührung der Strahlhülle mit der Vorderkantenebene. Bei
einer quer zu X liegenden Reihe gilt **x_Treffer = x_Vorderkante + r_j − Offset**.
Für den Kraftangriff nahe der Hinterkante eines geraden 80-mm-Bauteils ergibt sich
mit 1 mm Randabstand und 5 mm Offsetreserve ungefähr **80 − 1 − 5 = 74 mm**
statt bisher 79 mm. Für die Gegenrichtung wandert die Position entsprechend von
der Vorderkante weiter nach innen. Bei unregelmäßigen Konturen können andere Werte entstehen.
Bei einer längs zu X liegenden Reihe wird zusätzlich die Position jeder Öffnung
berücksichtigt. Der Schwerpunktabstand und der tatsächliche Hebelarm zur Kippkante
werden getrennt ausgewiesen. Für Y/Z wird die äußerste Position mit positivem
Hebelarm gewählt, bei der zunächst möglichst viele Düsen mindestens einen vollständig
abgedeckten Strahl einschließlich Randabstand besitzen. Unter diesen Positionen
wird der größte positive wirksame Hebelarm gewählt, danach die höchste Gewichtssumme.
So schaltet eine geringfügig schräge Bauteilkante nicht unnötig weitere Düsen aus.
Für X wird die Mitte des längsten Offsetbereichs mit der
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
Die benötigte mittlere Gesamtkraft steht außerdem direkt in der Tabellenspalte
**Kraft [N]**; bei fehlgeschlagener Berechnung erscheint dort **—**.
Grüne Strahlkreise bedeuten Gewicht 1, orangefarbene Gewicht 0,5 und rote Gewicht 0.
Das Trefferbild zeigt die **Förderbandrichtung (+X) von rechts nach links** als Pfeil.
Größere X-Koordinaten liegen deshalb links im Bild; die Querrichtung Y beziehungsweise Z
zeigt nach oben. Die Ansicht ist gegenüber der bisherigen Darstellung horizontal
gespiegelt, die Anlagenkoordinaten und die Offsetberechnung bleiben unverändert.
Welche Bauteilseite beaufschlagt wird, bestimmt der gewünschte Drehsinn zusammen
mit der Kippkante und der Strahlrichtung. Bei umgekehrtem Drehsinn wechselt für
Y-/Z-Drehungen die Kippkante und damit auch die Seite für den Kraftangriff.
Ausgeschaltete Düsen werden im Trefferbild vollständig ausgeblendet und beeinflussen
dessen Skalierung nicht. Die Ansicht zeigt die 8, 16 oder 24 Öffnungen der aktiven
Düsen mit Beschriftung **Düse.Öffnung**. Die Details enthalten weiterhin alle Düsen
zur Prüfung der Zuschaltgründe und unterscheiden geometrische
Überdeckung und wirksame Gewichte und nennen Zuschaltgrund, Volltrefferzahl und
Flächenbeitrag je Düse sowie die Einzelhebelarme.
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

**Empirische SPS-Druckskalierung:** Nach dem analytischen Modell wird der Sollwert
als `p_SPS = (p_analytisch − b) / a` berechnet. Alle Drücke sind Überdrücke;
intern werden Pa verwendet, in der GUI mbar. Der analytische Absolutdruck ist
kein Eingang der Skalierung. Unter **SPS-Druckskalierung** sind **Druckskalierung a [–]**
und **Druckskalierung b [mbar]** einstellbar. a muss endlich und positiv sein,
b endlich; beide Vorzeichen von b sind erlaubt. Die vorhandene Schaltfläche
zum Speichern der Modellstandards speichert auch diese Werte. Alte Standarddateien
erhalten fehlende Felder beim Laden im Speicher, ohne dabei überschrieben zu werden.

Die Vorgaben `a = 0,6544389198933329` und `b = −284,80891486008954 mbar`
stammen aus dem Spitzendruck-Fit bei **100 ms, Array 2/Düse 1**, Messlauf
`53f1095c8f3244b182ab08c3980a6dac`: Wiederholungen 2–5 der sechs Druckstufen,
insgesamt 24 Messungen. Dieses eine Paar gilt vorläufig für **alle Arrays,
Düsenkombinationen und Impulsdauern**, ohne automatische Umschaltung.
Beispiel: 2000 mbar analytischer Druck ergeben 3491,24852665 mbar SPS-Sollwert;
übernommen werden kaufmännisch gerundet **3491 mbar**. Mit a=1 und b=0 ist die
Skalierung neutral. Änderungen an a oder b verlangen eine neue Berechnung.

Spitzendruck und analytisch angenommener Druck sind damit empirisch zugeordnet;
die tatsächliche Kippwirkung sowie andere Düsen-/Versorgungszustände sind dadurch
nicht gesondert validiert. Die Details zeigen Formel, aktuelle Koeffizienten und
Herkunft. Die SPS-Druckgrenzen 0–6000 mbar werden am **skalierten** Wert geprüft;
Referenzbereich und Strömungsgrenze bleiben Prüfungen des analytischen Drucks.
Endliche Ergebnisse außerhalb der Grenzen bleiben sichtbar. Die Übernahme prüft
vor dem Runden und lehnt ungültige Werte ohne Kappung und ohne Teiländerungen ab.
Berechnungen allein schreiben keine Parameter; erst die ausdrückliche Übernahme
in den Editor löst dessen bestehende Live-Synchronisierung aus.

Die Strahlen aller zugeschalteten Düsen werden als additive Einzelstrahlen bei gemeinsamem Druck
angenommen. Strahlwechselwirkungen, Reibung und die Band-/Kippbewegung während
des Impulses sind nicht zusätzlich modelliert: Treffer und Hebelarm gelten am Impulsbeginn.

Geometrieauswertung, Einheit und Ergebnisse bleiben in der Sitzung; allgemeine
Modell- und Montagevorgaben können ausdrücklich als Startwerte gespeichert werden.
Testreihen und Pressure-Profile erhalten dadurch keine zusätzlichen Modellfelder.
Manuelle Pose-Kontexte speichern Bauteilname, STL-SHA-256, normalisierte Ausgangs-/
Zielorientierung und das ausdrücklich gewählte Drehkommando im Parametersatz.
Ein anderes Bauteil, ein anderer STL-Inhalt oder andere Orientierungen invalidieren
die Berechnung; es wird dabei kein Versuchs-Run angelegt. Eine Neuberechnung
der Roadmap findet nicht statt.

## Abgrenzung zur Versuchsaufzeichnung

Die normale AutoTune-Oberfläche berechnet Parameter und bedient bei Bedarf die SPS.
Sie enthält keine Kamera-Verbindung, Versuchsfreigabe, Aufnahmebedienung,
Variantenverwaltung oder Versuchsprotokollansicht.

`Engine`, `Strategy`, `RecipeStrategy`, bestehende Kamera-Clients und deren
Dateiformate bleiben als vorhandene Bausteine mit eigenen Tests erhalten.
Frühere Aufzeichnungen unter `data/runs/` werden nicht verändert oder gelöscht.
Ein neues Programm für experimentelle Datenerfassung und ML-Training wird
hier noch nicht entwickelt. Der vorhandene separate Drucktest bleibt unverändert.

## Roadmaps und Parametersätze

`BIBAZU_ROADMAPS_DIR` legt den Startordner des Roadmap-Dialogs fest. Ohne diese
Variable beginnt er unter `Documents/BiBaZu_Roadmaps`; ein Nachbar-Repository ist
nicht erforderlich. Ein Beispiel steht in [examples/roadmap.json](examples/roadmap.json).
Vorhandene Roadmaps, Meshes, Vorschaubilder und Consumer-Sidecars bleiben nutzbar.
Bei fehlender STL kann auf der Hauptseite eine Ersatzdatei zugeordnet werden.

Das Dateimenü lädt und speichert Pressure-Profile **Version 13**. Diese bleiben
in Pressure Control verwendbar. Bestehende Maschinenmetadaten werden erhalten,
jedoch keine Roboterwinkel oder Kalibrierwerte aus einer Datei auf Hardware übertragen.
Roadmaps verwenden `roadmap_transition`; manuelle Auswahl verwendet `trial_context`
mit `kind: "manual"`, Bauteilname, STL-Inhaltshash und normalisierten Posen.
Neue manuelle Kontexte speichern zusätzlich `rotation_axis` und `signed_angle_deg`;
beide werden gemeinsam und gegen die Zielorientierung geprüft. Ein Parametersatz
enthält höchstens einen Kontext. Quaternionen bleiben ein internes Speicherformat.

Dateien werden atomar ersetzt. Der Dateidialog beginnt unter `data/profiles/`
oder im zuletzt gewählten Ordner. Beim Laden werden alle Parameter validiert,
bevor die Auswahl und der Editor geändert werden. Bei bestehender SPS-Verbindung
werden die geladenen Editorwerte über die reguläre Live-Synchronisierung übertragen.

Alte Testreihen (`schema_version: 1`, `variants`) bleiben über die vorhandenen
Bibliotheksbausteine lesbar, sind aber nicht Teil des normalen Dateimenüs.
Beispiel: [examples/testreihe.json](examples/testreihe.json). Auch die alten
Run-/Trial-Dateien mit Parametern, Messreihen und Clip-Verweisen bleiben erhalten.
Der normale Berechnungsablauf erzeugt keine solchen Dateien.

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
Port unter `/docs`, das Schema unter `/openapi.json`. Der normale
AutoTune-Start baut keine Kamera-Verbindung auf; ein alter `--vision-url`-Parameter
wird für Startskript-Kompatibilität noch akzeptiert, aber nicht verwendet.

- `GET /api/v1/health`, `/capabilities`, `/state`
- `POST /api/v1/sessions`, `/sessions/heartbeat`; `DELETE /api/v1/sessions/current`
- `PUT /api/v1/parameters`; `GET /api/v1/operations/{operation_id}`
- `POST /api/v1/conveyor/start`, `/api/v1/conveyor/stop`

API-Version und Parameterformat bleiben bei Version 1. Sitzungen benötigen
keine Roadmap-Transition. Schreibaufrufe erfordern `X-Session-Id`; Heartbeats
erfolgen in der Oberfläche alle 0,5 Sekunden, die Sitzung verfällt nach 15 Sekunden. Operationen
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

In `analytical_model.py` führt `AnalyticalModel.evaluate` durch die Berechnung:
Ausgangspose transformieren, Kippmechanik bestimmen, Strahlen platzieren und Druck
berechnen. `tipping_mechanics` trennt die Suche der Kontaktkante von der Prüfung
auf Durchdringung der Auflageflächen. Die Offsetsuche bewertet Positionen als
benannte `ImpactSolution`-Ergebnisse; `_centered_offset` enthält die Auswahlregel
für X und `_outermost_offset` die für Y/Z. Formeln, Treffergrenzen und numerische
Toleranzen sind davon unabhängig und bleiben unverändert.

Die portierten Roadmap-, Vorschau-, ADS- und Prozess-Lock-Bausteine stammen aus
BiBaZu Pressure Control und werden hier unter derselben MIT-Lizenz gepflegt.
