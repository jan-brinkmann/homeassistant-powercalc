from homeassistant import data_entry_flow
from homeassistant.const import CONF_DOMAIN, CONF_NAME
from homeassistant.core import HomeAssistant
import pytest

from custom_components.powercalc.config_flow import Step
from custom_components.powercalc.const import (
    CONF_CREATE_ENERGY_SENSOR,
    CONF_CREATE_UTILITY_METERS,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_TYPE,
    CONF_SENSOR_TYPE,
    GroupType,
    SensorType,
)
from tests.common import create_mock_config_entry
from tests.config_flow.common import select_menu_item


@pytest.mark.parametrize("start_at_zero", [None, False])
async def test_domain_group_flow(hass: HomeAssistant, start_at_zero: bool | None) -> None:
    result = await select_menu_item(hass, Step.MENU_GROUP, Step.GROUP_DOMAIN)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    user_input = {CONF_NAME: "My domain", CONF_DOMAIN: "light", CONF_CREATE_UTILITY_METERS: False}
    if start_at_zero is not None:
        user_input[CONF_GROUP_ENERGY_START_AT_ZERO] = start_at_zero
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GROUP_ENERGY_START_AT_ZERO] == (True if start_at_zero is None else start_at_zero)


async def test_domain_group_option_menu(hass: HomeAssistant) -> None:
    """Test the domain group option menu."""
    entry = await create_mock_config_entry(
        hass,
        {
            CONF_SENSOR_TYPE: SensorType.GROUP,
            CONF_GROUP_TYPE: GroupType.DOMAIN,
            CONF_DOMAIN: "light",
            CONF_CREATE_ENERGY_SENSOR: True,
            CONF_CREATE_UTILITY_METERS: False,
        },
    )

    result = await hass.config_entries.options.async_init(
        entry.entry_id,
        data=None,
    )

    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == Step.INIT
    assert result["menu_options"] == [Step.BASIC_OPTIONS, Step.GROUP_DOMAIN]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": Step.GROUP_DOMAIN},
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_DOMAIN: "switch",
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DOMAIN] == "switch"
