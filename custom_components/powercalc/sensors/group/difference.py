"""Device-based differences of existing power and energy sensors."""

from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.typing import ConfigType

from custom_components.powercalc.const import (
    CONF_BASE_DEVICE,
    CONF_CREATE_ENERGY_SENSOR,
    CONF_DIFFERENCE_DEVICES,
    CONF_UTILITY_METER_NET_CONSUMPTION,
    GroupType,
)
from custom_components.powercalc.errors import SensorConfigurationError
from custom_components.powercalc.group_include.filter import DeviceFilter
from custom_components.powercalc.group_include.include import find_entities
from custom_components.powercalc.sensors.energy_related import create_energy_related_sensors
from custom_components.powercalc.sensors.group.custom import (
    create_grouped_energy_sensor,
    create_grouped_power_sensor,
    filter_entity_list_by_class,
)


@dataclass
class DifferenceGroupMembers:
    power: dict[str, int]
    energy: dict[str, int]


class DifferenceGroupConfigurationError(SensorConfigurationError):
    def __init__(self, translation_key: str, message: str) -> None:
        super().__init__(message)
        self.config_flow_key = translation_key


async def resolve_difference_group_members(hass: HomeAssistant, config: ConfigType) -> DifferenceGroupMembers:
    """Resolve device sensors using the same member selection as standard groups."""
    base_device = config.get(CONF_BASE_DEVICE)
    difference_devices = config.get(CONF_DIFFERENCE_DEVICES) or []
    if not base_device or not difference_devices:
        raise DifferenceGroupConfigurationError(
            "difference_devices_mandatory", "A base device and at least one difference device are required"
        )
    if base_device in difference_devices or len(set(difference_devices)) != len(difference_devices):
        raise DifferenceGroupConfigurationError(
            "difference_duplicate_devices", "Each device can only occur once in a difference group"
        )

    members = DifferenceGroupMembers(power={}, energy={})
    registry = dr.async_get(hass)
    for device_id in [base_device, *difference_devices]:
        if registry.async_get(device_id) is None:
            raise DifferenceGroupConfigurationError("difference_device_sensors", f"Device {device_id} does not exist")
        result = await find_entities(hass, DeviceFilter(device_id), include_non_powercalc=True)
        power_ids = filter_entity_list_by_class(result.resolved, SensorDeviceClass.POWER)
        energy_ids = filter_entity_list_by_class(result.resolved, SensorDeviceClass.ENERGY)
        if not power_ids or not energy_ids:
            raise DifferenceGroupConfigurationError(
                "difference_device_sensors", f"Device {device_id} must have enabled power and energy sensors"
            )
        if members.power.keys() & power_ids or members.energy.keys() & energy_ids:
            raise DifferenceGroupConfigurationError(
                "difference_shared_sensors", f"Device {device_id} shares sensors with another selected device"
            )
        weight = 1 if device_id == base_device else -1
        members.power.update(dict.fromkeys(power_ids, weight))
        members.energy.update(dict.fromkeys(energy_ids, weight))
    return members


async def create_difference_group_sensors(hass: HomeAssistant, config: ConfigType) -> list[Entity]:
    members = await resolve_difference_group_members(hass, config)
    group_name = str(config[CONF_NAME])
    config[CONF_CREATE_ENERGY_SENSOR] = True
    power_sensor = create_grouped_power_sensor(
        hass, group_name, GroupType.DIFFERENCE, config, set(members.power), member_weights=members.power
    )
    energy_sensor = create_grouped_energy_sensor(
        hass,
        group_name,
        GroupType.DIFFERENCE,
        config,
        set(members.energy),
        power_sensor,
        member_weights=members.energy,
    )
    config[CONF_UTILITY_METER_NET_CONSUMPTION] = True
    sensors: list[Entity] = [power_sensor, energy_sensor]
    sensors.extend(create_energy_related_sensors(hass, config, energy_sensor))
    return sensors
