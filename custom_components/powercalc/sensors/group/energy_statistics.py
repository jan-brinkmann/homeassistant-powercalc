"""One-time initialization of a new group's energy statistics."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING, cast

from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter
from sqlalchemy.exc import SQLAlchemyError

from custom_components.powercalc.const import (
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_SENSOR_TYPE,
    DATA_CONFIGURED_ENTITIES,
    DOMAIN,
    ENTRY_DATA_ENERGY_ENTITY,
    ENTRY_DATA_POWER_ENTITY,
    SensorType,
)
from custom_components.powercalc.sensors.energy import EnergySensor, VirtualStandbyEnergySensor

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.components.sensor import SensorEntity

    from custom_components.powercalc.sensors.group.custom import GroupedPowerSensor

_LOGGER = logging.getLogger(__name__)
_HISTORY_START = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class EnergySample:
    state: float
    consumption: float


@dataclass
class AggregatedStatistics:
    history: list[StatisticData]
    latest: dict[str, EnergySample]

    def calculate_seed_sum(self, members: dict[str, int], current_values: dict[str, float] | None) -> float:
        total = 0.0
        for entity_id, sample in self.latest.items():
            consumption = sample.consumption
            if current_values is not None:
                consumption += max(0, current_values.get(entity_id, sample.state) - sample.state)
            total += consumption * members[entity_id]
        return total


def sum_statistics(statistics: dict[str, list[StatisticsRow]], members: dict[str, int]) -> AggregatedStatistics:
    """Combine cumulative states and sums, carrying values forward across missing hours."""
    rows_by_start: dict[float, dict[str, EnergySample]] = defaultdict(dict)
    for entity_id, rows in statistics.items():
        if entity_id not in members:
            continue
        for row in rows:
            state = row.get("state")
            consumption = row.get("sum")
            if state is None or consumption is None or not math.isfinite(state) or not math.isfinite(consumption):
                continue
            rows_by_start[row["start"]][entity_id] = EnergySample(state, consumption)

    latest: dict[str, EnergySample] = {}
    result: list[StatisticData] = []
    for timestamp, samples in sorted(rows_by_start.items()):
        latest.update(samples)
        result.append(
            {
                "start": dt_util.utc_from_timestamp(timestamp),
                "state": sum(sample.state * members[entity_id] for entity_id, sample in latest.items()),
                "sum": sum(sample.consumption * members[entity_id] for entity_id, sample in latest.items()),
            }
        )
    return AggregatedStatistics(result, latest)


@callback
def resolve_energy_entity(hass: HomeAssistant, power_entity_id: str) -> str | None:
    """Resolve an explicitly linked energy sensor, or an unambiguous device energy sensor."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        configured_power = entry.data.get(ENTRY_DATA_POWER_ENTITY)
        if entry.data.get(CONF_SENSOR_TYPE) == SensorType.REAL_POWER:
            configured_power = entry.data.get(CONF_ENTITY_ID)
        if configured_power == power_entity_id and (energy_entity_id := entry.data.get(ENTRY_DATA_ENERGY_ENTITY)):
            return str(energy_entity_id)

    configured_entities = cast(
        dict[str, list[tuple[Entity, bool]]], hass.data.get(DOMAIN, {}).get(DATA_CONFIGURED_ENTITIES, {})
    )
    for entities in configured_entities.values():
        if any(entity.entity_id == power_entity_id for entity, _ in entities):
            candidates = [
                entity.entity_id
                for entity, _ in entities
                if isinstance(entity, EnergySensor) and not isinstance(entity, VirtualStandbyEnergySensor)
            ]
            if len(candidates) == 1:
                return candidates[0]

    # Riemann integral sensors expose their power source in the entity's attributes.
    candidates = [
        state.entity_id
        for state in hass.states.async_all("sensor")
        if state.attributes.get(ATTR_DEVICE_CLASS) == SensorDeviceClass.ENERGY
        and state.attributes.get("source") == power_entity_id
    ]
    if len(candidates) == 1:
        return candidates[0]

    return _resolve_registry_energy_sensor(hass, power_entity_id)


@callback
def _resolve_registry_energy_sensor(hass: HomeAssistant, power_entity_id: str) -> str | None:
    registry = er.async_get(hass)
    power_entry = registry.async_get(power_entity_id)
    if power_entry is None:
        return None
    if power_entry.platform == DOMAIN and (
        energy_entity := registry.async_get_entity_id("sensor", DOMAIN, f"{power_entry.unique_id}_energy")
    ):
        return energy_entity
    if power_entry.device_id is None:
        return None
    candidates = [
        entry.entity_id
        for entry in er.async_entries_for_device(registry, power_entry.device_id)
        if (entry.device_class or entry.original_device_class) == SensorDeviceClass.ENERGY and not entry.disabled
    ]
    return str(candidates[0]) if len(candidates) == 1 else None


class GroupEnergyStatistics:
    def __init__(
        self,
        hass: HomeAssistant,
        config: ConfigType,
        energy_entities: set[str] | None = None,
        power_sensor: GroupedPowerSensor | None = None,
        member_weights: dict[str, int] | None = None,
    ) -> None:
        self._hass = hass
        self._enabled = not config.get(CONF_GROUP_ENERGY_START_AT_ZERO, True)
        self._energy_entities = energy_entities
        self._power_sensor = power_sensor
        self._member_weights = member_weights

    @callback
    def set_target(self, entity_id: str) -> None:
        """Remember whether the target existed before entity registration."""
        self._enabled = self._enabled and er.async_get(self._hass).async_get(entity_id) is None

    async def async_initialize(
        self,
        sensor: SensorEntity,
        calibrate: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if not self._enabled:
            return
        self._enabled = False

        members = self._resolve_members()
        members.pop(sensor.entity_id, None)
        unit = sensor.native_unit_of_measurement
        if not members or unit is None or unit not in EnergyConverter.VALID_UNITS:
            return

        try:
            current_values = self._get_current_values(members, unit)
            if calibrate and current_values is not None:
                await calibrate(str(sum(value * members[entity_id] for entity_id, value in current_values.items())))
            if RECORDER_DOMAIN not in self._hass.data:
                return
            recorder = get_instance(self._hass)
            if recorder.entity_filter and not recorder.entity_filter(sensor.entity_id):
                return

            # Freeze the seed before awaiting database I/O. Subsequent live updates
            # are then counted by the recorder relative to this state.
            current_state = float(str(sensor.native_value)) if sensor.native_value is not None else None
            if current_state is None or not math.isfinite(current_state):
                return
            now = dt_util.utcnow()
            aggregated = await recorder.async_add_executor_job(
                self._read_statistics, sensor.entity_id, members, unit, now
            )
            if aggregated is None:
                return

            metadata: StatisticMetaData = {
                "statistic_id": sensor.entity_id,
                "source": RECORDER_DOMAIN,
                "name": None,
                "unit_of_measurement": unit,
                "unit_class": EnergyConverter.UNIT_CLASS,
                "mean_type": StatisticMeanType.NONE,
                "has_sum": True,
            }
            async_import_statistics(self._hass, metadata, aggregated.history)
            # The sensor recorder continues from short-term statistics. A seed
            # prevents its first sample from resetting the imported sum to zero.
            seed: StatisticData = {
                "start": now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0) - timedelta(minutes=5),
                "state": current_state,
                "sum": aggregated.calculate_seed_sum(members, current_values),
            }
            recorder.async_import_statistics(metadata, [seed], StatisticsShortTerm)
            _LOGGER.debug(
                "%s: Imported %s hours of member energy statistics", sensor.entity_id, len(aggregated.history)
            )
        except HomeAssistantError, SQLAlchemyError, ValueError:
            _LOGGER.exception("%s: Could not initialize group energy statistics", sensor.entity_id)

    def _resolve_members(self) -> dict[str, int]:
        if self._member_weights is not None:
            return self._member_weights.copy()
        if self._energy_entities:
            return dict.fromkeys(self._energy_entities, 1)
        if self._power_sensor is None:
            return {}
        members: dict[str, int] = {}
        for entity_id in self._power_sensor.entities:
            energy_entity = resolve_energy_entity(self._hass, entity_id)
            if energy_entity is None:
                _LOGGER.warning(
                    "Cannot inherit group energy statistics: no unambiguous energy sensor for %s", entity_id
                )
                return {}
            members[energy_entity] = members.get(energy_entity, 0) + 1
        return members

    def _get_current_values(self, members: dict[str, int], unit: str) -> dict[str, float] | None:
        values: dict[str, float] = {}
        for entity_id in members:
            state = self._hass.states.get(entity_id)
            if state is None:
                return None
            try:
                value = float(state.state)
            except ValueError:
                return None
            member_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            if not math.isfinite(value) or member_unit not in EnergyConverter.VALID_UNITS:
                return None
            values[entity_id] = EnergyConverter.convert(value, member_unit, unit)
        return values

    def _read_statistics(
        self, entity_id: str, members: dict[str, int], unit: str, now: datetime
    ) -> AggregatedStatistics | None:
        # An entity can have retained statistics even after its registry entry was
        # deleted. Never overwrite those when a group reuses the same entity ID.
        if get_metadata(self._hass, statistic_ids={entity_id}):
            return None
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        statistics = statistics_during_period(
            self._hass,
            _HISTORY_START,
            hour_start,
            set(members),
            "hour",
            units={"energy": unit},
            types={"state", "sum"},
        )
        aggregated = sum_statistics(statistics, members)
        if not aggregated.history:
            return None
        # Include already recorded consumption in the current hour in the seed,
        # without adding incomplete hourly rows to the long-term history.
        recent = statistics_during_period(
            self._hass, hour_start, now, set(members), "5minute", units={"energy": unit}, types={"state", "sum"}
        )
        aggregated.latest.update(sum_statistics(recent, members).latest)
        return aggregated
