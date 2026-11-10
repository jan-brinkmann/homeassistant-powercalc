"""Consumption aggregation, source resolution and persisted live references."""

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import StatisticsRow
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.powercalc.const import (
    ATTR_IS_GROUP,
    CONF_FORCE_CALCULATE_GROUP_ENERGY,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_MEMBER_SENSORS,
    CONF_GROUP_TYPE,
    CONF_SUB_GROUPS,
    DOMAIN,
    ENTRY_DATA_ENERGY_ENTITY,
    GroupType,
)
from custom_components.powercalc.sensors.group.history import (
    AggregationState,
    HistoryImportError,
    aggregate_statistics,
)
from custom_components.powercalc.sensors.group.history_import import ImportJournal
from custom_components.powercalc.sensors.group.history_sources import resolve_history_sources
from custom_components.powercalc.sensors.group.history_transition import apply_source_state
from tests.common import create_mock_group_entry


def samples(values: list[float | None], offset: int = 0) -> list[StatisticsRow]:
    return [{"start": float((hour + offset) * 3600), "sum": value} for hour, value in enumerate(values)]


@pytest.mark.parametrize(
    "source_rows,expected",
    [
        ({"a": samples([12, 12.3, 12.5])}, [0, 0.3, 0.5]),
        ({"a": samples([12, 12.3]), "b": samples([987, 987.2])}, [0, 0.5]),
        ({"a": samples([100, 101, 100.7, 101.2])}, [0, 1, 0.7, 1.2]),
        ({"a": samples([100, 100, 100])}, [0, 0, 0]),
        ({"a": samples([100, None, 200, 200.2])}, [0, 0.2]),
        ({"a": samples([2, 2.1, 2.2]), "b": samples([50, 50.3], 1)}, [0, 0.1, 0.5]),
        ({"a": samples([2, 2.1]), "b": []}, [0, 0.1]),
    ],
)
def test_aggregate_known_consumption(source_rows: dict[str, list[StatisticsRow]], expected: list[float | None]) -> None:
    checkpoint = AggregationState()
    result = aggregate_statistics(list(source_rows), source_rows, checkpoint)
    assert [row.get("sum") for row in result] == pytest.approx(expected)
    assert [row.get("state") for row in result] == pytest.approx(expected)


def test_gap_is_not_distributed_and_batches_preserve_reference() -> None:
    checkpoint = AggregationState()
    aggregate_statistics(["a"], {"a": samples([10, 10.3])}, checkpoint)
    result = aggregate_statistics(["a", "a"], {"a": samples([20, 20.2], 4)}, checkpoint)
    assert [row.get("sum") for row in result] == pytest.approx([0.5])
    assert result[0]["start"].timestamp() == 5 * 3600
    assert checkpoint.known_intervals == 2


@pytest.mark.parametrize(
    "rows",
    [
        [{"start": 1.0, "sum": 2.0}],
        [{"start": 0.0, "sum": float("nan")}],
        [{"start": 0.0, "sum": 2.0}, {"start": 0.0, "sum": 3.0}],
    ],
)
def test_invalid_statistics_are_rejected(rows: list[StatisticsRow]) -> None:
    with pytest.raises(HistoryImportError):
        aggregate_statistics(["a"], {"a": rows}, AggregationState())


def test_device_reset_uses_sum_not_state() -> None:
    rows = [
        {"start": 0.0, "sum": 100.0, "state": 1000.0},
        {"start": 3600.0, "sum": 100.2, "state": 0.2},
    ]
    assert aggregate_statistics(["a"], {"a": rows}, AggregationState())[-1]["sum"] == pytest.approx(0.2)


def test_utc_hours_across_daylight_saving_change() -> None:
    start = datetime(2026, 10, 25, tzinfo=UTC)
    rows = [{"start": (start + timedelta(hours=i)).timestamp(), "sum": i * 0.2} for i in range(4)]
    result = aggregate_statistics(["a"], {"a": rows}, AggregationState())
    assert [row["sum"] for row in result] == pytest.approx([0, 0.2, 0.4, 0.6])
    assert all(row["start"].tzinfo is UTC for row in result)


def source_state(value: str, minute: int, **attributes: Any) -> State:  # noqa: ANN401
    return State(
        "sensor.source",
        value,
        {"unit_of_measurement": "kWh", "state_class": "total", **attributes},
        last_updated=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute),
    )


@pytest.mark.parametrize("unit,value", [("Wh", "300"), ("kWh", "0.3"), ("MWh", "0.0003"), ("MJ", "1.08")])
def test_live_unit_conversion(unit: str, value: str) -> None:
    journal = ImportJournal("sensor.target", "kWh", ["sensor.source"], 0)
    apply_source_state(journal, source_state("0", 0))
    apply_source_state(journal, source_state(value, 1, unit_of_measurement=unit))
    assert journal.live_total == pytest.approx(0.3)


@pytest.mark.parametrize(
    "state_class,values,expected",
    [("total", [100, 101, 100.8], 0.8), ("total_increasing", [100, 101, 0.2, 0.5], 1.5)],
)
def test_live_resets_and_corrections(state_class: str, values: list[float], expected: float) -> None:
    journal = ImportJournal("sensor.target", "kWh", ["sensor.source"], 0)
    for minute, value in enumerate(values):
        state = source_state(str(value), minute, state_class=state_class)
        apply_source_state(journal, state)
        apply_source_state(journal, state)
    assert journal.live_total == pytest.approx(expected)


async def test_subgroups_and_direct_references_are_deduplicated(hass: HomeAssistant) -> None:
    subgroup = await create_mock_group_entry(
        hass,
        "Child",
        {
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.a"],
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            ENTRY_DATA_ENERGY_ENTITY: "sensor.child_energy",
        },
        setup=False,
    )
    parent = await create_mock_group_entry(
        hass,
        "Parent",
        {CONF_SUB_GROUPS: [subgroup.entry_id], CONF_GROUP_ENERGY_ENTITIES: ["sensor.a", "sensor.child_energy"]},
        setup=False,
    )
    assert await resolve_history_sources(hass, parent) == {"sensor.a"}
    hass.config_entries.async_update_entry(subgroup, data={**subgroup.data, CONF_SUB_GROUPS: [parent.entry_id]})
    with pytest.raises(HistoryImportError, match="Cyclic"):
        await resolve_history_sources(hass, parent)


def test_overlapping_batches_are_rejected() -> None:
    checkpoint = AggregationState()
    rows = {"a": samples([10, 11])}
    aggregate_statistics(["a"], rows, checkpoint)
    with pytest.raises(HistoryImportError, match="chronological"):
        aggregate_statistics(["a"], rows, checkpoint)


@pytest.mark.parametrize(
    "value,attributes,error",
    [
        ("2", {"state_class": "measurement"}, "cumulative"),
        ("2", {"unit_of_measurement": "W"}, "unit"),
        ("bad", {}, "Invalid"),
        ("nan", {}, "Invalid"),
        ("-1", {"state_class": "total_increasing"}, "Invalid"),
    ],
)
def test_invalid_live_sources_do_not_change_total(value: str, attributes: dict[str, str], error: str) -> None:
    journal = ImportJournal("sensor.target", "kWh", ["sensor.source"], 0)
    with pytest.raises(HistoryImportError, match=error):
        apply_source_state(journal, source_state(value, 0, **attributes))
    assert journal.live_total == 0


def test_explicit_reset_and_changed_state_class() -> None:
    journal = ImportJournal("sensor.target", "kWh", ["sensor.source"], 0)
    apply_source_state(journal, source_state("100", 0, last_reset="2025-12-01T00:00:00+00:00"))
    apply_source_state(journal, source_state("0.2", 1, last_reset="2026-01-01T00:00:00+00:00"))
    apply_source_state(journal, source_state("0.3", 2, last_reset="2026-01-01T01:00:00+01:00"))
    assert journal.live_total == pytest.approx(0.3)
    with pytest.raises(HistoryImportError, match="State class changed"):
        apply_source_state(journal, source_state("0.4", 3, state_class="total_increasing"))


@pytest.mark.parametrize(
    "config,error",
    [
        ({CONF_SUB_GROUPS: ["missing"]}, "Subgroup not found"),
        ({CONF_GROUP_TYPE: GroupType.STANDBY}, "supported group"),
        ({CONF_FORCE_CALCULATE_GROUP_ENERGY: True}, "supported group"),
        ({CONF_GROUP_MEMBER_SENSORS: ["missing"]}, "no existing energy"),
        ({}, "No cumulative"),
        ({CONF_GROUP_ENERGY_ENTITIES: ["sensor.yaml_group"]}, "Cannot resolve"),
    ],
)
async def test_unresolvable_sources_are_rejected(hass: HomeAssistant, config: dict[str, Any], error: str) -> None:
    hass.states.async_set("sensor.yaml_group", "10", {ATTR_IS_GROUP: True})
    entry = await create_mock_group_entry(hass, "History", config, setup=False)
    with pytest.raises(HistoryImportError, match=error):
        await resolve_history_sources(hass, entry)


@pytest.mark.parametrize("start_at_zero", [True, False])
async def test_invalid_group_setup_creates_repair(hass: HomeAssistant, start_at_zero: bool) -> None:
    await create_mock_group_entry(
        hass,
        "History",
        {
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_GROUP_ENERGY_START_AT_ZERO: start_at_zero,
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.source"] if start_at_zero else [],
        },
    )
    assert hass.states.get("sensor.history_energy").attributes["history_import_status"] == "failed"
    assert any(
        domain == DOMAIN and issue.translation_key == "group_history_failed"
        for (domain, _), issue in ir.async_get(hass).issues.items()
    )
