"""History initialization must leave the original group runtime in charge."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.powercalc.const import (
    CONF_ENERGY_SENSOR_UNIT_PREFIX,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_ENERGY_UPDATE_INTERVAL,
    CONF_GROUP_TYPE,
    GroupType,
    UnitPrefix,
)
from custom_components.powercalc.sensors.group.history import HistoryImportError
from custom_components.powercalc.sensors.group.history_import import GroupHistoryImport
from custom_components.powercalc.sensors.group.history_sensor import GroupHistoryInitializer
from tests.common import create_mock_group_entry
from tests.sensors.group.test_history_recorder import mock_recorder_before_hass as mock_recorder_before_hass
from tests.sensors.group.test_history_state_alignment import (
    FlatHistoryGroup,
    flat_history_group as flat_history_group,
)

pytestmark = pytest.mark.timeout(60)


@pytest.fixture(autouse=True)
def mock_unrelated_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.powercalc.discovery.DiscoveryManager.start_discovery", AsyncMock())


async def test_original_publication_interval_and_reset_behavior(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup, freezer: FrozenDateTimeFactory
) -> None:
    await create_mock_group_entry(
        hass,
        "Ordinary",
        {
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.a", "sensor.b"],
            CONF_GROUP_ENERGY_START_AT_ZERO: False,
            CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
        },
    )
    group_ids = ["sensor.history_energy", "sensor.ordinary_energy"]
    attributes = {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    boundary = flat_history_group.boundary

    async def change_at(seconds: int, value: str) -> None:
        freezer.move_to(boundary + timedelta(seconds=seconds))
        hass.states.async_set("sensor.a", value, attributes)
        await hass.async_block_till_done()

    await change_at(180, "2.6")
    assert [float(hass.states.get(entity).state) for entity in group_ids] == pytest.approx([4.208, 4.208])
    published = [hass.states.get(entity).last_updated for entity in group_ids]
    await change_at(185, "2.7")
    assert [hass.states.get(entity).last_updated for entity in group_ids] == published
    assert [float(hass.states.get(entity).state) for entity in group_ids] == pytest.approx([4.208, 4.208])
    freezer.move_to(boundary + timedelta(seconds=246))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    states = [hass.states.get(entity) for entity in group_ids]
    assert [float(state.state) for state in states] == pytest.approx([4.308, 4.308])
    assert states[0].last_updated == states[1].last_updated

    with (
        patch.object(
            GroupHistoryInitializer, "_replay_history", side_effect=AssertionError("Unexpected history replay")
        ),
        patch.object(GroupHistoryImport, "save", side_effect=AssertionError("Unexpected live import checkpoint")),
    ):
        assert await hass.config_entries.async_reload(flat_history_group.entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        await change_at(360, "0.1")
        await change_at(420, "0.2")
    assert float(hass.states.get(group_ids[0]).state) == pytest.approx(float(hass.states.get(group_ids[1]).state))
    assert (
        hass.states.get(group_ids[0]).attributes["state_class"]
        == hass.states.get(group_ids[1]).attributes["state_class"]
    )


@pytest.mark.parametrize("overlap", [False, True])
async def test_direct_group_counter_uses_its_own_history_and_runtime_updates(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup, freezer: FrozenDateTimeFactory, overlap: bool
) -> None:
    boundary = flat_history_group.boundary
    freezer.move_to(boundary + timedelta(minutes=2, seconds=1))
    sources = ["sensor.history_energy", "sensor.a"] if overlap else ["sensor.history_energy"]
    config = {
        CONF_GROUP_TYPE: GroupType.CUSTOM,
        CONF_GROUP_ENERGY_ENTITIES: sources,
        CONF_GROUP_ENERGY_START_AT_ZERO: False,
        CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
    }
    await create_mock_group_entry(hass, "Parent", {**config, CONF_GROUP_ENERGY_IMPORT_HISTORY: True})
    await hass.async_block_till_done(wait_background_tasks=True)
    parent = hass.states.get("sensor.parent_energy")
    if overlap:
        assert parent.state == "unknown"
        assert parent.attributes["history_import_status"] == "failed"
        return
    assert float(parent.state) == pytest.approx(4.1203)
    await create_mock_group_entry(hass, "Ordinary parent", config)
    freezer.move_to(boundary + timedelta(minutes=3))
    hass.states.async_set(
        "sensor.a", "2.6", {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    )
    await hass.async_block_till_done()
    freezer.move_to(boundary + timedelta(minutes=4, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.parent_energy").state) == pytest.approx(4.208)
    assert hass.states.get("sensor.parent_energy").state == hass.states.get("sensor.ordinary_parent_energy").state


async def test_history_does_not_expand_the_original_domain_runtime_selection(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup
) -> None:
    # The original domain-all implementation selects registry class overrides.
    # An entity with only an original class must not silently introduce a different
    # live membership just because history import was enabled.
    er.async_get(hass).async_get_or_create(
        "sensor", "test", "unselected", original_device_class="energy", suggested_object_id="unselected"
    )
    await create_mock_group_entry(
        hass,
        "Empty domain",
        {CONF_GROUP_TYPE: GroupType.DOMAIN, CONF_DOMAIN: "all", CONF_GROUP_ENERGY_IMPORT_HISTORY: True},
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get("sensor.empty_domain_energy")
    assert state.state == "unknown"
    assert state.attributes["history_import_status"] == "failed"


async def test_failed_runtime_handoff_removes_listeners_and_can_resume(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup, freezer: FrozenDateTimeFactory
) -> None:
    save = GroupHistoryImport.save

    async def fail_handoff_checkpoint(job: GroupHistoryImport) -> None:
        if job.journal.runtime_handoff_complete:
            raise HistoryImportError("Cannot save the runtime handoff")
        await save(job)

    freezer.move_to(flat_history_group.boundary + timedelta(minutes=2, seconds=1))
    with patch.object(GroupHistoryImport, "save", fail_handoff_checkpoint):
        entry = await create_mock_group_entry(
            hass,
            "Unfinished parent",
            {
                CONF_GROUP_TYPE: GroupType.CUSTOM,
                CONF_GROUP_ENERGY_ENTITIES: ["sensor.history_energy"],
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
                CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
                CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
            },
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    stopped = hass.states.get("sensor.unfinished_parent_energy")
    assert stopped.state == "unavailable"
    assert stopped.attributes["restored"] is True
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(
        "sensor.a", "2.6", {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.unfinished_parent_energy") == stopped
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.unfinished_parent_energy").attributes["history_import_status"] == "completed"


async def test_original_decimal_rounding_is_preserved_at_halfway_values(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(flat_history_group.boundary + timedelta(minutes=2, seconds=1))
    attributes = {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    hass.states.async_set("sensor.a", "2.51235", attributes)
    config = {
        CONF_GROUP_TYPE: GroupType.CUSTOM,
        CONF_GROUP_ENERGY_ENTITIES: ["sensor.a", "sensor.b"],
        CONF_GROUP_ENERGY_START_AT_ZERO: False,
        CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
        CONF_GROUP_ENERGY_UPDATE_INTERVAL: 0,
    }
    await create_mock_group_entry(hass, "Rounded original", config)
    await create_mock_group_entry(hass, "Rounded history", {**config, CONF_GROUP_ENERGY_IMPORT_HISTORY: True})
    await hass.async_block_till_done(wait_background_tasks=True)
    for value, expected in [["2.51235", "4.1204"], ["2.51245", "4.1204"], ["2.51255", "4.1206"]]:
        hass.states.async_set("sensor.a", value, attributes)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.rounded_original_energy").state == expected
        assert hass.states.get("sensor.rounded_history_energy").state == expected
