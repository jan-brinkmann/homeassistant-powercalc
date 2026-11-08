"""Live accumulation using the same cumulative-sensor semantics as recorder."""

from math import isfinite
from typing import Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import State
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from custom_components.powercalc.const import UNAVAILABLE_STATES

from .history import HistoryImportError
from .history_import import ImportJournal


def apply_source_state(journal: ImportJournal, state: State, weight: int = 1) -> None:
    """Apply one observation once; a new member establishes its own zero point."""
    if state.state in UNAVAILABLE_STATES:
        return
    value = read_energy_value(state, journal.unit)
    state_class = state.attributes["state_class"]
    previous = journal.members.get(state.entity_id)
    timestamp = state.last_updated.timestamp()
    if previous is not None and timestamp <= previous["updated"] and previous.get("weight", 1) == weight:
        return
    reset = normalize_last_reset(state.attributes.get("last_reset"))
    if previous is not None and previous.get("weight", 1) == weight:
        if previous["state_class"] != state_class:
            raise HistoryImportError(f"State class changed for {state.entity_id}")
        delta = value - previous["value"]
        if state_class == SensorStateClass.TOTAL_INCREASING:
            if value < 0.9 * previous["value"]:
                delta = value
        elif reset is not None and reset != previous["last_reset"]:
            delta = value
        journal.live_total += delta * weight
    member: dict[str, Any] = {
        "value": value,
        "state_class": state_class,
        "last_reset": reset,
        "updated": timestamp,
        "weight": weight,
    }
    journal.members[state.entity_id] = member


def read_energy_value(state: State, target_unit: str) -> float:
    state_class = state.attributes.get("state_class")
    if state_class not in [SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING]:
        raise HistoryImportError(f"{state.entity_id} is not a cumulative energy sensor")
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit not in EnergyConverter.VALID_UNITS:
        raise HistoryImportError(f"Unsupported energy unit for {state.entity_id}: {unit}")
    try:
        value = EnergyConverter.convert(float(state.state), unit, target_unit)
    except ValueError as error:
        raise HistoryImportError(f"Invalid energy value for {state.entity_id}: {state.state}") from error
    if not isfinite(value) or (state_class == SensorStateClass.TOTAL_INCREASING and value < 0):
        raise HistoryImportError(f"Invalid cumulative energy value for {state.entity_id}: {state.state}")
    return value


def normalize_last_reset(value: object) -> str | None:
    if not isinstance(value, str) or (parsed := dt_util.parse_datetime(value)) is None:
        return None
    return dt_util.as_utc(parsed).isoformat()
