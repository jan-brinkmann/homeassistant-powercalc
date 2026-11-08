"""Counter offsets must not turn missing history into a different initial reading."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.powercalc.const import (
    CONF_ENERGY_SENSOR_UNIT_PREFIX,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_ENERGY_UPDATE_INTERVAL,
    CONF_GROUP_TYPE,
    DOMAIN,
    GroupType,
    UnitPrefix,
)
from custom_components.powercalc.sensors.group.history import HistoryImportError
from custom_components.powercalc.sensors.group.history_import import GroupHistoryImport, ImportJournal, ImportPhase
from custom_components.powercalc.sensors.group.history_recorder import HistoryRecorder
from tests.common import create_mock_group_entry
from tests.sensors.group.test_history_recorder import mock_recorder_before_hass as mock_recorder_before_hass


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "gaps,reset,interrupted,bridge_minutes",
    [
        pytest.param(False, False, False, 0, id="complete-history"),
        pytest.param(True, False, False, 0, id="gaps"),
        pytest.param(False, True, False, 0, id="reset"),
        pytest.param(True, True, False, 0, id="gaps-and-reset"),
        pytest.param(False, False, True, 0, id="interruption"),
        pytest.param(True, False, True, 0, id="gaps-and-interruption"),
        pytest.param(False, True, True, 0, id="reset-and-interruption"),
        pytest.param(True, True, True, 0, id="gaps-reset-and-interruption"),
        pytest.param(True, True, True, 15, id="gaps-reset-interruption-and-bridge"),
    ],
)
async def test_initial_counter_matches_group_without_history(
    hass: HomeAssistant,
    recorder_mock: Recorder,
    freezer: FrozenDateTimeFactory,
    gaps: bool,
    reset: bool,
    interrupted: bool,
    bridge_minutes: int,
) -> None:
    """Compare actual groups, including units, missing hours and an open-interval reset."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_start()
    boundary = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    attributes = {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    a_attributes = {**attributes, "state_class": "total_increasing" if reset else "total"}
    b_attributes = {**attributes, "unit_of_measurement": "Wh"}
    a_values = [100, 100.3, None, 200, 200.2] if gaps else [100, 100.3, 100.5, 100.7, 100.9]
    a_last = 400.2 if gaps else 300.9
    freezer.move_to(boundary - timedelta(minutes=1))
    for entity_id, unit, values, offset in [
        ["sensor.a", "kWh", a_values, 200],
        ["sensor.b", "Wh", [2000, 2200, 2400, 2600, 2800], 8000],
    ]:
        source = HistoryRecorder(hass, entity_id, unit)
        await source.import_batch(
            [
                {
                    "start": (boundary - timedelta(hours=5 - index)).timestamp(),
                    "sum": value,
                    "state": value + offset,
                }
                for index, value in enumerate(values)
                if value is not None
            ],
            "hour",
        )
        await source.import_batch(
            [{"start": boundary.timestamp() - 300, "sum": values[-1], "state": values[-1] + offset}], "5minute"
        )
    hass.states.async_set("sensor.a", str(a_last), a_attributes)
    hass.states.async_set("sensor.b", "10800", b_attributes)
    await async_wait_recording_done(hass)
    freezer.move_to(boundary + timedelta(minutes=1))
    a_current = 0.1 if reset else a_last + 0.1
    hass.states.async_set("sensor.a", str(a_current), a_attributes)
    hass.states.async_set("sensor.b", "10900", b_attributes)
    await async_wait_recording_done(hass)
    for minute in range(5, bridge_minutes + 1, 5):
        freezer.move_to(boundary + timedelta(minutes=minute))
        do_adhoc_statistics(hass, start=boundary + timedelta(minutes=minute - 5))
        await async_wait_recording_done(hass)
    live_boundary = boundary + timedelta(minutes=bridge_minutes)
    freezer.move_to(live_boundary + timedelta(minutes=2))
    config = {
        CONF_GROUP_TYPE: GroupType.CUSTOM,
        CONF_GROUP_ENERGY_ENTITIES: ["sensor.a", "sensor.b"],
        CONF_GROUP_ENERGY_START_AT_ZERO: False,
        CONF_GROUP_ENERGY_UPDATE_INTERVAL: 0,
        CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
    }
    await create_mock_group_entry(hass, "Without history", config)
    target_id = "sensor.with_history_energy"
    save = GroupHistoryImport.save

    async def interrupt_after_origin(job: GroupHistoryImport) -> None:
        await save(job)
        if interrupted and job.journal.native_offset is not None and not job.journal.short_term_seeded:
            raise asyncio.CancelledError

    with patch.object(GroupHistoryImport, "save", interrupt_after_origin):
        entry = await create_mock_group_entry(hass, "With history", {**config, CONF_GROUP_ENERGY_IMPORT_HISTORY: True})
        await hass.async_block_till_done(wait_background_tasks=True)
    if interrupted:
        assert hass.states.get(target_id).state == "unknown"
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    current = hass.states.get(target_id)
    assert current.attributes["history_import_status"] == "completed"
    assert float(current.state) == pytest.approx(a_current + 10.9, rel=0, abs=1e-8)
    assert float(current.state) == pytest.approx(float(hass.states.get("sensor.without_history_energy").state))
    assert current.attributes["history_incomplete_intervals"] == (2 if gaps else 0)
    warnings = [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key == "group_history_partial"
    ]
    assert len(warnings) == (1 if gaps else 0)
    assert all(issue.severity == ir.IssueSeverity.WARNING for issue in warnings)
    target = HistoryRecorder(hass, target_id, "kWh")
    rows = await target.read([target_id], boundary - timedelta(hours=5), boundary)
    assert [row["sum"] for row in rows[target_id]] == pytest.approx(
        [0, 0.5, 0.7, 0.9, 1.3] if gaps else [0, 0.5, 0.9, 1.3, 1.7]
    )
    initial_consumption = 1.5 if gaps else 1.9
    native_offset = a_current + 10.9 - initial_consumption
    assert [row["state"] for row in rows[target_id]] == pytest.approx(
        [row["sum"] + native_offset for row in rows[target_id]]
    )
    freezer.move_to(live_boundary + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=live_boundary)
    await async_wait_recording_done(hass)
    freezer.move_to(live_boundary + timedelta(minutes=6))
    hass.states.async_set("sensor.a", str(a_current + 0.2), a_attributes)
    hass.states.async_set("sensor.b", "11100", b_attributes)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    with patch.object(HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected reimport")):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get(target_id).state) == pytest.approx(a_current + 11.3)
    assert float(hass.states.get(target_id).state) == pytest.approx(
        float(hass.states.get("sensor.without_history_energy").state)
    )
    freezer.move_to(live_boundary + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=live_boundary + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    rows = await target.read([target_id], live_boundary, live_boundary + timedelta(minutes=10), "5minute")
    assert [row["sum"] for row in rows[target_id]] == pytest.approx([initial_consumption, initial_consumption + 0.4])
    assert [row["state"] for row in rows[target_id]] == pytest.approx([a_current + 10.9, a_current + 11.3])


async def test_short_term_import_requires_a_counter_origin(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    await hass.async_start()
    job = GroupHistoryImport(hass, "no_origin", "sensor.target", "kWh")
    job.journal = ImportJournal("sensor.target", "kWh", ["sensor.source"], 0, phase=ImportPhase.SHORT_TERM)
    with pytest.raises(HistoryImportError, match="counter reference"):
        await job.import_short_term(dt_util.utcnow())
    await job.recorder.require_empty_target()
