"""Domain selection and signed energy sources for group history imports."""

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.components.recorder import Recorder
from homeassistant.const import CONF_DOMAIN, CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.powercalc.const import (
    CONF_ENERGY_SENSOR_UNIT_PREFIX,
    CONF_EXCLUDE_ENTITIES,
    CONF_GROUP_ENERGY_BASE_ENTITY,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_ENERGY_SUBTRACT_ENTITIES,
    CONF_GROUP_TYPE,
    CONF_SUB_GROUPS,
    CONF_SUBTRACT_ENTITIES,
    DATA_CONFIGURED_ENTITIES,
    DATA_DOMAIN_ENTITIES,
    DOMAIN,
    ENTRY_DATA_ENERGY_ENTITY,
    GroupType,
    UnitPrefix,
)
from custom_components.powercalc.sensors.energy import RealEnergySensor
from custom_components.powercalc.sensors.group.history import AggregationState, HistoryImportError, aggregate_statistics
from custom_components.powercalc.sensors.group.history_import import ImportJournal
from custom_components.powercalc.sensors.group.history_live import apply_source_state
from custom_components.powercalc.sensors.group.history_recorder import HistoryRecorder
from custom_components.powercalc.sensors.group.history_sources import (
    find_energy_counter,
    resolve_history_source_weights,
)
from custom_components.powercalc.sensors.power import RealPowerSensor
from tests.common import create_mock_group_entry, mock_entities_in_registry, run_powercalc_setup
from tests.sensors.group.test_history_recorder import mock_recorder_before_hass as mock_recorder_before_hass


@pytest.mark.parametrize(
    "last_base,last_part,expected", [(100.3, 900.2, 0.1), (100.1, 900.3, -0.2), (99.9, 899.8, 0.1)]
)
def test_signed_consumption_keeps_corrections(last_base: float, last_part: float, expected: float) -> None:
    rows = {
        "sensor.base": [{"start": 0.0, "sum": 100.0}, {"start": 3600.0, "sum": last_base}],
        "sensor.part": [{"start": 0.0, "sum": 900.0}, {"start": 3600.0, "sum": last_part}],
    }
    result = aggregate_statistics(
        list(rows), rows, AggregationState(), source_weights={"sensor.base": 1, "sensor.part": -1}
    )
    assert [row["sum"] for row in result] == pytest.approx([0, expected])


def test_live_sign_change_establishes_a_baseline_at_the_same_timestamp() -> None:
    journal = ImportJournal("sensor.target", "kWh", ["sensor.part"], 0)
    attributes = {"unit_of_measurement": "kWh", "state_class": "total"}
    now = dt_util.utcnow()
    apply_source_state(journal, State("sensor.part", "100", attributes, last_updated=now))
    apply_source_state(journal, State("sensor.part", "100", attributes, last_updated=now), -1)
    apply_source_state(journal, State("sensor.part", "100.2", attributes, last_updated=now + timedelta(seconds=1)), -1)
    assert journal.live_total == pytest.approx(-0.2)


async def test_subtracted_subgroups_cancel_shared_sources(hass: HomeAssistant) -> None:
    await create_mock_group_entry(
        hass,
        "Child",
        {
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.a", "sensor.b"],
            ENTRY_DATA_ENERGY_ENTITY: "sensor.child_energy",
        },
        setup=False,
    )
    subtract = await create_mock_group_entry(
        hass,
        "Remainder",
        {
            CONF_GROUP_TYPE: GroupType.SUBTRACT,
            CONF_ENTITY_ID: "sensor.main_power",
            CONF_SUBTRACT_ENTITIES: ["sensor.part_power"],
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.child_energy",
            CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.a"],
            ENTRY_DATA_ENERGY_ENTITY: "sensor.remainder_energy",
        },
        setup=False,
    )
    assert await resolve_history_source_weights(hass, dict(subtract.data), subtract) == {"sensor.b": 1}
    parent = await create_mock_group_entry(
        hass,
        "Parent",
        {
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_SUB_GROUPS: [subtract.entry_id],
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.b"],
        },
        setup=False,
    )
    assert await resolve_history_source_weights(hass, dict(parent.data), parent) == {"sensor.b": 1}


async def test_automatic_counter_mapping_uses_configured_powercalc_entities(hass: HomeAssistant) -> None:
    await run_powercalc_setup(hass)
    power = RealPowerSensor("sensor.power")
    energy = RealEnergySensor("sensor.energy")
    hass.data[DOMAIN][DATA_CONFIGURED_ENTITIES]["light.source"] = [(power, False), (energy, False)]
    assert find_energy_counter(hass, "sensor.power") == "sensor.energy"
    with pytest.raises(HistoryImportError, match="explicitly"):
        find_energy_counter(hass, "sensor.unmapped_power")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("group_kind", ["domain", "all", "subtract", "subtract_auto"])
async def test_group_history_and_recorder_continuation(
    hass: HomeAssistant, recorder_mock: Recorder, group_kind: str
) -> None:
    assert await async_setup_component(hass, "sensor", {})
    await run_powercalc_setup(hass)
    await hass.async_start()
    boundary = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    attributes = {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    power_a = RealPowerSensor("sensor.a_power")
    power_b = RealPowerSensor("sensor.b_power")
    energy_a = RealEnergySensor("sensor.a_energy")
    energy_b = RealEnergySensor("sensor.b_energy")
    hass.data[DOMAIN][DATA_DOMAIN_ENTITIES]["light"] = [power_a, power_b, energy_a, energy_b]
    hass.data[DOMAIN][DATA_CONFIGURED_ENTITIES].update(
        {
            "light.a": [(power_a, False), (energy_a, False)],
            "light.b": [(power_b, False), (energy_b, False)],
        }
    )
    if group_kind == "all":
        mock_entities_in_registry(
            hass,
            {
                "sensor.a_energy": {"device_class": "energy"},
                "sensor.b_energy": {"device_class": "energy"},
            },
        )
    with freeze_time(boundary - timedelta(minutes=1), real_asyncio=True) as freezer:
        for source, initial_sum, initial_state, step in [
            ["sensor.a_energy", 100.0, 300.0, 0.3],
            ["sensor.b_energy", 500.0, 900.0, 0.2],
        ]:
            hass.states.async_set(source, str(initial_state + 2 * step), attributes)
            recorder = HistoryRecorder(hass, source, "kWh")
            await recorder.import_batch(
                [
                    {
                        "start": (boundary - timedelta(hours=3 - i)).timestamp(),
                        "sum": initial_sum + i * step,
                        "state": initial_state + i * step,
                    }
                    for i in range(3)
                ],
                "hour",
            )
            await recorder.import_batch(
                [
                    {
                        "start": boundary.timestamp() - 300,
                        "sum": initial_sum + 2 * step,
                        "state": initial_state + 2 * step,
                    }
                ],
                "5minute",
            )
        freezer.move_to(boundary + timedelta(minutes=1))
        hass.states.async_set("sensor.a_energy", "300.7", attributes)
        hass.states.async_set("sensor.b_energy", "900.5", attributes)
        await async_wait_recording_done(hass)
        freezer.move_to(boundary + timedelta(minutes=2))
        subtracting = group_kind.startswith("subtract")
        config = {
            CONF_GROUP_TYPE: GroupType.SUBTRACT if subtracting else GroupType.DOMAIN,
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_GROUP_ENERGY_START_AT_ZERO: False,
            CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
        }
        if subtracting:
            config.update({CONF_ENTITY_ID: "sensor.a_power", CONF_SUBTRACT_ENTITIES: ["sensor.b_power"]})
            if group_kind == "subtract":
                config.update(
                    {
                        CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.a_energy",
                        CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.b_energy"],
                    }
                )
        else:
            config[CONF_DOMAIN] = "all" if group_kind == "all" else "light"
        entry = await create_mock_group_entry(hass, "History types", config)
        await hass.async_block_till_done(wait_background_tasks=True)
        target = "sensor.history_types_energy"
        expected = 0.2 if subtracting else 1.2
        native_offset = -600 if subtracting else 1200
        state = hass.states.get(target)
        assert state.attributes["history_import_status"] == "completed"
        assert float(state.state) == pytest.approx(native_offset + expected)
        rows = await HistoryRecorder(hass, target, "kWh").read([target], boundary - timedelta(hours=3), boundary)
        assert [row["sum"] for row in rows[target]] == pytest.approx([0, 0.1, 0.2] if subtracting else [0, 0.5, 1])
        assert [row["state"] for row in rows[target]] == pytest.approx(
            [row["sum"] + native_offset for row in rows[target]]
        )
        freezer.move_to(boundary + timedelta(minutes=5))
        do_adhoc_statistics(hass, start=boundary)
        await async_wait_recording_done(hass)
        freezer.move_to(boundary + timedelta(minutes=6))
        hass.states.async_set("sensor.a_energy", "300.8", attributes)
        hass.states.async_set("sensor.b_energy", "900.8", attributes)
        await hass.async_block_till_done()
        expected_after = 0 if subtracting else 1.6
        assert float(hass.states.get(target).state) == pytest.approx(native_offset + expected_after)
        with patch.object(HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected history reimport")):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
        assert float(hass.states.get(target).state) == pytest.approx(native_offset + expected_after)
        freezer.move_to(boundary + timedelta(minutes=10))
        do_adhoc_statistics(hass, start=boundary + timedelta(minutes=5))
        await async_wait_recording_done(hass)
        rows = await HistoryRecorder(hass, target, "kWh").read(
            [target], boundary, boundary + timedelta(minutes=10), "5minute"
        )
        assert [row["sum"] for row in rows[target]] == pytest.approx([expected, expected_after])
        freezer.move_to(boundary + timedelta(minutes=11))
        changes = (
            {
                CONF_ENTITY_ID: "sensor.b_power",
                CONF_SUBTRACT_ENTITIES: ["sensor.a_power"],
                CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.b_energy",
                CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.a_energy"],
            }
            if subtracting
            else {CONF_EXCLUDE_ENTITIES: ["sensor.a_energy"]}
        )
        with patch.object(HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected history reimport")):
            hass.config_entries.async_update_entry(entry, data={**entry.data, **changes})
            await hass.async_block_till_done(wait_background_tasks=True)
        assert float(hass.states.get(target).state) == pytest.approx(native_offset + expected_after)
        freezer.move_to(boundary + timedelta(minutes=12))
        hass.states.async_set("sensor.a_energy", "300.9", attributes)
        hass.states.async_set("sensor.b_energy", "900.85", attributes)
        await hass.async_block_till_done()
        assert float(hass.states.get(target).state) == pytest.approx(native_offset + (-0.05 if subtracting else 1.65))
        await async_wait_recording_done(hass)
        freezer.move_to(boundary + timedelta(minutes=15))
        do_adhoc_statistics(hass, start=boundary + timedelta(minutes=10))
        await async_wait_recording_done(hass)
        rows = await HistoryRecorder(hass, target, "kWh").read(
            [target], boundary, boundary + timedelta(minutes=15), "5minute"
        )
        assert [row["sum"] for row in rows[target]] == pytest.approx(
            [expected, expected_after, -0.05 if subtracting else 1.65]
        )


@pytest.mark.parametrize("problem", ["count", "overlap", "conflict", "double"])
async def test_ambiguous_subtracted_sources_are_rejected(hass: HomeAssistant, problem: str) -> None:
    await create_mock_group_entry(
        hass,
        "Parts",
        {
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.a", "sensor.b"],
            ENTRY_DATA_ENERGY_ENTITY: "sensor.parts",
        },
        setup=False,
    )
    await create_mock_group_entry(
        hass,
        "Inner remainder",
        {
            CONF_GROUP_TYPE: GroupType.SUBTRACT,
            CONF_ENTITY_ID: "sensor.p1",
            CONF_SUBTRACT_ENTITIES: ["sensor.p2"],
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.a",
            CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.b"],
            ENTRY_DATA_ENERGY_ENTITY: "sensor.inner",
        },
        setup=False,
    )
    config = {
        CONF_GROUP_TYPE: GroupType.SUBTRACT,
        CONF_ENTITY_ID: "sensor.main",
        CONF_SUBTRACT_ENTITIES: ["sensor.p1"],
        CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.main_energy",
        CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.a"],
    }
    if problem == "count":
        config[CONF_SUBTRACT_ENTITIES] = ["sensor.p1", "sensor.p2"]
    elif problem == "overlap":
        config[CONF_SUBTRACT_ENTITIES] = ["sensor.p1", "sensor.p2"]
        config[CONF_GROUP_ENERGY_SUBTRACT_ENTITIES] = ["sensor.parts", "sensor.a"]
    elif problem == "double":
        config[CONF_GROUP_ENERGY_BASE_ENTITY] = "sensor.b"
        config[CONF_GROUP_ENERGY_SUBTRACT_ENTITIES] = ["sensor.inner"]
    else:
        config = {CONF_GROUP_TYPE: GroupType.CUSTOM, CONF_GROUP_ENERGY_ENTITIES: ["sensor.inner", "sensor.b"]}
    with pytest.raises(HistoryImportError):
        await resolve_history_source_weights(hass, config)


async def test_complete_cancellation_is_a_known_zero_curve(hass: HomeAssistant) -> None:
    weights = await resolve_history_source_weights(
        hass,
        {
            CONF_GROUP_TYPE: GroupType.SUBTRACT,
            CONF_ENTITY_ID: "sensor.main",
            CONF_SUBTRACT_ENTITIES: ["sensor.part"],
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.counter",
            CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.counter"],
        },
    )
    assert weights == {"sensor.counter": 0}
    result = aggregate_statistics(
        list(weights),
        {
            "sensor.counter": [
                {"start": 0.0, "sum": 100.0},
                {"start": 3600.0, "sum": 200.0},
            ]
        },
        AggregationState(),
        source_weights=weights,
    )
    assert [row["sum"] for row in result] == [0, 0]
