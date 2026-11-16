# Vergleich: Differenzgruppe und ursprüngliche Gruppe „Subtrahieren“

Stand: 11. November 2026.

Dieses Dokument vergleicht die ursprüngliche Gruppe **Subtrahieren** mit der neu
implementierten **Differenzgruppe**. Die zusätzlich vorgeschlagene Absicherung
gegen sinkende Energiewerte wird gesondert beschrieben: Sie ist bisher ein Konzept
und gehört noch nicht zum implementierten Verhalten.

Als Codegrundlage dienen die Differenzgruppe im Branch
`inherit_long-term_statistics`, Commit `fc9883b797fb137717c2ae0eb9822cc131c36527`,
und die ursprüngliche Subtrahieren-Gruppe aus `master`, Commit
`dc815072d6fc8f44ff09928c5ea1e75b68b42fe6`. Die Subtrahieren-Gruppe entspricht im
genannten Entwicklungsstand wieder dieser ursprünglichen Implementierung.

## Überblick

| Eigenschaft | Subtrahieren, ursprüngliches Verhalten | Differenzgruppe, implementierter Stand |
| --- | --- | --- |
| Auswahl | Eine Basis-Leistungsentität und abzuziehende Leistungsentitäten | Ein Basisgerät und mehrere Differenzgeräte |
| Benötigte Eingangswerte | Leistung | Leistung und Energie je Gerät |
| Gruppenleistung | Basisleistung minus Summe der abzuziehenden Leistungen | Leistungssensoren des Basisgeräts minus Leistungssensoren der Differenzgeräte |
| Gruppenenergie | Normalerweise zeitliche Integration der Gruppenleistung | Verrechnung der Zuwächse vorhandener Energiezähler |
| Eigene Integration | Ja, beim automatisch erzeugten virtuellen Energiesensor | Nein |
| Erzeugte Sensoren | Leistung; Energie abhängig von der Konfiguration | Immer Leistung und Energie |
| Verbrauch während einer HA-Auszeit | Aus verpassten Leistungswerten allein nicht zuverlässig rekonstruierbar | Kann durch weiterlaufende Gerätezähler nachgeliefert werden |
| Unterschiedliche Meldezyklen der Energiezähler | Ohne Einfluss auf die normale Integralberechnung | Können vorübergehend sinkende oder überhöhte Gruppenwerte verursachen |
| Unterschiedliche Meldezyklen der Leistungssensoren | Beeinflussen Gruppenleistung und daraus berechnete Energie | Beeinflussen Gruppenleistung; Gruppenenergie wird unabhängig davon berechnet |
| Energie beim Erstellen | Neue Integralzählung; keine Übernahme der Mitgliederhistorie | Start bei null oder aktuelle Energiedifferenz mit einmaliger Statistikübernahme |
| Garantiert monotoner Energiezähler | Keine allgemeine Garantie, etwa bei negativer Gruppenleistung | Bisher keine Garantie; dafür wurde ein zusätzliches Konzept erarbeitet |

Der Vergleich der Energieverfahren bezieht sich bei **Subtrahieren** auf den
automatisch erzeugten virtuellen Energiesensor. Wird ausdrücklich ein bestehender
Energiesensor über `energy_sensor_id` referenziert, übernimmt dessen eigenes
Verhalten die Energiequelle. Dadurch entsteht keine automatische Subtraktion der
Energiezähler der Gruppenmitglieder.

## 1. Berechnungsmethoden

### 1.1 Subtrahieren: Erst Leistung subtrahieren, dann integrieren

Die Gruppenleistung ergibt sich aus den zuletzt bekannten Leistungswerten:

```text
P_Gruppe = P_Basis − Summe(P_Differenzgeräte)
```

Der automatisch angelegte Energiesensor integriert diese Gruppenleistung über die
Zeit:

```text
E_Gruppe = E_Start + Integral(P_Gruppe über die Zeit)
```

Beispiel: Bleibt die Gruppenleistung eine Stunde lang bei 600 W, ergibt das einen
Energiezuwachs von 0,6 kWh.

Powercalc verwendet dafür den Integrationssensor von Home Assistant. Die
Integrationsmethode ist konfigurierbar; die Voreinstellung ist die linke
Riemannsumme (`left`). Dabei wird für ein Zeitintervall die zuvor bekannte Leistung
angenommen. Andere Methoden gewichten die Intervallgrenzen anders.

Zusätzlich sind zeitgesteuerte Integrationsschritte vorgesehen. Sie rechnen bei
ausbleibenden Änderungen mit der zuletzt bekannten Leistung weiter. Solche Schritte
erzeugen keine zusätzlichen Messinformationen über zwischenzeitliche Lastwechsel.

Die Energiezähler der Geräte werden bei diesem Berechnungsweg nicht ausgewertet.
Auch vorhandene, hochwertige Hardware-Energiezähler verbessern diesen
Gruppenenergiewert daher nicht automatisch.

### 1.2 Differenzgruppe: Zuwächse der Energiezähler verrechnen

Die Gruppenleistung wird ebenfalls durch Subtraktion berechnet. Für die Energie
verwendet die Differenzgruppe jedoch die vorhandenen Energiezähler:

```text
Zuwachs eines Mitglieds = neuer Energiezählerstand − vorheriger Energiezählerstand

E_Gruppe_neu = E_Gruppe_alt
               + Zuwächse der Basis-Energiesensoren
               − Zuwächse der Differenz-Energiesensoren
```

Einheiten wie Wh und kWh werden vor der Verrechnung angeglichen. Die Zuwächse
werden bei den jeweiligen Sensormeldungen verarbeitet; es gibt bisher keine
gemeinsame zeitliche Abrechnung aller Mitglieder.

Der Anfangswert hängt von der gewählten Startart ab:

- **Start bei null:** Die zuerst erfassten Zählerstände bilden die Referenzen.
  Danach werden deren Zuwächse verrechnet.
- **Start bei null deaktiviert:** Der Anfangswert ergibt sich aus der aktuellen
  Energiedifferenz. Verfügbare Langzeitstatistiken werden beim erstmaligen Erstellen
  einmalig übernommen.

Ohne Zählerrücksetzungen und bei vollständig bekannten Referenzen entspricht die
fortlaufende Verrechnung einer Differenz der Gesamtzähler, gegebenenfalls mit
einem Offset für den Start bei null. Die Verarbeitung über Zuwächse ermöglicht die
Behandlung einzelner Zählerrücksetzungen.

### 1.3 Wann liefern beide Methoden dasselbe Ergebnis?

Für dieselben Zeiträume und dieselben physikalischen Messbereiche entspricht das
Integral einer Leistungsdifferenz mathematisch der Differenz der zugehörigen
Energiezuwächse.

In der Praxis unterscheiden sich aber Messfrequenz, Rundung, Meldeverzögerungen und
Ausfallzeiten. Außerdem kann ein Gerät Energie intern viel häufiger messen, als es
Leistungswerte an Home Assistant meldet. Deshalb können beide Gruppen im realen
Betrieb unterschiedliche Energieverbräuche liefern.

## 2. Home Assistant ist offline, während weiter Energie verbraucht wird

### Subtrahieren

Während Home Assistant ausgeschaltet ist, läuft dort keine Integration. Die
Leistungsverläufe dieser Zeit fehlen. Nach dem Neustart kann der bisherige
Gruppenzähler wiederhergestellt werden; aus einer aktuellen Leistungsaufnahme lässt
sich der Verbrauch während der Auszeit aber nicht zuverlässig bestimmen.

Ein vor der Auszeit bekannter Leistungswert und ein Wert danach verraten nicht,
wann Geräte dazwischen ein- oder ausgeschaltet wurden. Eine Fortschreibung über
diese Lücke wäre eine Annahme. Die ursprüngliche Subtrahieren-Gruppe liest die
Geräteenergiezähler nicht aus, um diese Lücke nachträglich auszugleichen.

### Differenzgruppe

Wenn die Geräte ihre kumulativen Energiezähler unabhängig von Home Assistant
weiterführen, enthalten die nächsten Zählerstände auch den Offline-Verbrauch.
Dieser kann gegenüber den gespeicherten Referenzständen nachgeliefert werden.

Beispiel für eine Auszeit:

| Größe | Zuwachs während der Auszeit |
| --- | ---: |
| Basisgerät | 1,0 kWh |
| Alle Differenzgeräte zusammen | 0,4 kWh |
| Restverbrauch der Gruppe | 0,6 kWh |

Die Differenzgruppe kann diese 0,6 kWh nach der Wiederverbindung erfassen, wenn alle
beteiligten Zählerstände und ihre gespeicherten Referenzen verfügbar sind. Die
einzelnen Nachmeldungen können zunächst wieder vorläufige Überhöhungen oder
Abnahmen auslösen.

Dieser Vorteil setzt voraus:

- Die Energiezähler laufen tatsächlich im Gerät oder in einem anderen unabhängig
  weiterlaufenden System.
- Während der Auszeit gehen keine benötigten Zählerstände durch Rücksetzungen oder
  einen Tageswechsel verloren.
- Der Gruppenwert und die Referenzstände werden konsistent wiederhergestellt.

Ein Energiesensor, der selbst erst in Home Assistant aus Leistung berechnet wird,
kann dieselbe Offline-Lücke besitzen. Die Differenzgruppe kann fehlende Messdaten
solcher Eingangssensoren nicht ergänzen.

### Gesamtsumme und zeitliche Zuordnung sind unterschiedlich

Ein nachgelieferter Gesamtzuwachs sagt noch nicht, in welcher Stunde der Auszeit
wie viel verbraucht wurde. Im laufenden Betrieb erfolgt keine automatische
nachträgliche Verteilung auf die ausgefallenen Stunden. In der Gruppenhistorie
kann der Zuwachs erst bei der Wiederverbindung sichtbar werden.

Für eine genaue Verteilung auf Stunden oder Tage wären zusätzliche historische
Messdaten erforderlich. Die einmalige Statistikübernahme beim Erstellen der Gruppe
ist kein allgemeiner Mechanismus zur Reparatur späterer Ausfallzeiträume.

## 3. Unterschiedliche Meldezyklen der Energiezähler

### Subtrahieren

Ob die Geräte ihre Energiezähler alle zehn Sekunden oder nur alle fünf Minuten
melden, beeinflusst die normale Integralberechnung dieser Gruppe nicht. Sie
verwendet die Leistungswerte.

### Differenzgruppe

Die Zuwächse treffen nicht gleichzeitig ein. Die Differenz zweier steigender
Zähler muss deshalb selbst nicht bei jeder einzelnen Meldung steigen.

Beispiel mit einem Differenzgerät, alle Werte in kWh:

| Ereignis | Basis | Differenzgerät | Gruppenenergie |
| --- | ---: | ---: | ---: |
| Ausgangszustand | 100,0 | 40,0 | 60,0 |
| Differenzgerät meldet zuerst | 100,0 | 40,1 | 59,9 |
| Basis meldet später | 100,2 | 40,1 | 60,1 |

Meldet dagegen die Basis zuerst, steigt der Gruppenwert vorläufig von 60,0 auf
60,2 und fällt nach der Differenzmeldung auf 60,1.

Keine dieser Abnahmen erfordert einen sinkenden Eingangszähler. Unterschiedliche
Auflösungen verstärken den Effekt: Ein grob auflösender Basiszähler kann längere
Zeit unverändert bleiben, während feinere Differenzzähler bereits Zuwächse melden.

Sind alle Zuwächse später vollständig angekommen, ergibt die laufende Verrechnung
die entsprechende Gesamtdifferenz. Der zwischenzeitliche Verlauf und seine
Zuordnung zu Statistikzeiträumen können trotzdem unruhig oder verzerrt sein.
Ein selteneres Veröffentlichen des Gruppenwertes kann das teilweise verdecken,
gewährleistet aber keine Synchronisation.

## 4. Unterschiedliche Meldezyklen der Leistungssensoren

Beide Gruppen berechnen ihre Leistung aus den jeweils zuletzt bekannten Werten.
Dadurch kann auch die Gruppenleistung kurzzeitig zu hoch, zu niedrig oder negativ
sein, wenn die Sensoren unterschiedliche Zeitpunkte abbilden.

Bei **Subtrahieren** fließen solche Zwischenzustände in die Integration ein. Je
länger eine vorläufig falsche Leistungsdifferenz stehen bleibt, desto größer kann
ihr Beitrag zum Energiefehler werden. Auch selten gemeldete Lastwechsel oder kurze,
gar nicht gemeldete Lastspitzen begrenzen die Genauigkeit.

Bei der **Differenzgruppe** hängt die Gruppenenergie unmittelbar an den
Energiezählern. Ein kurzzeitig falscher Gruppenleistungswert verändert diesen
Energiewert nicht. Die Qualität der Eingangszähler ist dafür entscheidend.

Aus diesem Grund müssen die angezeigte Gruppenleistung und ein unmittelbar
beobachteter Energiezuwachs nicht jederzeit exakt zueinander passen.

## 5. Verfügbarkeit, Rücksetzungen und Messfehler

### Fehlende oder veraltete Eingangswerte

Bei **Subtrahieren** ist die Basis zwingend: Fehlt ihr Leistungswert, wird die
Gruppenleistung nicht verfügbar. Fehlende abzuziehende Leistungswerte behandelt
die ursprüngliche Implementierung dagegen als null. Dadurch kann sie vorübergehend
zu viel Leistung ausweisen und zu viel Energie integrieren. Ein veralteter, noch
numerischer Leistungswert kann ebenfalls fälschlich weiterverwendet werden.

Bei der **Differenzgruppe** wird eine nicht verfügbare Energiemeldung nicht als
Zählerstand null abgezogen. Ihr Energiebeitrag wartet auf einen gültigen Wert;
andere Mitglieder können den Gruppenwert währenddessen weiterhin verändern. Ein
weitergelaufener Zähler kann später nachliefern. Die aktuelle Implementierung
wartet dabei noch nicht auf einen vollständig abgestimmten Satz aller Mitglieder.

Die Leistungssensoren der Differenzgruppe können ebenfalls mit unvollständigen
Mitgliederwerten rechnen. Die Nutzung von Energiezählern macht die Anzeige der
Gruppenleistung nicht automatisch vollständig.

### Zählerrücksetzungen

Die Differenzgruppe übernimmt die Behandlung aus der Standardgruppe: Sinkt ein
Mitgliederzähler, wird dieser negative Zuwachs ignoriert. Der neue Stand dient als
Referenz für die folgenden Zuwächse.

Das verhindert einen künstlichen Sprung durch das Subtrahieren zurückgesetzter
Gesamtstände. Es rekonstruiert aber keine verlorene Energie: Verbrauch zwischen
letzter Meldung, Rücksetzung und nächster Meldung kann teilweise fehlen. Geschieht
die Rücksetzung während einer Auszeit und ist der neue Stand später bereits wieder
höher als der alte, lässt sich die Rücksetzung am Zählerstand allein unter Umständen
gar nicht erkennen.

Rücksetzungen der Geräteenergiezähler beeinflussen die normale Energieintegration
von **Subtrahieren** nicht unmittelbar. Dort bleiben fehlende oder falsche
Leistungswerte die entscheidende Fehlerquelle.

### Messbereiche und mehrfache Erfassung

Die Differenzgruppe nimmt die passenden Sensoren der ausgewählten Geräte auf.
Hat ein Gerät mehrere geeignete Energie- oder Leistungssensoren, werden diese
kombiniert. Ein Gesamtzähler zusätzlich zu seinen bereits enthaltenen Einzelkanälen
kann dadurch zu einer doppelten Erfassung führen. In der implementierten Auswahl
werden Utility-Meter- und Powercalc-Standby-Energiesensoren ausgeschlossen; dennoch
müssen die verbleibenden Messbereiche fachlich zusammenpassen.

Bei **Subtrahieren** lassen sich einzelne Leistungsentitäten gezielt auswählen.
Auch hier können überlappende Messbereiche doppelt abgezogen werden.

Für einen nichtnegativen Restverbrauch muss die Basis die abzuziehenden Verbraucher
physikalisch umfassen. Werden größere oder andere Messbereiche abgezogen, kann eine
negative Differenz sachlich richtig sein. Unterschiedliche Messabweichungen fallen
besonders ins Gewicht, wenn zwei große, fast gleiche Mengen voneinander abgezogen
werden.

### Können die Energiewerte beider Gruppen sinken?

- **Differenzgruppe, aktueller Stand:** Ja, durch zeitversetzte Meldungen oder durch
  einen tatsächlich größeren Zuwachs der abzuziehenden Zähler.
- **Subtrahieren:** Bei durchgehend nichtnegativer Gruppenleistung liefert die
  Integration nichtnegative Beiträge. Negative Gruppenleistung kann jedoch auch
  einen negativen Energiebeitrag erzeugen. Eine solche Phase kann durch echte
  Verhältnisse oder durch zeitversetzte Leistungswerte entstehen. Die Einstufung
  eines Sensors als steigender Zähler ist für sich kein numerischer Schutz gegen
  solche Beiträge.

## 6. Anfangswert und Langzeitstatistiken

Die ursprüngliche Gruppe **Subtrahieren** übernimmt beim Erstellen keine
Langzeitstatistiken ihrer Mitglieder. Ihr virtueller Energiezähler beginnt eine
eigene Integration und kann diesen selbst aufgebauten Stand später wiederherstellen.
Das Wiederherstellen des eigenen Zählerstands ist etwas anderes als das erstmalige
Übernehmen einer Mitgliederhistorie.

Die **Differenzgruppe** unterstützt den Start bei null oder die einmalige Übernahme
der aktuellen Energiedifferenz und verfügbarer Langzeitstatistiken. Die Historien
werden nach Einheitenumrechnung voneinander abgezogen. Bei Neustarts oder erneutem
Laden wird diese Übernahme nicht wiederholt.

Die Statistikübernahme benötigt aufgezeichnete Eingangsdaten. Fehlende Historie
kann sie nicht erzeugen. Auch historische Differenzen können durch unterschiedliche
Meldezyklen und fehlende Messpunkte zeitweise sinken.

Beim Übernehmen aktueller Gesamtstände sind zudem die Zählursprünge wichtig:
Zähler seit unterschiedlichen Installations- oder Rücksetzzeitpunkten bilden nicht
automatisch einen gemeinsamen Abrechnungszeitraum. Der Start bei null vermeidet
die Übernahme solcher alten Offsets, benötigt aber weiterhin sinnvolle
Anfangsreferenzen und passende Messbereiche.

## 7. Geplante Absicherung der Differenzgruppe

Das separat erarbeitete Konzept soll zeitversetzte Meldungen abfangen und einen
monotonen veröffentlichten Gruppenenergiewert ermöglichen. Es ist noch nicht
implementiert.

Die vorgesehenen Schritte sind:

1. Zählerstände und möglichst echte Messzeitpunkte puffern.
2. Einen Basisstand erst verrechnen, wenn alle Differenzgeräte den betreffenden
   Messzeitpunkt mit einem gleichzeitigen oder späteren Wert abdecken.
3. Sämtliche Beiträge und negativen Korrekturen in einem dauerhaften internen Saldo
   erhalten.
4. Den veröffentlichten Gruppenwert nur erhöhen. Liegt der Saldo darunter, bleibt
   die Anzeige stehen, bis weitere Zuwächse die Differenz ausgeglichen haben.
5. Saldo, Referenzen und ausstehende Verrechnungen konsistent über Neustarts erhalten.

Damit würden auch vorzeitige Überhöhungen bei einer zuerst meldenden Basis
vermieden. Nur den bisherigen Höchstwert festzuhalten, ohne Meldungen zeitlich
abzusichern und Korrekturen zu erhalten, reicht dafür nicht aus.

Der Preis ist eine verzögerte Anzeige. Bleibt ein benötigtes Gerät ohne gültige
Meldung, muss die abgesicherte Verrechnung warten. Auch unveränderte, aber gültig
erneut gemeldete Zählerstände müssen deshalb berücksichtigt werden.

Verlässliche Messzeitpunkte und passende Messbereiche sind für die Genauigkeit
entscheidend. Empfangszeitpunkte in Home Assistant erlauben nur eine angenäherte
Zuordnung. Eine feste Wartezeit bietet bei beliebigen Meldeverzögerungen keine
allgemeine Absicherung. Die Behandlung der übernommenen Historie und des Übergangs
zum Livebetrieb muss bei der Umsetzung ebenfalls an diese Regeln angepasst werden.

## 8. Geeignete Einsatzfälle

**Subtrahieren eignet sich**, wenn nur Leistungssensoren verfügbar sind oder die
Leistung aus Gerätezuständen und Profilen geschätzt wird. Die Auswahl einzelner
Entitäten ist direkt steuerbar. Für brauchbare Energiewerte braucht Home Assistant
dabei ausreichend vollständige und zeitnahe Leistungsinformationen.

**Die Differenzgruppe eignet sich**, wenn verlässliche kumulative Energiezähler
vorhanden sind. Besonders vorteilhaft sind unabhängig weiterlaufende Hardwarezähler:
Sie können Verbrauch auch über Kommunikations- und Home-Assistant-Auszeiten hinweg
bewahren. Im implementierten Stand müssen vorläufige Rückgänge durch asynchrone
Energiemeldungen berücksichtigt werden; das geplante Verfahren soll diese gegen
eine verzögerte Anzeige eintauschen.

## Codegrundlagen

Für die Aussagen wurden insbesondere folgende Implementierungen gelesen:

- `custom_components/powercalc/sensors/group/subtract.py`:
  `create_subtract_group_sensors` und `SubtractGroupSensor.get_summed_state`.
- `custom_components/powercalc/sensors/energy.py`:
  `create_energy_sensor`, `_create_virtual_energy_sensor` und `VirtualEnergySensor`.
- `custom_components/powercalc/sensors/group/difference.py` im genannten
  Entwicklungsstand: Geräteauswahl und Gewichtung mit `+1` beziehungsweise `−1`.
- `custom_components/powercalc/sensors/group/custom.py` im genannten
  Entwicklungsstand: `create_grouped_energy_sensor`, `GroupedEnergySensor`,
  `calculate_delta` und `PreviousStateStore`.
- `custom_components/powercalc/sensors/group/energy_statistics.py` im genannten
  Entwicklungsstand: einmalige Übernahme der Mitgliederstatistiken.
- Der lokal installierte Home-Assistant-Integrationssensor:
  Zustandswiederherstellung, Integration bei Meldungen und zeitgesteuerte
  Fortschreibung mit der zuletzt bekannten Leistung.
