"""Source selection and persistence boundaries for additional history group types."""

from unittest.mock import patch

from homeassistant.const import CONF_DOMAIN, CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.setup import async_setup_component
import pytest

from custom_components.powercalc.const import (
    ATTR_IS_GROUP,
    CONF_CREATE_ENERGY_SENSORS,
    CONF_DISABLE_EXTENDED_ATTRIBUTES,
    CONF_GROUP_ENERGY_BASE_ENTITY,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_ENERGY_SUBTRACT_ENTITIES,
    CONF_GROUP_TYPE,
    CONF_SENSOR_TYPE,
    CONF_SUBTRACT_ENTITIES,
    DATA_GROUP_ENTITIES,
    DOMAIN,
    ENTRY_DATA_ENERGY_ENTITY,
    ENTRY_DATA_POWER_ENTITY,
    GroupType,
    SensorType,
)
from custom_components.powercalc.sensors.energy import RealEnergySensor
from custom_components.powercalc.sensors.group.custom import GroupedEnergySensor, configure_group_history
from custom_components.powercalc.sensors.group.history import HistoryImportError
from custom_components.powercalc.sensors.group.history_recorder import HistoryRecorder
from custom_components.powercalc.sensors.group.history_sources import (
    find_energy_counter,
    resolve_history_source_weights,
)
from custom_components.powercalc.sensors.group.subtract import create_subtract_group_sensors
from tests.common import (
    create_mock_config_entry,
    create_mock_group_entry,
    mock_entities_in_registry,
    run_powercalc_setup,
)
from tests.sensors.group.test_history_failures import (
    PendingGroup,
    assert_failure,
    pending_group as pending_group,
    setup_pending_group,
)
from tests.sensors.group.test_history_recorder import mock_recorder_before_hass as mock_recorder_before_hass


async def test_all_domain_keeps_unavailable_sources_and_excludes_group_outputs(hass: HomeAssistant) -> None:
    await run_powercalc_setup(hass)
    mock_entities_in_registry(
        hass,
        {
            "sensor.keep": {"original_device_class": "energy"},
            "sensor.unavailable": {"original_device_class": "energy"},
            "sensor.disabled": {"original_device_class": "energy", "disabled_by": RegistryEntryDisabler.USER},
            "sensor.helper_group": {"original_device_class": "energy", "platform": "group"},
            "sensor.runtime_group": {"original_device_class": "energy"},
            "sensor.yaml_group": {"original_device_class": "energy"},
            "sensor.power": {"original_device_class": "power"},
        },
    )
    hass.states.async_set("sensor.unavailable", "unavailable", {"unit_of_measurement": "kWh", "state_class": "total"})
    hass.states.async_set("sensor.yaml_group", "100", {ATTR_IS_GROUP: True})
    hass.data[DOMAIN][DATA_GROUP_ENTITIES]["sensor.runtime_group"] = GroupedEnergySensor(
        hass,
        "Runtime",
        {"sensor.keep"},
        "sensor.runtime_group",
        {},
        GroupType.CUSTOM,
    )
    weights = await resolve_history_source_weights(hass, {CONF_GROUP_TYPE: GroupType.DOMAIN, CONF_DOMAIN: "all"})
    assert weights == {"sensor.keep": 1, "sensor.unavailable": 1}
    with pytest.raises(HistoryImportError, match="Cannot resolve"):
        await resolve_history_source_weights(
            hass,
            {
                CONF_GROUP_TYPE: GroupType.CUSTOM,
                CONF_GROUP_ENERGY_ENTITIES: ["sensor.runtime_group"],
            },
        )


async def test_domain_can_resolve_persisted_energy_ids_before_source_setup(hass: HomeAssistant) -> None:
    await create_mock_config_entry(
        hass,
        {
            CONF_SENSOR_TYPE: SensorType.VIRTUAL_POWER,
            CONF_ENTITY_ID: "light.source",
            ENTRY_DATA_ENERGY_ENTITY: "sensor.source_energy",
        },
        setup=False,
    )
    await create_mock_group_entry(
        hass,
        "Not a source",
        {
            CONF_GROUP_TYPE: GroupType.SUBTRACT,
            CONF_ENTITY_ID: "light.source",
            ENTRY_DATA_ENERGY_ENTITY: "sensor.group_energy",
        },
        setup=False,
    )
    assert await resolve_history_source_weights(hass, {CONF_GROUP_TYPE: GroupType.DOMAIN, CONF_DOMAIN: "light"}) == {
        "sensor.source_energy": 1,
    }


@pytest.mark.parametrize("real_source", [False, True])
async def test_counter_lookup_uses_persisted_powercalc_associations(hass: HomeAssistant, real_source: bool) -> None:
    await create_mock_config_entry(
        hass,
        {
            CONF_SENSOR_TYPE: SensorType.REAL_POWER if real_source else SensorType.VIRTUAL_POWER,
            CONF_ENTITY_ID: "sensor.power" if real_source else "light.source",
            ENTRY_DATA_ENERGY_ENTITY: "sensor.energy",
            **({} if real_source else {ENTRY_DATA_POWER_ENTITY: "sensor.power"}),
        },
        setup=False,
    )
    assert find_energy_counter(hass, "sensor.power") == "sensor.energy"
    await create_mock_config_entry(
        hass,
        {
            CONF_SENSOR_TYPE: SensorType.REAL_POWER,
            CONF_ENTITY_ID: "sensor.power",
            ENTRY_DATA_ENERGY_ENTITY: "sensor.other_energy",
        },
        setup=False,
    )
    with pytest.raises(HistoryImportError, match="No unique"):
        find_energy_counter(hass, "sensor.power")


async def test_history_flag_does_not_force_energy_sensor_creation(hass: HomeAssistant) -> None:
    sensors = create_subtract_group_sensors(
        hass,
        {
            CONF_NAME: "History",
            CONF_ENTITY_ID: "sensor.base",
            CONF_SUBTRACT_ENTITIES: ["sensor.part"],
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_CREATE_ENERGY_SENSORS: False,
        },
    )
    assert len(sensors) == 1
    assert sensors[0].device_class == "power"


async def test_pending_import_freezes_the_source_signs(hass: HomeAssistant, pending_group: PendingGroup) -> None:
    job = pending_group.job
    job.journal.sources.append("sensor.other")
    job.journal.source_weights = {"sensor.source": 1, "sensor.other": -1}
    await job.save()
    await HistoryRecorder(hass, "sensor.other", "kWh").import_batch(
        [
            {"start": pending_group.boundary.timestamp() - 300, "state": 400.0, "sum": 200.0},
        ],
        "5minute",
    )
    hass.states.async_set("sensor.other", "400", {"unit_of_measurement": "kWh", "state_class": "total"})
    hass.config_entries.async_update_entry(
        pending_group.entry,
        data={
            **pending_group.entry.data,
            CONF_GROUP_TYPE: GroupType.SUBTRACT,
            CONF_ENTITY_ID: "sensor.other_power",
            CONF_SUBTRACT_ENTITIES: ["sensor.source_power"],
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.other",
            CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.source"],
        },
    )
    await setup_pending_group(hass, pending_group)
    assert_failure(hass, "members changed")
    stored = await job.store.async_load()
    assert stored["source_weights"] == {"sensor.source": 1, "sensor.other": -1}


async def test_group_cannot_import_its_own_target(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_start()
    with patch(
        "custom_components.powercalc.sensors.group.history_sources.resolve_history_source_weights",
        return_value={"sensor.history_energy": 1},
    ):
        await create_mock_group_entry(
            hass,
            "History",
            {
                CONF_GROUP_TYPE: GroupType.DOMAIN,
                CONF_DOMAIN: "light",
                CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
            },
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert_failure(hass, "own energy sensor")


async def test_domain_history_keeps_diagnostics_with_extended_attributes_disabled(
    hass: HomeAssistant, pending_group: PendingGroup
) -> None:
    hass.config_entries.async_update_entry(
        pending_group.entry,
        data={
            **pending_group.entry.data,
            CONF_GROUP_TYPE: GroupType.DOMAIN,
            CONF_DOMAIN: "light",
            CONF_DISABLE_EXTENDED_ATTRIBUTES: True,
        },
    )
    with patch(
        "custom_components.powercalc.sensors.group.history_sources.resolve_history_source_weights",
        return_value={"sensor.source": 1},
    ):
        await setup_pending_group(hass, pending_group)
    state = hass.states.get("sensor.history_energy")
    assert state.attributes["history_import_status"] == "completed"
    assert float(state.state) == pytest.approx(0.5)
    assert "entities" not in state.attributes


async def test_import_cannot_take_ownership_of_an_existing_external_counter(hass: HomeAssistant) -> None:
    with pytest.raises(HistoryImportError, match="newly created"):
        configure_group_history(hass, RealEnergySensor("sensor.external"), {CONF_GROUP_ENERGY_IMPORT_HISTORY: True})
