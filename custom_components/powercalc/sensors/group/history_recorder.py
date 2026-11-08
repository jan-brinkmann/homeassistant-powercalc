"""Recorder access for a resumable, exclusively owned group statistics import."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from math import isclose
from typing import Literal, TypeVar

from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    get_last_short_term_statistics,
    get_last_statistics,
    get_metadata,
    import_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask, SynchronizeTask
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter

from .history import HistoryImportError

T = TypeVar("T")
Period = Literal["hour", "5minute"]


class HistoryRecorder:
    def __init__(self, hass: HomeAssistant, target: str, unit: str) -> None:
        self.hass = hass
        try:
            self.recorder = get_instance(hass)
        except KeyError as error:
            raise HistoryImportError("Recorder must be enabled to import group history") from error
        if self.recorder.entity_filter and not self.recorder.entity_filter(target):
            raise HistoryImportError(f"The group target is excluded from recorder: {target}")
        self.target = target
        self.unit = unit
        self.metadata = StatisticMetaData(
            statistic_id=target,
            source="recorder",
            name=None,
            unit_of_measurement=unit,
            unit_class="energy",
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
        )

    async def execute(self, function: Callable[..., T], *args: object, **kwargs: object) -> T:
        return await self.recorder.async_add_executor_job(partial(function, *args, **kwargs))

    async def read(
        self, sources: list[str], start: datetime, end: datetime, period: Period = "hour"
    ) -> dict[str, list[StatisticsRow]]:
        return await self.execute(
            statistics_during_period,
            self.hass,
            start,
            end,
            set(sources),
            period,
            {"energy": self.unit},
            {"sum", "state", "last_reset"},
        )

    async def validate_sources(self, sources: list[str]) -> None:
        metadata = await self.execute(get_metadata, self.hass, statistic_ids=set(sources))
        invalid = [
            source
            for source in sources
            if source not in metadata
            or metadata[source][1]["unit_class"] != "energy"
            or not metadata[source][1]["has_sum"]
            or metadata[source][1]["source"] != "recorder"
            or metadata[source][1]["unit_of_measurement"] not in EnergyConverter.VALID_UNITS
            or (self.recorder.entity_filter is not None and not self.recorder.entity_filter(source))
        ]
        if invalid:
            raise HistoryImportError(f"Sources have no suitable consumption statistics: {', '.join(invalid)}")

    async def require_empty_target(self) -> None:
        await self.synchronize()
        if await self.execute(get_metadata, self.hass, statistic_ids={self.target}):
            raise HistoryImportError(f"Statistics already exist for {self.target}; nothing was overwritten")

    async def synchronize(self) -> None:
        """Wait even when the recorder has popped a job but has not committed it."""
        future = self.hass.loop.create_future()
        self.recorder.queue_task(SynchronizeTask(future))
        await future

    async def verify_live_reference(self, boundary: datetime, total: float, state_offset: float = 0.0) -> None:
        await self.synchronize()
        rows = await self.execute(get_last_short_term_statistics, self.hass, 1, self.target, False, {"state", "sum"})
        latest = rows.get(self.target, [])
        expected: StatisticsRow = {
            "start": (boundary - timedelta(minutes=5)).timestamp(),
            "state": total + state_offset,
            "sum": total,
        }
        if len(latest) != 1 or latest[0]["start"] != expected["start"] or not rows_match(latest[0], expected):
            raise HistoryImportError(f"The recorder transition reference changed for {self.target}")

    async def import_batch(
        self,
        rows: list[StatisticsRow],
        period: Period,
        *,
        first_batch: bool = False,
        previous_rows: list[StatisticsRow] | None = None,
    ) -> None:
        """Replay a journalled batch only if existing rows agree, then verify the commit."""
        if not rows:
            return
        future: asyncio.Future[None] = self.hass.loop.create_future()
        self.recorder.queue_task(ImportGroupStatisticsTask(self, rows, period, future, first_batch, previous_rows))
        await future

    def import_in_recorder(
        self, rows: list[StatisticsRow], period: Period, first_batch: bool, previous_rows: list[StatisticsRow] | None
    ) -> None:
        """Validate and import on the recorder thread, serialized with other writers."""
        self.validate_target_metadata()
        if first_batch:
            self.validate_first_batch(rows)
        start = datetime.fromtimestamp(rows[0]["start"], UTC)
        end = datetime.fromtimestamp(rows[-1]["start"], UTC) + (
            timedelta(hours=1) if period == "hour" else timedelta(minutes=5)
        )
        types: set[Literal["change", "last_reset", "max", "mean", "min", "state", "sum"]] = {"sum", "state"}
        read_batch = partial(
            statistics_during_period,
            self.hass,
            start,
            end,
            {self.target},
            period,
            {"energy": self.unit},
            types,
        )
        existing = read_batch().get(self.target, [])
        expected = {row["start"]: row for row in rows}
        validate_existing_rows(self.target, existing, expected, previous_rows)
        statistics: list[StatisticData] = []
        for row in rows:
            statistic = StatisticData(start=datetime.fromtimestamp(row["start"], UTC))
            if (value := row.get("state")) is not None:
                statistic["state"] = value
            if (value := row.get("sum")) is not None:
                statistic["sum"] = value
            statistics.append(statistic)
        # The core import API owns the transaction and short-term cache refresh.
        # Running the checks in this same queue task closes the race with other
        # recorder import/correction jobs; no direct database writes are used.
        table = Statistics if period == "hour" else StatisticsShortTerm
        if not import_statistics(self.recorder, self.metadata, statistics, table):
            raise HistoryImportError(f"Recorder could not commit the statistics batch for {self.target}")
        stored = read_batch().get(self.target, [])
        if len(stored) != len(rows) or any(
            row["start"] not in expected or not rows_match(row, expected[row["start"]]) for row in stored
        ):
            raise HistoryImportError(f"Recorder did not confirm the statistics batch for {self.target}")

    def validate_target_metadata(self) -> None:
        target_metadata = get_metadata(self.hass, statistic_ids={self.target})
        if not target_metadata:
            return
        existing_metadata = target_metadata[self.target][1]
        keys: list[Literal["source", "unit_of_measurement", "unit_class", "mean_type", "has_sum"]] = [
            "source",
            "unit_of_measurement",
            "unit_class",
            "mean_type",
            "has_sum",
        ]
        if any(existing_metadata[key] != self.metadata[key] for key in keys):
            raise HistoryImportError(f"Conflicting statistics metadata for {self.target}")

    def validate_first_batch(self, rows: list[StatisticsRow]) -> None:
        """An existing target must be exactly a replay of our first journalled batch."""
        if not get_metadata(self.hass, statistic_ids={self.target}):
            return
        short_term = get_last_short_term_statistics(self.hass, 1, self.target, False, {"state", "sum"})
        existing = get_last_statistics(self.hass, len(rows) + 1, self.target, False, {"state", "sum"})
        expected = {row["start"]: row for row in rows}
        actual = existing.get(self.target, [])
        if (
            short_term
            or len(actual) != len(rows)
            or any(row["start"] not in expected or not rows_match(row, expected[row["start"]]) for row in actual)
        ):
            raise HistoryImportError(f"Statistics appeared for {self.target} after the import was prepared")


@dataclass
class ImportGroupStatisticsTask(RecorderTask):
    importer: HistoryRecorder
    rows: list[StatisticsRow]
    period: Period
    future: asyncio.Future[None]
    first_batch: bool
    previous_rows: list[StatisticsRow] | None

    def run(self, instance: Recorder) -> None:
        try:
            self.importer.import_in_recorder(self.rows, self.period, self.first_batch, self.previous_rows)
        except Exception as error:  # noqa: BLE001
            # Propagate worker failures to the awaiting import, including unexpected
            # API failures. Never leave a pending future or crash the recorder thread.
            failure = HistoryImportError(f"Statistics import failed for {self.importer.target}: {error}")
            failure.__cause__ = error
            instance.hass.loop.call_soon_threadsafe(self.finish, failure)
        else:
            instance.hass.loop.call_soon_threadsafe(self.finish, None)

    def finish(self, error: Exception | None) -> None:
        if self.future.done():
            return
        if error is not None:
            self.future.set_exception(error)
        else:
            self.future.set_result(None)


def validate_existing_rows(
    target: str,
    existing: list[StatisticsRow],
    expected: dict[float, StatisticsRow],
    previous_rows: list[StatisticsRow] | None,
) -> None:
    """Permit only exact retries or a journalled state-only correction of owned rows."""
    previous = {row["start"]: row for row in previous_rows or []}
    if previous_rows is not None:
        if previous.keys() != expected.keys() or len(existing) != len(expected):
            raise HistoryImportError(f"Statistics to align changed or disappeared for {target}")
        if any(previous[start].get("sum") != row.get("sum") for start, row in expected.items()):
            raise HistoryImportError(f"Counter alignment must preserve consumption sums for {target}")
    for row in existing:
        wanted = expected.get(row["start"])
        old = previous.get(row["start"])
        if wanted is None or (not rows_match(row, wanted) and not (old and rows_match(row, old))):
            raise HistoryImportError(f"Conflicting statistics for {target} at {row['start']}")


def rows_match(first: StatisticsRow, second: StatisticsRow) -> bool:
    keys: list[Literal["state", "sum"]] = ["state", "sum"]
    for key in keys:
        value = first.get(key)
        other = second.get(key)
        if value is None or other is None:
            if value != other:
                return False
        elif not isclose(value, other, rel_tol=0, abs_tol=1e-9):
            return False
    return True
