"""History import options for domain and subtract group creation."""

from homeassistant import data_entry_flow
from homeassistant.const import CONF_DOMAIN, CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant
import pytest

from custom_components.powercalc.const import (
    CONF_CREATE_ENERGY_SENSOR,
    CONF_CREATE_UTILITY_METERS,
    CONF_GROUP_ENERGY_BASE_ENTITY,
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_ENERGY_SUBTRACT_ENTITIES,
    CONF_GROUP_TYPE,
    CONF_SENSOR_TYPE,
    CONF_SUBTRACT_ENTITIES,
    ENTRY_DATA_ENERGY_ENTITY,
    ENTRY_DATA_POWER_ENTITY,
    GroupType,
    SensorType,
)
from custom_components.powercalc.flow_helper.common import Step
from tests.common import create_mock_config_entry
from tests.config_flow.common import initialize_options_flow, select_menu_item, submit_form_step, submit_options_step


@pytest.mark.parametrize(
    "group_type,step", [(GroupType.DOMAIN, Step.GROUP_DOMAIN), (GroupType.SUBTRACT, Step.GROUP_SUBTRACT)]
)
async def test_create_history_group(hass: HomeAssistant, group_type: GroupType, step: Step) -> None:
    result = await select_menu_item(hass, Step.MENU_GROUP, step)
    user_input = {CONF_NAME: "History", CONF_GROUP_ENERGY_IMPORT_HISTORY: True}
    if group_type == GroupType.DOMAIN:
        user_input[CONF_DOMAIN] = "light"
    else:
        user_input.update(
            {
                CONF_ENTITY_ID: "sensor.base_power",
                CONF_SUBTRACT_ENTITIES: ["sensor.part_power"],
                CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.base_energy",
                CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.part_energy"],
            }
        )
    result = await submit_form_step(hass, result, user_input)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GROUP_TYPE] == group_type
    assert result["data"][CONF_GROUP_ENERGY_IMPORT_HISTORY]
    assert result["data"][CONF_GROUP_ENERGY_START_AT_ZERO] is False


@pytest.mark.parametrize("step", [Step.GROUP_DOMAIN, Step.GROUP_SUBTRACT])
async def test_history_requires_energy_sensor(hass: HomeAssistant, step: Step) -> None:
    result = await select_menu_item(hass, Step.MENU_GROUP, step)
    data = {CONF_NAME: "History", CONF_GROUP_ENERGY_IMPORT_HISTORY: True, CONF_CREATE_ENERGY_SENSOR: False}
    if step == Step.GROUP_DOMAIN:
        data[CONF_DOMAIN] = "light"
    else:
        data.update({CONF_ENTITY_ID: "sensor.base_power", CONF_SUBTRACT_ENTITIES: ["sensor.part_power"]})
    result = await submit_form_step(hass, result, data)
    assert result["errors"] == {"base": "group_history_requires_energy"}


@pytest.mark.parametrize(
    "energy_data",
    [
        {},
        {CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.base_energy"},
        {
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.base_energy",
            CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.part_energy"],
        },
    ],
)
async def test_subtract_requires_all_counter_associations(hass: HomeAssistant, energy_data: dict) -> None:
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_SUBTRACT)
    result = await submit_form_step(
        hass,
        result,
        {
            CONF_NAME: "History",
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_ENTITY_ID: "sensor.base_power",
            CONF_SUBTRACT_ENTITIES: ["sensor.part_power", "sensor.other_power"],
            **energy_data,
        },
    )
    assert result["errors"] == {"base": "group_history_energy_sources"}


async def test_subtract_uses_existing_powercalc_energy_associations(hass: HomeAssistant) -> None:
    for source in ["base", "part"]:
        await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.REAL_POWER,
                CONF_ENTITY_ID: f"sensor.{source}_power",
                ENTRY_DATA_POWER_ENTITY: f"sensor.{source}_power",
                ENTRY_DATA_ENERGY_ENTITY: f"sensor.{source}_energy",
            },
            setup=False,
        )
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_SUBTRACT)
    result = await submit_form_step(
        hass,
        result,
        {
            CONF_NAME: "History",
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_ENTITY_ID: "sensor.base_power",
            CONF_SUBTRACT_ENTITIES: ["sensor.part_power"],
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert CONF_GROUP_ENERGY_BASE_ENTITY not in result["data"]


async def test_subtract_history_options_keep_the_import_one_time(hass: HomeAssistant) -> None:
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_SUBTRACT)
    result = await submit_form_step(
        hass,
        result,
        {
            CONF_NAME: "History",
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_ENTITY_ID: "sensor.base_power",
            CONF_SUBTRACT_ENTITIES: ["sensor.part_power"],
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.base_energy",
            CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.part_energy"],
        },
    )
    entry = result["result"]
    options = await initialize_options_flow(hass, entry, Step.GROUP_SUBTRACT)
    assert CONF_GROUP_ENERGY_IMPORT_HISTORY not in options["data_schema"].schema
    options = await submit_options_step(hass, options, {CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.a", "sensor.b"]})
    assert options["errors"] == {"base": "group_history_energy_sources"}
    options = await submit_options_step(
        hass,
        options,
        {
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.part_energy",
            CONF_GROUP_ENERGY_SUBTRACT_ENTITIES: ["sensor.base_energy"],
        },
    )
    assert options["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_GROUP_ENERGY_IMPORT_HISTORY]
    assert entry.data[CONF_GROUP_ENERGY_BASE_ENTITY] == "sensor.part_energy"


async def test_subtract_without_history_keeps_existing_configuration(hass: HomeAssistant) -> None:
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_SUBTRACT)
    result = await submit_form_step(
        hass,
        result,
        {
            CONF_NAME: "Subtract",
            CONF_ENTITY_ID: "sensor.base_power",
            CONF_SUBTRACT_ENTITIES: ["sensor.part_power"],
        },
    )
    assert result["data"] == {
        CONF_SENSOR_TYPE: SensorType.GROUP,
        CONF_GROUP_TYPE: GroupType.SUBTRACT,
        CONF_NAME: "Subtract",
        CONF_ENTITY_ID: "sensor.base_power",
        CONF_SUBTRACT_ENTITIES: ["sensor.part_power"],
        CONF_CREATE_ENERGY_SENSOR: True,
        CONF_CREATE_UTILITY_METERS: False,
    }


async def test_history_can_use_a_base_without_subtracted_sources(hass: HomeAssistant) -> None:
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_SUBTRACT)
    result = await submit_form_step(
        hass,
        result,
        {
            CONF_NAME: "Identity",
            CONF_ENTITY_ID: "sensor.base_power",
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_GROUP_ENERGY_BASE_ENTITY: "sensor.base_energy",
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SUBTRACT_ENTITIES] == []
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.identity_energy") is not None
