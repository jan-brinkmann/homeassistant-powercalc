# Code Review / Code-Review

## English

The patch introduces incorrect cost accounting for signed difference totals and history initialization paths that can omit or double-count energy. Translation-key parity also fails.

### [P1] Handle signed deltas in difference-group cost sensors

Location: `custom_components/powercalc/sensors/group/difference.py:95`

When cost sensors are enabled, this creates a `CostSensor` for an energy counter that legitimately decreases. However, `CostSensor._accumulate` treats decreases as resets and substitutes the entire new counter value for the delta. A change from 100 to 99 kWh at €0.30/kWh therefore adds €29.70 despite consumption decreasing. Add signed-delta handling before creating these cost sensors; utility-meter cost sensors also misinterpret normal decreases as cycle resets.

### [P2] Wait for complete member readings before seeding history

Location: `custom_components/powercalc/sensors/group/energy_statistics.py:193-195`

If one member is unavailable at creation, `_get_current_values` returns `None`, but initialization still imports its history against the group's partial current total. For example, A's historical state/sum of 100/50 kWh and B's 10/5 kWh produce a seed with state 10 and sum 55. When A returns unchanged at 100, `calculate_delta` adds its entire counter and recorder adds another 100 kWh to the imported consumption. Defer seeding until member readings and baselines can be aligned.

### [P2] Resolve every power member for integral-group history

Location: `custom_components/powercalc/sensors/group/energy_statistics.py:241-242`

For a custom group with `force_calculate_group_energy` enabled, any nonempty energy selection bypasses power-to-energy resolution here. If the group integrates power from A and B but its selected energy entities contain only A, the new sensor inherits only A's total and history while subsequently integrating both devices. Resolve all power members for integral sensors, rather than assuming the independently configured energy selection is complete.

### [P2] Avoid multiplying shared device-level energy histories

Location: `custom_components/powercalc/sensors/group/energy_statistics.py:253`

In a power-only custom group containing several phase power sensors on one device, the registry fallback can resolve every phase to the same device-level energy meter. Incrementing its weight makes a three-phase group inherit three times that meter's total and history, although subsequent integration sums the phases only once. Deduplicate shared device-level histories or reject ambiguous repeated associations instead of multiplying them.

### [P2] Add the new translation keys to every locale

Location: `custom_components/powercalc/translations/en.json:26-29`

The difference-group keys were added only to English and German, leaving 25 missing leaf keys in each of the other 17 locales. This fails `tests/test_translations.py::test_translations_keep_english_keys`. Add the corresponding entries to every remaining locale, as required by the [translation parity rule](.agents/skills/powercalc_translations/SKILL.md#L27).

## Deutsch

Der Patch führt zu fehlerhafter Kostenberechnung bei Differenzgruppen. Außerdem kann die Initialisierung aus historischen Daten Energie auslassen oder mehrfach zählen. Die Übersetzungsschlüssel sind nicht in allen Sprachen vollständig.

### [P1] Vorzeichenbehaftete Änderungen bei Kostensensoren von Differenzgruppen berücksichtigen

Fundstelle: `custom_components/powercalc/sensors/group/difference.py:95`

Hier wird bei aktivierten Kostensensoren ein `CostSensor` für einen Energiezähler erstellt, dessen Wert regulär sinken kann. `CostSensor._accumulate` interpretiert sinkende Werte jedoch als Zurücksetzen des Zählers und verwendet den gesamten neuen Zählerstand als Verbrauchsdifferenz. Ein Rückgang von 100 auf 99 kWh bei 0,30 €/kWh erhöht die Kosten dadurch um 29,70 €. Vor dem Erstellen dieser Kostensensoren muss die Verarbeitung vorzeichenbehafteter Differenzen unterstützt werden. Auch die Kostensensoren für Verbrauchszähler interpretieren normale Rückgänge fälschlich als Beginn eines neuen Abrechnungszyklus.

### [P2] Vor dem Initialisieren der Historie auf vollständige Messwerte aller Gruppenmitglieder warten

Fundstelle: `custom_components/powercalc/sensors/group/energy_statistics.py:193-195`

Ist ein Mitglied beim Erstellen nicht verfügbar, liefert `_get_current_values` den Wert `None`. Trotzdem importiert die Initialisierung dessen Historie auf Grundlage des unvollständigen aktuellen Gruppenwerts. Beispiel: A hat einen historischen Zustand/Summenwert von 100/50 kWh, B von 10/5 kWh. Daraus entsteht ein Startwert mit Zustand 10 und Summe 55. Sobald A unverändert mit 100 kWh zurückkehrt, addiert `calculate_delta` dessen gesamten Zählerstand. Der Recorder zählt dadurch weitere 100 kWh zum bereits importierten Verbrauch hinzu. Die Initialisierung muss warten, bis die Messwerte und Ausgangswerte aller Mitglieder aufeinander abgestimmt werden können.

### [P2] Für die Historie integrierender Gruppen alle Leistungsmitglieder auflösen

Fundstelle: `custom_components/powercalc/sensors/group/energy_statistics.py:241-242`

Bei einer benutzerdefinierten Gruppe mit aktiviertem `force_calculate_group_energy` wird die Zuordnung von Leistungs- zu Energiesensoren übersprungen, sobald irgendeine Energieauswahl vorhanden ist. Integriert die Gruppe beispielsweise die Leistung von A und B, enthält ihre Energieauswahl aber nur A, übernimmt der neue Sensor lediglich den Gesamtwert und die Historie von A. Anschließend integriert er dennoch beide Geräte. Für integrierende Sensoren müssen deshalb alle Leistungsmitglieder aufgelöst werden; die unabhängig konfigurierte Energieauswahl kann unvollständig sein.

### [P2] Gemeinsam verwendete Energiehistorien auf Geräteebene nicht mehrfach zählen

Fundstelle: `custom_components/powercalc/sensors/group/energy_statistics.py:253`

Enthält eine benutzerdefinierte Gruppe ausschließlich mehrere Phasen-Leistungssensoren desselben Geräts, kann die Registry-Ersatzsuche jede Phase demselben Energiezähler des Geräts zuordnen. Durch das Erhöhen seiner Gewichtung übernimmt eine dreiphasige Gruppe den Gesamtwert und die Historie dieses Zählers dreifach, obwohl die anschließende Integration die Phasen nur einmal aufsummiert. Gemeinsam verwendete Energiehistorien müssen dedupliziert oder mehrdeutige Mehrfachzuordnungen abgelehnt werden.

### [P2] Neue Übersetzungsschlüssel in allen Sprachdateien ergänzen

Fundstelle: `custom_components/powercalc/translations/en.json:26-29`

Die Schlüssel für Differenzgruppen wurden nur auf Englisch und Deutsch ergänzt. In jeder der übrigen 17 Sprachdateien fehlen dadurch 25 Schlüssel auf Blattebene. Deshalb schlägt `tests/test_translations.py::test_translations_keep_english_keys` fehl. Die entsprechenden Einträge müssen gemäß der [Regel zur Vollständigkeit der Übersetzungsschlüssel](.agents/skills/powercalc_translations/SKILL.md#L27) in allen übrigen Sprachdateien ergänzt werden.
