from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import AsyncMock, Mock, patch

from _pytest.fixtures import SubRequest
from freezegun import freeze_time
from homeassistant.components.recorder import migration
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_import_statistics,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_DOMAIN,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_NAME,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
    statistics_during_period,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from custom_components.powercalc.const import (
    CONF_BASE_DEVICE,
    CONF_CREATE_STANDBY_ENERGY_SENSOR,
    CONF_DIFFERENCE_DEVICES,
    CONF_DISCOVERY,
    CONF_FIXED,
    CONF_FORCE_CALCULATE_GROUP_ENERGY,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_ENERGY_UPDATE_INTERVAL,
    CONF_GROUP_POWER_ENTITIES,
    CONF_GROUP_TYPE,
    CONF_POWER,
    CONF_SENSOR_TYPE,
    CONF_SUBTRACT_ENTITIES,
    DATA_DOMAIN_ENTITIES,
    DOMAIN,
    ENTRY_DATA_ENERGY_ENTITY,
    ENTRY_DATA_POWER_ENTITY,
    GroupType,
    SensorType,
)
from custom_components.powercalc.sensors.energy import RealEnergySensor
from custom_components.powercalc.sensors.group.energy_statistics import (
    GroupEnergyStatistics,
    resolve_energy_entity,
    sum_statistics,
)
from custom_components.powercalc.sensors.power import RealPowerSensor
from tests.common import create_mock_config_entry, mock_entities_in_registry, run_powercalc_setup
from tests.config_flow.test_global_configuration import create_mock_global_config_entry
from tests.sensors.group.difference_common import (
    BASE_DEVICE,
    BASE_ENERGY,
    FIRST_DEVICE,
    FIRST_ENERGY,
    SECOND_DEVICE,
    SECOND_ENERGY,
    prepare_difference_devices,
)

START = datetime(2026, 10, 8, tzinfo=UTC)
CREATED = START + timedelta(days=2, minutes=15)
A_ENERGY = "sensor.a_energy"
B_ENERGY = "sensor.b_energy"
A_POWER = "sensor.a_power"
B_POWER = "sensor.b_power"
TARGET = "sensor.history_energy"


@pytest.fixture
def mock_recorder_before_hass(request: SubRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "recorder_mock" in request.fixturenames:
        request.getfixturevalue("recorder_db_url")
        monkeypatch.setattr(migration, "Recorder", Recorder, raising=False)
        monkeypatch.setattr(recorder_helper, "Session", Session, raising=False)


def test_sum_statistics() -> None:
    statistics: dict[str, list[StatisticsRow]] = {
        A_ENERGY: [
            {"start": START.timestamp(), "state": 10, "sum": 1},
            {"start": (START + timedelta(hours=1)).timestamp(), "state": 11, "sum": 2},
            {"start": (START + timedelta(hours=2)).timestamp(), "state": 12, "sum": 3},
        ],
        B_ENERGY: [
            {"start": START.timestamp(), "state": 0.4, "sum": 0.05},
            {"start": (START + timedelta(hours=2)).timestamp(), "state": 0.1, "sum": 0.45},
            {"start": (START + timedelta(hours=3)).timestamp(), "state": None, "sum": None},
            {"start": (START + timedelta(hours=4)).timestamp(), "state": float("nan"), "sum": 1},
            {"start": (START + timedelta(hours=5)).timestamp(), "state": 1, "sum": float("inf")},
        ],
        "sensor.unrelated": [{"start": START.timestamp(), "state": 999, "sum": 999}],
    }
    for weight in [1, -1]:
        members = {A_ENERGY: 1, B_ENERGY: weight}
        aggregated = sum_statistics(statistics, members)
        assert [row["state"] for row in aggregated.history] == pytest.approx(
            [10 + 0.4 * weight, 11 + 0.4 * weight, 12 + 0.1 * weight]
        )
        assert [row["sum"] for row in aggregated.history] == pytest.approx(
            [1 + 0.05 * weight, 2 + 0.05 * weight, 3 + 0.45 * weight]
        )
        assert aggregated.calculate_seed_sum(members, {A_ENERGY: 13, B_ENERGY: 0.2}) == pytest.approx(4 + 0.55 * weight)
        assert aggregated.calculate_seed_sum(members, None) == pytest.approx(3 + 0.45 * weight)


async def import_member_history(hass: HomeAssistant) -> None:
    for entity_id, unit, values in [
        (A_ENERGY, UnitOfEnergy.WATT_HOUR, [[10000, 1000], [11000, 2000], [12000, 3000]]),
        (B_ENERGY, UnitOfEnergy.KILO_WATT_HOUR, [[0.4, 0.05], [], [0.1, 0.45]]),
    ]:
        metadata: StatisticMetaData = {
            "statistic_id": entity_id,
            "source": "recorder",
            "name": None,
            "unit_of_measurement": unit,
            "unit_class": "energy",
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
        }
        rows: list[StatisticData] = []
        for hour, value in enumerate(values):
            if value:
                rows.append({"start": START + timedelta(hours=hour), "state": value[0], "sum": value[1]})
        async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)


async def prepare_members(hass: HomeAssistant) -> None:
    mock_entities_in_registry(
        hass,
        {
            A_POWER: {"device_class": SensorDeviceClass.POWER},
            B_POWER: {"device_class": SensorDeviceClass.POWER},
            A_ENERGY: {"device_class": SensorDeviceClass.ENERGY},
            B_ENERGY: {"device_class": SensorDeviceClass.ENERGY},
        },
    )
    for power_id, energy_id, unit, value, power in [
        (A_POWER, A_ENERGY, UnitOfEnergy.WATT_HOUR, 13000, 1000),
        (B_POWER, B_ENERGY, UnitOfEnergy.KILO_WATT_HOUR, 0.2, 200),
    ]:
        hass.states.async_set(
            power_id, power, {ATTR_UNIT_OF_MEASUREMENT: UnitOfPower.WATT, ATTR_DEVICE_CLASS: SensorDeviceClass.POWER}
        )
        hass.states.async_set(
            energy_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit, ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY}
        )
        await create_mock_config_entry(
            hass, {ENTRY_DATA_POWER_ENTITY: power_id, ENTRY_DATA_ENERGY_ENTITY: energy_id}, setup=False
        )


@pytest.mark.parametrize(
    "force_integral,include_energy_entities",
    [
        (False, True),
        (True, True),
        (False, False),
    ],
)
async def test_import_group_history(
    hass: HomeAssistant,
    recorder_mock: Recorder,
    force_integral: bool,
    include_energy_entities: bool,
) -> None:
    recorder_mock.engine.echo = False
    with freeze_time(CREATED) as freezer:
        await hass.async_start()
        await prepare_members(hass)
        await import_member_history(hass)
        entry = await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.GROUP,
                CONF_GROUP_TYPE: GroupType.CUSTOM,
                CONF_NAME: "History",
                CONF_GROUP_ENERGY_ENTITIES: [A_ENERGY, B_ENERGY] if include_energy_entities else [],
                CONF_GROUP_POWER_ENTITIES: [A_POWER, B_POWER],
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
                CONF_FORCE_CALCULATE_GROUP_ENERGY: force_integral,
            },
        )
        await async_wait_recording_done(hass)
        initial = 13.2
        assert float(hass.states.get(TARGET).state) == pytest.approx(initial)

        rows = await recorder_mock.async_add_executor_job(
            partial(statistics_during_period, hass, START, statistic_ids={TARGET}, period="hour")
        )
        assert [row["state"] for row in rows[TARGET]] == pytest.approx([10.4, 11.4, 12.1])
        assert [row["sum"] for row in rows[TARGET]] == pytest.approx([1.05, 2.05, 3.45])

        # A reload must neither repeat the import nor recalibrate the live sensor.
        with patch(
            "custom_components.powercalc.sensors.group.energy_statistics.async_import_statistics"
        ) as import_mock:
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            import_mock.assert_not_called()
        assert float(hass.states.get(TARGET).state) == pytest.approx(initial)

        freezer.move_to(CREATED + timedelta(minutes=30))
        if force_integral or not include_energy_entities:
            hass.states.async_set(
                A_POWER, 1200, {ATTR_UNIT_OF_MEASUREMENT: UnitOfPower.WATT, ATTR_DEVICE_CLASS: SensorDeviceClass.POWER}
            )
            increase = 0.6
        else:
            hass.states.async_set(A_ENERGY, 14000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
            increase = 1
        await hass.async_block_till_done()
        assert float(hass.states.get(TARGET).state) == pytest.approx(initial + increase)

        await async_wait_recording_done(hass)
        do_adhoc_statistics(hass, start=CREATED + timedelta(minutes=30))
        await async_wait_recording_done(hass)
        statistics = await recorder_mock.async_add_executor_job(
            partial(statistics_during_period, hass, CREATED, statistic_ids={TARGET}, period="5minute")
        )
        assert statistics[TARGET][-1]["sum"] == pytest.approx(4.55 + increase)


async def test_subtract_group_does_not_inherit_history(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    """Subtract groups keep integrating power, even with the former opt-in setting."""
    recorder_mock.engine.echo = False
    with freeze_time(CREATED) as freezer:
        await hass.async_start()
        await create_mock_global_config_entry(hass, {CONF_DISCOVERY: {CONF_ENABLED: False}})
        await prepare_members(hass)
        await import_member_history(hass)
        with patch(
            "custom_components.powercalc.sensors.group.energy_statistics.GroupEnergyStatistics.async_initialize",
            new_callable=AsyncMock,
        ) as initialize:
            await create_mock_config_entry(
                hass,
                {
                    CONF_SENSOR_TYPE: SensorType.GROUP,
                    CONF_GROUP_TYPE: GroupType.SUBTRACT,
                    CONF_NAME: "History",
                    CONF_ENTITY_ID: A_POWER,
                    CONF_SUBTRACT_ENTITIES: [B_POWER],
                    CONF_GROUP_ENERGY_START_AT_ZERO: False,
                },
            )
            initialize.assert_not_awaited()
        assert hass.states.get(TARGET).state == STATE_UNKNOWN
        await async_wait_recording_done(hass)
        statistics = await recorder_mock.async_add_executor_job(
            partial(statistics_during_period, hass, START, statistic_ids={TARGET}, period="hour")
        )
        assert statistics == {}

        freezer.move_to(CREATED + timedelta(minutes=30))
        hass.states.async_set(
            A_POWER, 1200, {ATTR_UNIT_OF_MEASUREMENT: UnitOfPower.WATT, ATTR_DEVICE_CLASS: SensorDeviceClass.POWER}
        )
        await hass.async_block_till_done()
        assert float(hass.states.get(TARGET).state) == pytest.approx(0.4)


@pytest.mark.parametrize("domain", ["all", "light"])
async def test_domain_group_does_not_inherit_history(hass: HomeAssistant, recorder_mock: Recorder, domain: str) -> None:
    """Both domain group strategies keep their original state calculation without importing history."""
    recorder_mock.engine.echo = False
    with freeze_time(CREATED) as freezer:
        await hass.async_start()
        await create_mock_global_config_entry(hass, {CONF_DISCOVERY: {CONF_ENABLED: False}})
        assert await async_setup_component(hass, DOMAIN, {})
        await prepare_members(hass)
        await import_member_history(hass)
        hass.data[DOMAIN][DATA_DOMAIN_ENTITIES]["light"] = [
            RealPowerSensor(A_POWER, UnitOfPower.WATT),
            RealPowerSensor(B_POWER, UnitOfPower.WATT),
            RealEnergySensor(A_ENERGY),
            RealEnergySensor(B_ENERGY),
        ]
        with patch(
            "custom_components.powercalc.sensors.group.energy_statistics.GroupEnergyStatistics.async_initialize",
            new_callable=AsyncMock,
        ) as initialize:
            await create_mock_config_entry(
                hass,
                {
                    CONF_SENSOR_TYPE: SensorType.GROUP,
                    CONF_GROUP_TYPE: GroupType.DOMAIN,
                    CONF_DOMAIN: domain,
                    CONF_NAME: "History",
                    # A previously saved opt-in must not activate a statistics import.
                    CONF_GROUP_ENERGY_START_AT_ZERO: False,
                },
            )
            initialize.assert_not_awaited()
        if domain == "all":
            # The shared energy counter already accepted this YAML setting on master.
            assert float(hass.states.get(TARGET).state) == pytest.approx(13.2)
        else:
            assert hass.states.get(TARGET).state == STATE_UNKNOWN
        await async_wait_recording_done(hass)
        statistics = await recorder_mock.async_add_executor_job(
            partial(statistics_during_period, hass, START, statistic_ids={TARGET}, period="hour")
        )
        assert statistics == {}

        freezer.move_to(CREATED + timedelta(minutes=30))
        if domain == "all":
            hass.states.async_set(A_ENERGY, 14000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
            expected = 14.2
        else:
            hass.states.async_set(
                A_POWER, 1200, {ATTR_UNIT_OF_MEASUREMENT: UnitOfPower.WATT, ATTR_DEVICE_CLASS: SensorDeviceClass.POWER}
            )
            expected = 0.6
        await hass.async_block_till_done()
        assert float(hass.states.get(TARGET).state) == pytest.approx(expected)


async def test_start_at_zero_does_not_import(hass: HomeAssistant) -> None:
    history = GroupEnergyStatistics(hass, {CONF_GROUP_ENERGY_START_AT_ZERO: True})
    with patch("custom_components.powercalc.sensors.group.energy_statistics.get_instance") as recorder:
        await history.async_initialize(AsyncMock())
        recorder.assert_not_called()


async def test_unresolved_energy_sensor(hass: HomeAssistant) -> None:
    assert resolve_energy_entity(hass, A_POWER) is None


async def test_missing_energy_link_does_not_import(hass: HomeAssistant) -> None:
    power_sensor = AsyncMock()
    power_sensor.entities = {A_POWER}
    history = GroupEnergyStatistics(hass, {CONF_GROUP_ENERGY_START_AT_ZERO: False}, power_sensor=power_sensor)
    with patch("custom_components.powercalc.sensors.group.energy_statistics.get_instance") as recorder:
        await history.async_initialize(AsyncMock())
        recorder.assert_not_called()


@pytest.mark.parametrize("start_at_zero", [True, False])
async def test_difference_group_statistics(hass: HomeAssistant, recorder_mock: Recorder, start_at_zero: bool) -> None:
    recorder_mock.engine.echo = False
    with freeze_time(CREATED) as freezer:
        await hass.async_start()
        await create_mock_global_config_entry(hass, {CONF_DISCOVERY: {CONF_ENABLED: False}})
        prepare_difference_devices(hass)
        for entity_id, unit, values in [
            (BASE_ENERGY, UnitOfEnergy.WATT_HOUR, [[100000, 10000], [101000, 11000]]),
            (FIRST_ENERGY, UnitOfEnergy.KILO_WATT_HOUR, [[20, 2], [20.1, 2.1]]),
            (SECOND_ENERGY, UnitOfEnergy.WATT_HOUR, [[10000, 1000], [10200, 1200]]),
        ]:
            metadata: StatisticMetaData = {
                "statistic_id": entity_id,
                "source": "recorder",
                "name": None,
                "unit_of_measurement": unit,
                "unit_class": "energy",
                "mean_type": StatisticMeanType.NONE,
                "has_sum": True,
            }
            async_import_statistics(
                hass,
                metadata,
                [
                    {"start": START + timedelta(hours=hour), "state": value[0], "sum": value[1]}
                    for hour, value in enumerate(values)
                ],
            )
        hass.states.async_set(BASE_ENERGY, 102000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
        hass.states.async_set(FIRST_ENERGY, 20.2, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR})
        hass.states.async_set(SECOND_ENERGY, 10300, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
        await async_wait_recording_done(hass)

        entry = await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.GROUP,
                CONF_GROUP_TYPE: GroupType.DIFFERENCE,
                CONF_NAME: "History",
                CONF_BASE_DEVICE: BASE_DEVICE,
                CONF_DIFFERENCE_DEVICES: [FIRST_DEVICE, SECOND_DEVICE],
                CONF_GROUP_ENERGY_START_AT_ZERO: start_at_zero,
                CONF_GROUP_ENERGY_UPDATE_INTERVAL: 0,
            },
        )
        await async_wait_recording_done(hass)
        initial = 0 if start_at_zero else 71.5
        assert float(hass.states.get(TARGET).state) == pytest.approx(initial)
        statistics = await recorder_mock.async_add_executor_job(
            partial(statistics_during_period, hass, START, statistic_ids={TARGET}, period="hour")
        )
        if start_at_zero:
            assert statistics == {}
        else:
            assert [row["state"] for row in statistics[TARGET]] == pytest.approx([70, 70.7])
            assert [row["sum"] for row in statistics[TARGET]] == pytest.approx([7, 7.7])

        do_adhoc_statistics(hass, start=CREATED)
        await async_wait_recording_done(hass)
        with patch(
            "custom_components.powercalc.sensors.group.energy_statistics.async_import_statistics"
        ) as import_mock:
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            import_mock.assert_not_called()
        freezer.move_to(CREATED + timedelta(minutes=10))
        hass.states.async_set(BASE_ENERGY, 103000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
        hass.states.async_set(FIRST_ENERGY, 20.6, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR})
        await async_wait_recording_done(hass)
        assert float(hass.states.get(TARGET).state) == pytest.approx(initial + 0.6)
        do_adhoc_statistics(hass, start=CREATED + timedelta(minutes=10))
        await async_wait_recording_done(hass)
        statistics = await recorder_mock.async_add_executor_job(
            partial(statistics_during_period, hass, CREATED, statistic_ids={TARGET}, period="5minute")
        )
        assert statistics[TARGET][-1]["sum"] == pytest.approx(0.6 if start_at_zero else 9.1)


async def test_resolve_yaml_energy_sensor(hass: HomeAssistant) -> None:
    hass.states.async_set("light.test", "on")
    await run_powercalc_setup(
        hass,
        {
            CONF_ENTITY_ID: "light.test",
            CONF_FIXED: {CONF_POWER: 10},
            CONF_CREATE_STANDBY_ENERGY_SENSOR: True,
        },
    )
    assert resolve_energy_entity(hass, "sensor.test_power") == "sensor.test_energy"


async def test_resolve_integral_energy_sensor(hass: HomeAssistant) -> None:
    hass.states.async_set(A_ENERGY, 10, {ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY, "source": A_POWER})
    assert resolve_energy_entity(hass, A_POWER) == A_ENERGY


async def test_resolve_configured_real_power_sensor(hass: HomeAssistant) -> None:
    await create_mock_config_entry(
        hass,
        {CONF_SENSOR_TYPE: SensorType.REAL_POWER, CONF_ENTITY_ID: A_POWER, ENTRY_DATA_ENERGY_ENTITY: A_ENERGY},
        setup=False,
    )
    assert resolve_energy_entity(hass, A_POWER) == A_ENERGY


async def test_resolve_group_energy_sensor(hass: HomeAssistant) -> None:
    mock_entities_in_registry(
        hass,
        {
            A_POWER: {"platform": DOMAIN, "unique_id": "group"},
            A_ENERGY: {"platform": DOMAIN, "unique_id": "group_energy"},
        },
    )
    assert resolve_energy_entity(hass, A_POWER) == A_ENERGY


async def test_power_sensor_without_energy_link(hass: HomeAssistant) -> None:
    mock_entities_in_registry(hass, {A_POWER: {}})
    assert resolve_energy_entity(hass, A_POWER) is None


@pytest.mark.parametrize(
    "count,disabled,expected", [(0, False, None), (1, False, A_ENERGY), (2, False, None), (1, True, None)]
)
async def test_resolve_device_energy_sensor(
    hass: HomeAssistant, count: int, disabled: bool, expected: str | None
) -> None:
    entities = {A_POWER: {"device_id": "device"}}
    for energy_id in [A_ENERGY, B_ENERGY][:count]:
        entities[energy_id] = {
            "device_id": "device",
            "original_device_class": SensorDeviceClass.ENERGY,
            "disabled_by": "user" if disabled else None,
        }
    mock_entities_in_registry(hass, entities)
    assert resolve_energy_entity(hass, A_POWER) == expected


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (None, UnitOfEnergy.KILO_WATT_HOUR, None),
        ("unavailable", UnitOfEnergy.KILO_WATT_HOUR, None),
        ("nan", UnitOfEnergy.KILO_WATT_HOUR, None),
        ("10", UnitOfPower.WATT, None),
        ("1500", UnitOfEnergy.WATT_HOUR, 1.5),
    ],
)
async def test_initialize_without_recorder(
    hass: HomeAssistant, value: str | None, unit: str, expected: float | None
) -> None:
    if value is not None:
        hass.states.async_set(A_ENERGY, value, {ATTR_UNIT_OF_MEASUREMENT: unit})
    sensor = Mock(spec=SensorEntity, entity_id=TARGET, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR)
    calibrate = AsyncMock()
    history = GroupEnergyStatistics(hass, {CONF_GROUP_ENERGY_START_AT_ZERO: False}, energy_entities={A_ENERGY})
    await history.async_initialize(sensor, calibrate)
    if expected is None:
        calibrate.assert_not_called()
    else:
        calibrate.assert_awaited_once_with(str(expected))
    # Initialization is consumed even if no recorder is configured.
    await history.async_initialize(sensor, calibrate)
    assert calibrate.await_count == (0 if expected is None else 1)


async def test_initialize_without_members(hass: HomeAssistant) -> None:
    history = GroupEnergyStatistics(hass, {CONF_GROUP_ENERGY_START_AT_ZERO: False})
    sensor = Mock(spec=SensorEntity, entity_id=TARGET, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR)
    await history.async_initialize(sensor)


@pytest.mark.parametrize("existing_history", [True, False])
async def test_no_history_is_overwritten_or_invented(
    hass: HomeAssistant, recorder_mock: Recorder, existing_history: bool
) -> None:
    recorder_mock.engine.echo = False
    with freeze_time(CREATED):
        await hass.async_start()
        await prepare_members(hass)
        if existing_history:
            metadata: StatisticMetaData = {
                "statistic_id": TARGET,
                "source": "recorder",
                "name": None,
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "unit_class": "energy",
                "mean_type": StatisticMeanType.NONE,
                "has_sum": True,
            }
            async_import_statistics(hass, metadata, [{"start": START, "state": 100, "sum": 80}])
            await async_wait_recording_done(hass)
        await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.GROUP,
                CONF_NAME: "History",
                CONF_GROUP_ENERGY_ENTITIES: [A_ENERGY, B_ENERGY],
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
            },
        )
        await async_wait_recording_done(hass)
        statistics = await recorder_mock.async_add_executor_job(
            partial(statistics_during_period, hass, START, statistic_ids={TARGET}, period="hour")
        )
        if existing_history:
            assert [row["sum"] for row in statistics[TARGET]] == [80]
            assert [row["state"] for row in statistics[TARGET]] == [100]
        else:
            assert not statistics
        assert float(hass.states.get(TARGET).state) == pytest.approx(13.2)


async def test_import_preserves_recent_meter_reset(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    recorder_mock.engine.echo = False
    with freeze_time(CREATED):
        await hass.async_start()
        await prepare_members(hass)
        await import_member_history(hass)
        hass.states.async_set(A_ENERGY, 100, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
        metadata: StatisticMetaData = {
            "statistic_id": A_ENERGY,
            "source": "recorder",
            "name": None,
            "unit_of_measurement": UnitOfEnergy.WATT_HOUR,
            "unit_class": "energy",
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
        }
        recorder_mock.async_import_statistics(
            metadata,
            [{"start": CREATED - timedelta(minutes=10), "state": 50, "sum": 3500}],
            StatisticsShortTerm,
        )
        await async_wait_recording_done(hass)
        await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.GROUP,
                CONF_NAME: "History",
                CONF_GROUP_ENERGY_ENTITIES: [A_ENERGY, B_ENERGY],
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
            },
        )
        await async_wait_recording_done(hass)
        assert float(hass.states.get(TARGET).state) == pytest.approx(0.3)
        statistics = await recorder_mock.async_add_executor_job(
            partial(
                statistics_during_period, hass, CREATED - timedelta(minutes=5), statistic_ids={TARGET}, period="5minute"
            )
        )
        assert statistics[TARGET][-1]["sum"] == pytest.approx(4.1)


async def test_import_failure_keeps_group_operational(
    hass: HomeAssistant, recorder_mock: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    recorder_mock.engine.echo = False
    await hass.async_start()
    await prepare_members(hass)
    with patch(
        "custom_components.powercalc.sensors.group.energy_statistics.statistics_during_period",
        side_effect=SQLAlchemyError("Database unavailable"),
    ):
        await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.GROUP,
                CONF_NAME: "History",
                CONF_GROUP_ENERGY_ENTITIES: [A_ENERGY, B_ENERGY],
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
            },
        )
    assert "Could not initialize group energy statistics" in caplog.text
    assert float(hass.states.get(TARGET).state) == pytest.approx(13.2)
    hass.states.async_set(A_ENERGY, 14000, {ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.WATT_HOUR})
    await hass.async_block_till_done()
    assert float(hass.states.get(TARGET).state) == pytest.approx(14.2)


@pytest.mark.parametrize("recorder_config", [{"exclude": {"entities": [TARGET]}}])
async def test_recorder_excluded_group_is_not_imported(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    recorder_mock.engine.echo = False
    await hass.async_start()
    await prepare_members(hass)
    await import_member_history(hass)
    with patch("custom_components.powercalc.sensors.group.energy_statistics.async_import_statistics") as import_mock:
        await create_mock_config_entry(
            hass,
            {
                CONF_SENSOR_TYPE: SensorType.GROUP,
                CONF_NAME: "History",
                CONF_GROUP_ENERGY_ENTITIES: [A_ENERGY, B_ENERGY],
                CONF_GROUP_ENERGY_START_AT_ZERO: False,
            },
        )
        import_mock.assert_not_called()
    assert float(hass.states.get(TARGET).state) == pytest.approx(13.2)


@pytest.mark.parametrize("native_value", [None, float("nan")])
async def test_unavailable_target_does_not_import(
    hass: HomeAssistant, recorder_mock: Recorder, native_value: float | None
) -> None:
    recorder_mock.engine.echo = False
    await hass.async_start()
    await prepare_members(hass)
    sensor = Mock(
        spec=SensorEntity,
        entity_id=TARGET,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        native_value=native_value,
    )
    history = GroupEnergyStatistics(hass, {CONF_GROUP_ENERGY_START_AT_ZERO: False}, energy_entities={A_ENERGY})
    with patch("custom_components.powercalc.sensors.group.energy_statistics.async_import_statistics") as import_mock:
        await history.async_initialize(sensor)
        import_mock.assert_not_called()
