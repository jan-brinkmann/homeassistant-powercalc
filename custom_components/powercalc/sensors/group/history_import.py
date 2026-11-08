"""Persistent journal for a one-time group statistics import."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isclose, isfinite
from typing import Any

from homeassistant.components.recorder.statistics import StatisticsRow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .history import AggregationState, HistoryImportError, aggregate_statistics
from .history_recorder import HistoryRecorder, Period

HOUR = timedelta(hours=1)
FIVE_MINUTES = timedelta(minutes=5)
BATCH_DURATION = timedelta(days=31)


class ImportPhase(StrEnum):
    HOURLY = "hourly"
    SHORT_TERM = "short_term"
    LIVE = "completed"


@dataclass
class ImportJournal:
    target: str
    unit: str
    sources: list[str]
    boundary: float
    cursor: float = 0
    phase: ImportPhase = ImportPhase.HOURLY
    aggregation: AggregationState = field(default_factory=AggregationState)
    pending: list[StatisticsRow] = field(default_factory=list)
    pending_hourly: list[StatisticsRow] = field(default_factory=list)
    pending_period: Period = "hour"
    imported_rows: int = 0
    live_total: float = 0
    live_since: float = 0
    transition: float | None = None
    live_started: bool = False
    members: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportJournal:
        values = dict(data)
        values["phase"] = ImportPhase(data["phase"])
        values["aggregation"] = AggregationState(**data["aggregation"])
        # Journals from the first implementation did not distinguish capture from
        # publication. Preserve completed imports when loading that older format.
        values.setdefault("live_started", data["phase"] == ImportPhase.LIVE)
        return cls(**values)


class GroupHistoryImport:
    """Keep source membership and batches immutable across interrupted imports."""

    def __init__(
        self, hass: HomeAssistant, unique_id: str, target: str, unit: str, progress: Callable[[], None] | None = None
    ) -> None:
        self.hass = hass
        self.store = Store[dict[str, Any]](
            hass, 1, f"powercalc_group_history.{unique_id}", atomic_writes=True, serialize_in_event_loop=False
        )
        self.recorder = HistoryRecorder(hass, target, unit)
        self.journal: ImportJournal
        self.progress = progress

    async def save(self) -> None:
        payload = asdict(self.journal)
        await self.store.async_save(payload)
        # Store logs some write failures without raising. Read through a fresh
        # store so pending in-memory data cannot masquerade as a durable journal.
        verified = await Store[dict[str, Any]](self.hass, 1, self.store.key).async_load()
        if verified != payload:
            raise HistoryImportError("The group history checkpoint could not be saved; recorder import was stopped")

    async def load(self, sources: list[str]) -> None:
        stored = await self.store.async_load()
        if stored is not None:
            try:
                self.journal = ImportJournal.from_dict(stored)
            except (KeyError, TypeError, ValueError) as error:
                raise HistoryImportError(
                    "The stored group history checkpoint is invalid; it was not overwritten"
                ) from error
            if self.journal.target != self.recorder.target or self.journal.unit != self.recorder.unit:
                raise HistoryImportError("The target entity ID or energy unit changed during the history import")
            return
        await self.recorder.require_empty_target()
        await self.recorder.validate_sources(sources)
        boundary = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        # Require a recent reference before writing any historical batch. The current
        # source state can be unavailable; it is the recorder reference that matters.
        references = await self.read_references(sources, boundary)
        hourly_rows = await self.recorder.read(sources, boundary - HOUR, boundary)
        latest_hourly: dict[str, StatisticsRow] = {}
        for source in sources:
            samples = hourly_rows.get(source, [])
            if len(samples) == 1:
                latest_hourly[source] = samples[0]
        # At an hour boundary the five-minute reference can be ready before the
        # hourly rollup. Fail before persisting a cursor or writing any target row,
        # so a reload can retry once recorder has finished the hourly compilation.
        validate_hourly_references(latest_hourly, references, boundary.timestamp())
        self.journal = ImportJournal(
            target=self.recorder.target,
            unit=self.recorder.unit,
            sources=sources,
            boundary=boundary.timestamp(),
        )
        await self.save()

    async def read_references(self, sources: list[str], boundary: datetime) -> dict[str, StatisticsRow]:
        rows = await self.recorder.read(sources, boundary - FIVE_MINUTES, boundary, "5minute")
        references: dict[str, StatisticsRow] = {}
        for source in sources:
            samples = rows.get(source, [])
            if len(samples) != 1 or not is_complete_reference(samples[0], (boundary - FIVE_MINUTES).timestamp()):
                raise HistoryImportError(
                    f"No reliable recent recorder reference for {source} at {boundary.isoformat()}"
                )
            references[source] = samples[0]
        return references

    async def flush_pending(self) -> None:
        journal = self.journal
        if journal.pending:
            await self.recorder.import_batch(
                journal.pending, journal.pending_period, first_batch=journal.imported_rows == 0
            )
            journal.imported_rows += len(journal.pending)
            journal.pending = []
            await self.save()
            if self.progress:
                self.progress()
        if journal.pending_hourly:
            await self.recorder.import_batch(journal.pending_hourly, "hour")
            journal.imported_rows += len(journal.pending_hourly)
            journal.pending_hourly = []
            await self.save()

    async def stage_batch(self, rows: list[StatisticsRow], period: Period) -> None:
        self.journal.pending = rows
        self.journal.pending_period = period
        # Persist the exact payload BEFORE enqueueing. A retry checks existing rows
        # against this payload instead of re-reading potentially corrected sources.
        await self.save()
        await self.flush_pending()

    async def import_hourly(self) -> None:
        journal = self.journal
        await self.flush_pending()
        boundary = datetime.fromtimestamp(journal.boundary, UTC)
        while journal.phase == ImportPhase.HOURLY and journal.cursor < journal.boundary:
            start = datetime.fromtimestamp(journal.cursor, UTC)
            end = min(start + BATCH_DURATION, boundary)
            source_rows = await self.recorder.read(journal.sources, start, end)
            aggregated = await self.hass.async_add_executor_job(
                aggregate_statistics, journal.sources, source_rows, journal.aggregation
            )
            journal.cursor = end.timestamp()
            await self.stage_batch(
                [
                    {"start": row["start"].timestamp(), "state": row.get("state"), "sum": row.get("sum")}
                    for row in aggregated
                ],
                "hour",
            )
        if journal.phase != ImportPhase.HOURLY:
            return
        if journal.aggregation.known_intervals == 0:
            raise HistoryImportError("No source has two adjacent hourly consumption statistics")
        references = await self.read_references(journal.sources, boundary)
        validate_hourly_references(journal.aggregation.previous, references, journal.boundary)
        journal.aggregation.previous = references
        journal.aggregation.last_start = journal.boundary - 300
        journal.phase = ImportPhase.SHORT_TERM
        journal.cursor = journal.boundary
        await self.stage_batch(
            [{"start": journal.boundary - 300, "sum": journal.aggregation.total, "state": journal.aggregation.total}],
            "5minute",
        )

    async def import_short_term(self, boundary: datetime) -> None:
        journal = self.journal
        await self.flush_pending()
        if journal.phase != ImportPhase.SHORT_TERM:
            return
        while journal.cursor < boundary.timestamp():
            start = datetime.fromtimestamp(journal.cursor, UTC)
            end = min(start.replace(minute=0, second=0, microsecond=0) + HOUR, boundary)
            source_rows = await self.recorder.read(journal.sources, start, end, "5minute")
            # Missing short-term data makes the handoff ambiguous. Do not report a
            # successful import if recorder retention or an outage removed the bridge.
            expected = int((end - start) / FIVE_MINUTES)
            for source in journal.sources:
                samples = source_rows.get(source, [])
                if len(samples) != expected or any(
                    not is_complete_reference(sample, (start + index * FIVE_MINUTES).timestamp())
                    for index, sample in enumerate(samples)
                ):
                    raise HistoryImportError(
                        f"Short-term statistics are incomplete during the live transition: {source}"
                    )
            aggregated = await self.hass.async_add_executor_job(
                aggregate_statistics, journal.sources, source_rows, journal.aggregation, FIVE_MINUTES
            )
            journal.cursor = end.timestamp()
            # Journal both tables together: a crash after the short-term commit
            # must not skip the hourly summary when this cursor is resumed.
            if end.minute == 0:
                journal.pending_hourly = [
                    {
                        "start": (end - HOUR).timestamp(),
                        "sum": journal.aggregation.total,
                        "state": journal.aggregation.total,
                    }
                ]
            await self.stage_batch(
                [
                    {"start": row["start"].timestamp(), "state": row.get("state"), "sum": row.get("sum")}
                    for row in aggregated
                ],
                "5minute",
            )


def is_complete_reference(row: StatisticsRow, timestamp: float) -> bool:
    total = row.get("sum")
    state = row.get("state")
    return row["start"] == timestamp and total is not None and state is not None and isfinite(total) and isfinite(state)


def validate_hourly_references(
    previous_rows: dict[str, StatisticsRow], references: dict[str, StatisticsRow], boundary: float
) -> None:
    for source, reference in references.items():
        previous = previous_rows.get(source)
        previous_sum = previous.get("sum") if previous else None
        reference_sum = reference.get("sum")
        if (
            previous is None
            or previous["start"] != boundary - HOUR.total_seconds()
            or previous_sum is None
            or reference_sum is None
            or not isclose(previous_sum, reference_sum, rel_tol=0, abs_tol=1e-9)
        ):
            raise HistoryImportError(f"Hourly and short-term reference disagree for {source}")
