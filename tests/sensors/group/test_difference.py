from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from freezegun import freeze_time
from homeassistant.components.sensor import ATTR_LAST_RESET, SensorDeviceClass
from homeassistant.components.utility_meter.const import DAILY
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_ENTITY_ID,
    CONF_NAME,
    STATE_UNAVAILABLE,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.powercalc.const import (
    CONF_BASE_DEVICE,
    CONF_CREATE_COST_SENSOR,
    CONF_CREATE_ENERGY_SENSORS,
    CONF_CREATE_GROUP,
    CONF_CREATE_UTILITY_METERS,
    CONF_DIFFERENCE_DEVICES,
    CONF_ENERGY_PRICE,
    CONF_ENERGY_PRICE_SENSOR,
    CONF_FORCE_CALCULATE_GROUP_ENERGY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_ENERGY_UPDATE_INTERVAL,
    CONF_GROUP_TYPE,
    CONF_SENSOR_TYPE,
    CONF_UTILITY_METER_TYPES,
    DATA_CONFIGURED_ENTITIES,
    DOMAIN,
    GroupType,
    SensorType,
)
from custom_components.powercalc.sensors.energy import RealEnergySensor
from custom_components.powercalc.sensors.group.difference import (
    DifferenceGroupConfigurationError,
    resolve_difference_group_members,
)
from custom_components.powercalc.sensors.power import RealPowerSensor
from tests.common import (
    assert_entity_state,
    async_advance_time,
    create_mock_config_entry,
    mock_entities_in_registry,
    run_powercalc_setup,
)
from tests.sensors.group.difference_common import (
    BASE_DEVICE,
    BASE_ENERGY,
    BASE_POWER,
    FIRST_DEVICE,
    FIRST_ENERGY,
    SECOND_DEVICE,
    SECOND_ENERGY,
    prepare_difference_devices,
)


@pytest.mark.parametrize("start_at_zero", [True, False])
async def test_difference_group(hass: HomeAssistant, start_at_zero: bool) -> None:
    prepare_difference_devices(hass)
    entry = await create_mock_config_entry(
        hass,
        {
            CONF_SENSOR_TYPE: SensorType.GROUP,
            CONF_GROUP_TYPE: GroupType.DIFFERENCE,
            CONF_NAME: "Difference",
            CONF_BASE_DEVICE: BASE_DEVICE,
            CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE, SECOND_DEVICE],
            CONF_GROUP_ENERGY_START_AT_ZERO: start_at_zero,
            CONF_CREATE_UTILITY_METERS: True,
        },
    )
    initial = 0 if start_at_zero else 70
    assert_entity_state(hass, "sensor.difference_power", "1250.00")
    assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial)
    assert hass.states.get("sensor.difference_energy_daily")

    for entity_id, value, unit in [
        (BASE_ENERGY, 101000, UnitOfEnergy.WATT_HOUR),
        (FIRST_ENERGY, 20.25, UnitOfEnergy.KILO_WATT_HOUR),
        (SECOND_ENERGY, 10250, UnitOfEnergy.WATT_HOUR),
    ]:
        hass.states.async_set(entity_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit})
        await hass.async_block_till_done()
    assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial + 0.5)

    # Restoring the group keeps the signed total and the member baselines.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial + 0.5)

    # A member reset contributes no increase, matching the standard group.
    hass.states.async_set(FIRST_ENERGY, 0, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR})
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial + 0.5)
    hass.states.async_set(FIRST_ENERGY, 0.1, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR})
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial + 0.4)

    hass.states.async_set(BASE_ENERGY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    hass.states.async_set(BASE_ENERGY, 102000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial + 1.4)

    # Changing power and advancing time must not add energy.
    hass.states.async_set(BASE_POWER, 3000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfPower.WATT})
    await async_advance_time(hass, timedelta(hours=1))
    assert_entity_state(hass, "sensor.difference_power", "2250.00")
    assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial + 1.4)


async def test_yaml_group_never_uses_integration(hass: HomeAssistant) -> None:
    prepare_difference_devices(hass)
    with patch("custom_components.powercalc.sensors.group.custom.VirtualEnergySensor") as integral_sensor:
        await run_powercalc_setup(
            hass,
            {
                CONF_CREATE_GROUP: "Difference",
                CONF_GROUP_TYPE: GroupType.DIFFERENCE,
                CONF_BASE_DEVICE: BASE_DEVICE,
                CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE, SECOND_DEVICE],
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
                CONF_FORCE_CALCULATE_GROUP_ENERGY: True,
            },
            {CONF_CREATE_ENERGY_SENSORS: False},
        )
        integral_sensor.assert_not_called()
    assert_entity_state(hass, "sensor.difference_power", "1250.00")
    assert_entity_state(hass, "sensor.difference_energy", "70.0000")


async def test_negative_energy_difference(hass: HomeAssistant) -> None:
    prepare_difference_devices(hass)
    await create_mock_config_entry(
        hass,
        {
            CONF_SENSOR_TYPE: SensorType.GROUP,
            CONF_GROUP_TYPE: GroupType.DIFFERENCE,
            CONF_NAME: "Difference",
            CONF_BASE_DEVICE: FIRST_DEVICE,
            CONF_DIFFERENCE_DEVICES: [BASE_DEVICE],
            CONF_GROUP_ENERGY_START_AT_ZERO: False,
        },
    )
    assert_entity_state(hass, "sensor.difference_energy", "-80.0000")
    assert_entity_state(hass, "sensor.difference_power", "-1500.00")


async def test_all_device_channels_are_included(hass: HomeAssistant) -> None:
    prepare_difference_devices(hass)
    registry = er.async_get(hass)
    for unique_id, device_class, value, unit in [
        ("extra_power", SensorDeviceClass.POWER, 100, UnitOfPower.WATT),
        ("extra_energy", SensorDeviceClass.ENERGY, 2000, UnitOfEnergy.WATT_HOUR),
    ]:
        entity = registry.async_get_or_create(
            "sensor", "test", unique_id, device_id=BASE_DEVICE, original_device_class=device_class
        )
        hass.states.async_set(entity.entity_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit})
    await create_mock_config_entry(
        hass,
        {
            CONF_SENSOR_TYPE: SensorType.GROUP,
            CONF_GROUP_TYPE: GroupType.DIFFERENCE,
            CONF_NAME: "Difference",
            CONF_BASE_DEVICE: BASE_DEVICE,
            CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE, SECOND_DEVICE],
            CONF_GROUP_ENERGY_START_AT_ZERO: False,
        },
    )
    assert_entity_state(hass, "sensor.difference_power", "1350.00")
    assert_entity_state(hass, "sensor.difference_energy", "72.0000")


@pytest.mark.parametrize(
    "base,differences,reason",
    [
        (None, [FIRST_DEVICE], "difference_devices_mandatory"),
        (BASE_DEVICE, [], "difference_devices_mandatory"),
        (BASE_DEVICE, [BASE_DEVICE], "difference_duplicate_devices"),
        (BASE_DEVICE, [FIRST_DEVICE, FIRST_DEVICE], "difference_duplicate_devices"),
        ("missing", [FIRST_DEVICE], "difference_device_sensors"),
    ],
)
async def test_invalid_device_selection(
    hass: HomeAssistant, base: str | None, differences: list[str], reason: str
) -> None:
    prepare_difference_devices(hass)
    with pytest.raises(DifferenceGroupConfigurationError) as error:
        await resolve_difference_group_members(hass, {CONF_BASE_DEVICE: base, CONF_DIFFERENCE_DEVICES: differences})
    assert error.value.config_flow_key == reason


@pytest.mark.parametrize("missing_sensor", [BASE_POWER, BASE_ENERGY])
async def test_device_requires_both_sensor_types(hass: HomeAssistant, missing_sensor: str) -> None:
    prepare_difference_devices(hass)
    er.async_get(hass).async_update_entity(missing_sensor, disabled_by=er.RegistryEntryDisabler.USER)
    with pytest.raises(DifferenceGroupConfigurationError, match="enabled power and energy"):
        await resolve_difference_group_members(
            hass, {CONF_BASE_DEVICE: BASE_DEVICE, CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE]}
        )


async def test_devices_with_shared_sensors_are_rejected(hass: HomeAssistant) -> None:
    prepare_difference_devices(hass)
    mock_entities_in_registry(
        hass, {"light.base": {"device_id": BASE_DEVICE}, "light.first": {"device_id": FIRST_DEVICE}}
    )
    shared = [(RealPowerSensor(BASE_POWER, UnitOfPower.WATT), True), (RealEnergySensor(BASE_ENERGY), True)]
    hass.data[DOMAIN] = {DATA_CONFIGURED_ENTITIES: {"light.base": shared, "light.first": shared}}
    with pytest.raises(DifferenceGroupConfigurationError, match="shares sensors"):
        await resolve_difference_group_members(
            hass, {CONF_BASE_DEVICE: BASE_DEVICE, CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE]}
        )


@pytest.mark.parametrize("start_at_zero", [True, False])
@pytest.mark.parametrize("dynamic_price", [True, False])
async def test_difference_costs_use_signed_deltas(
    hass: HomeAssistant, start_at_zero: bool, dynamic_price: bool
) -> None:
    with freeze_time(datetime(2026, 10, 8, 12, tzinfo=UTC)) as freezer:
        prepare_difference_devices(hass)
        hass.states.async_set(BASE_ENERGY, 120000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
        hass.states.async_set("sensor.price", 0.3, {ATTR_UNIT_OF_MEASUREMENT: "EUR/kWh"})
        entry = await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.GROUP,
                CONF_GROUP_TYPE: GroupType.DIFFERENCE,
                CONF_NAME: "Difference",
                CONF_BASE_DEVICE: BASE_DEVICE,
                CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE],
                CONF_GROUP_ENERGY_START_AT_ZERO: start_at_zero,
                CONF_GROUP_ENERGY_UPDATE_INTERVAL: 0,
                CONF_CREATE_COST_SENSOR: True,
                CONF_CREATE_UTILITY_METERS: True,
                CONF_UTILITY_METER_TYPES: [DAILY],
                **({CONF_ENERGY_PRICE_SENSOR: "sensor.price"} if dynamic_price else {CONF_ENERGY_PRICE: 0.3}),
            },
        )
        cost_id = "sensor.difference_cost"
        meter_id = "sensor.difference_energy_daily"
        meter_cost_id = "sensor.difference_energy_daily_cost"
        initial_reset = hass.states.get(meter_cost_id).attributes[ATTR_LAST_RESET]
        initial = 0 if start_at_zero else 100
        assert float(hass.states.get("sensor.difference_energy").state) == pytest.approx(initial)

        for entity_id, value, unit, expected_cost in [
            (BASE_ENERGY, 121000, UnitOfEnergy.WATT_HOUR, 0.3),
            (FIRST_ENERGY, 22, UnitOfEnergy.KILO_WATT_HOUR, -0.3),
            (FIRST_ENERGY, 123, UnitOfEnergy.KILO_WATT_HOUR, -30.6),
        ]:
            hass.states.async_set(entity_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit})
            await hass.async_block_till_done()
            await hass.async_block_till_done()
            assert float(hass.states.get(cost_id).state) == pytest.approx(expected_cost)
            assert float(hass.states.get(meter_cost_id).state) == pytest.approx(expected_cost)
            assert hass.states.get(meter_cost_id).attributes[ATTR_LAST_RESET] == initial_reset

        # Restoring negative energy and costs must retain both the baseline and cycle marker.
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        if dynamic_price:
            hass.states.async_set("sensor.price", 0.4, {ATTR_UNIT_OF_MEASUREMENT: "EUR/kWh"})
            await hass.async_block_till_done()
        price = 0.4 if dynamic_price else 0.3
        hass.states.async_set(BASE_ENERGY, 122000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
        await hass.async_block_till_done()
        await hass.async_block_till_done()
        assert float(hass.states.get(cost_id).state) == pytest.approx(-30.6 + price)
        assert float(hass.states.get(meter_cost_id).state) == pytest.approx(-30.6 + price)

        # A real cycle reset increases a negative meter to zero, but resets its cost.
        freezer.tick(timedelta(minutes=1))
        await hass.services.async_call("utility_meter", "reset", {CONF_ENTITY_ID: meter_id}, blocking=True)
        await hass.async_block_till_done()
        assert float(hass.states.get(meter_cost_id).state) == 0
        assert hass.states.get(meter_cost_id).attributes[ATTR_LAST_RESET] != initial_reset
        assert float(hass.states.get(cost_id).state) == pytest.approx(-30.6 + price)
        hass.states.async_set(BASE_ENERGY, 123000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
        await hass.async_block_till_done()
        await hass.async_block_till_done()
        assert float(hass.states.get(meter_cost_id).state) == pytest.approx(price)
        assert float(hass.states.get(cost_id).state) == pytest.approx(-30.6 + 2 * price)
