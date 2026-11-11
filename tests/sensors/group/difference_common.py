from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant

from tests.common import mock_devices, mock_entities_in_registry

BASE_DEVICE = "base-device"
FIRST_DEVICE = "first-device"
SECOND_DEVICE = "second-device"
BASE_POWER = "sensor.base_power"
BASE_ENERGY = "sensor.base_energy"
FIRST_POWER = "sensor.first_power"
FIRST_ENERGY = "sensor.first_energy"
SECOND_POWER = "sensor.second_power"
SECOND_ENERGY = "sensor.second_energy"


def prepare_difference_devices(hass: HomeAssistant) -> None:
    mock_devices(
        hass, {BASE_DEVICE: {"name": "Base"}, FIRST_DEVICE: {"name": "First"}, SECOND_DEVICE: {"name": "Second"}}
    )
    mock_entities_in_registry(
        hass,
        {
            BASE_POWER: {"device_id": BASE_DEVICE, "original_device_class": SensorDeviceClass.POWER},
            BASE_ENERGY: {"device_id": BASE_DEVICE, "original_device_class": SensorDeviceClass.ENERGY},
            FIRST_POWER: {"device_id": FIRST_DEVICE, "original_device_class": SensorDeviceClass.POWER},
            FIRST_ENERGY: {"device_id": FIRST_DEVICE, "original_device_class": SensorDeviceClass.ENERGY},
            SECOND_POWER: {"device_id": SECOND_DEVICE, "original_device_class": SensorDeviceClass.POWER},
            SECOND_ENERGY: {"device_id": SECOND_DEVICE, "original_device_class": SensorDeviceClass.ENERGY},
            "sensor.base_daily": {
                "device_id": BASE_DEVICE,
                "platform": "utility_meter",
                "original_device_class": SensorDeviceClass.ENERGY,
            },
        },
    )
    for entity_id, value, unit in [
        (BASE_POWER, 2000, UnitOfPower.WATT),
        (BASE_ENERGY, 100000, UnitOfEnergy.WATT_HOUR),
        (FIRST_POWER, 0.5, UnitOfPower.KILO_WATT),
        (FIRST_ENERGY, 20, UnitOfEnergy.KILO_WATT_HOUR),
        (SECOND_POWER, 250, UnitOfPower.WATT),
        (SECOND_ENERGY, 10000, UnitOfEnergy.WATT_HOUR),
        ("sensor.base_daily", 999, UnitOfEnergy.KILO_WATT_HOUR),
    ]:
        hass.states.async_set(entity_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit})
