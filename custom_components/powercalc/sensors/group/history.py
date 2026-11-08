"""Aggregate consumption statistics without inventing observations between samples."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite

from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import StatisticsRow
from homeassistant.exceptions import HomeAssistantError


class HistoryImportError(HomeAssistantError):
    """The history cannot be imported with a reliable continuation."""


@dataclass
class AggregationState:
    """Checkpoint carried across chronological batches, in the target energy unit."""

    total: float = 0.0
    previous: dict[str, StatisticsRow] = field(default_factory=dict)
    missing_intervals: int = 0
    known_intervals: int = 0
    last_start: float | None = None


def aggregate_statistics(
    sources: list[str],
    rows: dict[str, list[StatisticsRow]],
    checkpoint: AggregationState,
    interval: timedelta = timedelta(hours=1),
    source_weights: dict[str, int] | None = None,
    state_offset: float = 0.0,
) -> list[StatisticData]:
    """Accumulate only adjacent sum differences, preserving signed corrections.

    A first sample establishes a reference. A gap establishes a new reference;
    its multi-interval difference is deliberately excluded, never interpolated.
    """
    sources = sorted(set(sources))
    weights = source_weights or {}
    seconds = interval.total_seconds()
    by_start = index_statistics(sources, rows, seconds)

    result: list[StatisticData] = []
    for timestamp, samples in sorted(by_start.items()):
        initial_reference = not checkpoint.previous
        if checkpoint.last_start is not None:
            if timestamp <= checkpoint.last_start:
                raise HistoryImportError("Statistics batches must be chronological")
            checkpoint.missing_intervals += max(0, int((timestamp - checkpoint.last_start) / seconds) - 1)
        checkpoint.last_start = timestamp
        known = 0
        for source in sources:
            sample = samples.get(source)
            previous = checkpoint.previous.get(source)
            if sample is None:
                continue
            value = sample.get("sum")
            if (
                previous is not None
                and previous["start"] == timestamp - seconds
                and (previous_value := previous.get("sum")) is not None
                and value is not None
            ):
                checkpoint.total += (value - previous_value) * weights.get(source, 1)
                known += 1
            checkpoint.previous[source] = sample

        if known != len(sources) and not initial_reference:
            checkpoint.missing_intervals += 1
        if known:
            checkpoint.known_intervals += 1
        if known or initial_reference:
            result.append(
                StatisticData(
                    start=datetime.fromtimestamp(timestamp, UTC),
                    state=checkpoint.total + state_offset,
                    sum=checkpoint.total,
                )
            )
        # Omit unknown hours rather than writing NULL sums: HA's change query uses
        # zero when its preceding reference row is NULL, which would create a spike
        # when the selected date range starts immediately after such a row.
    return result


def index_statistics(
    sources: list[str], rows: dict[str, list[StatisticsRow]], seconds: float
) -> dict[float, dict[str, StatisticsRow]]:
    by_start: dict[float, dict[str, StatisticsRow]] = {}
    for source in sources:
        for row in rows.get(source, []):
            timestamp = row["start"]
            value = row.get("sum")
            if timestamp % seconds or not isfinite(timestamp):
                raise HistoryImportError(f"Invalid statistic timestamp for {source}: {timestamp}")
            if value is not None and not isfinite(value):
                raise HistoryImportError(f"Invalid statistic sum for {source}: {value}")
            if source in by_start.setdefault(timestamp, {}):
                raise HistoryImportError(f"Duplicate statistic for {source}: {timestamp}")
            by_start[timestamp][source] = row
    return by_start
