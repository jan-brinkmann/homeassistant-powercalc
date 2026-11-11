from homeassistant import data_entry_flow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
import pytest

from custom_components.powercalc.const import (
    CONF_BASE_DEVICE,
    CONF_CREATE_ENERGY_SENSOR,
    CONF_CREATE_UTILITY_METERS,
    CONF_DIFFERENCE_DEVICES,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_TYPE,
    CONF_SENSOR_TYPE,
    GroupType,
    SensorType,
)
from custom_components.powercalc.flow_helper.common import Step
from tests.config_flow.common import initialize_options_flow, select_menu_item
from tests.sensors.group.difference_common import (
    BASE_DEVICE,
    FIRST_DEVICE,
    SECOND_DEVICE,
    prepare_difference_devices,
)


@pytest.mark.parametrize("start_at_zero", [True, False])
async def test_difference_group_flow(hass: HomeAssistant, start_at_zero: bool) -> None:
    prepare_difference_devices(hass)
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_DIFFERENCE)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Difference",
            CONF_BASE_DEVICE: BASE_DEVICE,
            CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE, SECOND_DEVICE],
            CONF_GROUP_ENERGY_START_AT_ZERO: start_at_zero,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_NAME: "Difference",
        CONF_SENSOR_TYPE: SensorType.GROUP,
        CONF_GROUP_TYPE: GroupType.DIFFERENCE,
        CONF_BASE_DEVICE: BASE_DEVICE,
        CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE, SECOND_DEVICE],
        CONF_GROUP_ENERGY_START_AT_ZERO: start_at_zero,
        CONF_CREATE_ENERGY_SENSOR: True,
        CONF_CREATE_UTILITY_METERS: False,
    }
    entry = result["result"]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.difference_power")
    assert hass.states.get("sensor.difference_energy")

    # Both sensors remain enabled in the basic options.
    options = await initialize_options_flow(hass, entry, Step.BASIC_OPTIONS)
    assert CONF_CREATE_ENERGY_SENSOR not in options["data_schema"].schema

    options = await initialize_options_flow(hass, entry, Step.GROUP_DIFFERENCE)
    assert CONF_GROUP_ENERGY_START_AT_ZERO not in options["data_schema"].schema
    options = await hass.config_entries.options.async_configure(
        options["flow_id"], {CONF_BASE_DEVICE: BASE_DEVICE, CONF_DIFFERENCE_DEVICES: [BASE_DEVICE]}
    )
    assert options["type"] == data_entry_flow.FlowResultType.FORM
    assert options["errors"] == {"base": "difference_duplicate_devices"}
    assert entry.data[CONF_DIFFERENCE_DEVICES] == [FIRST_DEVICE, SECOND_DEVICE]

    options = await hass.config_entries.options.async_configure(
        options["flow_id"], {CONF_BASE_DEVICE: BASE_DEVICE, CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE]}
    )
    assert options["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_DIFFERENCE_DEVICES] == [FIRST_DEVICE]
    assert entry.data[CONF_GROUP_ENERGY_START_AT_ZERO] is start_at_zero
    assert hass.states.get("sensor.difference_power").state == "1500.00"


@pytest.mark.parametrize(
    "base,differences,reason",
    [
        (BASE_DEVICE, [BASE_DEVICE], "difference_duplicate_devices"),
        (BASE_DEVICE, [FIRST_DEVICE, FIRST_DEVICE], "difference_duplicate_devices"),
        ("missing", [FIRST_DEVICE], "difference_device_sensors"),
    ],
)
async def test_invalid_group_flow(hass: HomeAssistant, base: str, differences: list[str], reason: str) -> None:
    prepare_difference_devices(hass)
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_DIFFERENCE)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Difference", CONF_BASE_DEVICE: base, CONF_DIFFERENCE_DEVICES: differences}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": reason}
