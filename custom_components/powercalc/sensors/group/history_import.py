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
    pending_previous: list[StatisticsRow] = field(default_factory=list)
    pending_hourly: list[StatisticsRow] = field(default_factory=list)
    pending_period: Period = "hour"
    imported_rows: int = 0
    live_total: float = 0
    live_since: float = 0
    transition: float | None = None
    live_started: bool = False
    runtime_handoff_complete: bool = False
    native_offset: float | None = None
    short_term_seeded: bool = False
    state_alignment_cursor: float = 0
    state_alignment_complete: bool = False
    source_weights: dict[str, int] = field(default_factory=dict)
    members: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportJournal:
        values = dict(data)
        values["phase"] = ImportPhase(data["phase"])
        values["aggregation"] = AggregationState(**data["aggregation"])
        # Journals from the first implementation did not distinguish capture from
        # publication. Preserve completed imports when loading that older format.
        values.setdefault("live_started", data["phase"] == ImportPhase.LIVE)
        # Preserve the counter origin and already imported references of older
        # journals. Rebasing a running counter would create artificial usage.
        values.setdefault("native_offset", 0.0)
        values.setdefault("short_term_seeded", data["phase"] != ImportPhase.HOURLY)
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

    async def load(self, sources: list[str], source_weights: dict[str, int] | None = None) -> None:
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
            source_weights=dict(source_weights or {}),
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
            if journal.pending_previous:
                await self.recorder.import_batch(
                    journal.pending, journal.pending_period, previous_rows=journal.pending_previous
                )
            else:
                await self.recorder.import_batch(
                    journal.pending, journal.pending_period, first_batch=journal.imported_rows == 0
                )
                journal.imported_rows += len(journal.pending)
            journal.pending = []
            journal.pending_previous = []
            await self.save()
            if self.progress:
                self.progress()
        if journal.pending_hourly:
            await self.recorder.import_batch(journal.pending_hourly, "hour")
            journal.imported_rows += len(journal.pending_hourly)
            journal.pending_hourly = []
            await self.save()

    async def stage_batch(
        self, rows: list[StatisticsRow], period: Period, previous_rows: list[StatisticsRow] | None = None
    ) -> None:
        self.journal.pending = rows
        self.journal.pending_previous = previous_rows or []
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
                aggregate_statistics, journal.sources, source_rows, journal.aggregation, HOUR, journal.source_weights
            )
            journal.cursor = end.timestamp()
            if not journal.imported_rows and aggregated:
                journal.state_alignment_cursor = aggregated[0]["start"].timestamp()
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
        await self.save()

    async def align_hourly_states(self) -> None:
        """Apply the live counter origin to owned history without changing consumption."""
        journal = self.journal
        await self.flush_pending()
        if journal.state_alignment_complete:
            return
        if journal.native_offset is None:
            raise HistoryImportError("The initial group counter reference has not been established")
        if journal.native_offset == 0:
            journal.state_alignment_cursor = journal.boundary
        boundary = datetime.fromtimestamp(journal.boundary, UTC)
        while journal.state_alignment_cursor < journal.boundary:
            start = datetime.fromtimestamp(journal.state_alignment_cursor, UTC)
            end = min(start + BATCH_DURATION, boundary)
            existing = (await self.recorder.read([journal.target], start, end)).get(journal.target, [])
            updated = align_counter_states(existing, journal.native_offset)
            journal.state_alignment_cursor = end.timestamp()
            # Freeze both sides before permitting any replacement. On recovery,
            # recorder accepts the exact old values or the exact committed values.
            await self.stage_batch(updated, "hour", existing if updated else None)
        journal.state_alignment_complete = True
        await self.save()

    async def import_short_term(self, boundary: datetime) -> None:
        journal = self.journal
        await self.flush_pending()
        if journal.phase != ImportPhase.SHORT_TERM:
            return
        if journal.native_offset is None:
            raise HistoryImportError("The initial group counter reference has not been established")
        if not journal.short_term_seeded:
            journal.short_term_seeded = True
            await self.stage_batch(
                [
                    {
                        "start": journal.boundary - 300,
                        "sum": journal.aggregation.total,
                        "state": journal.aggregation.total + journal.native_offset,
                    }
                ],
                "5minute",
            )
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
                aggregate_statistics,
                journal.sources,
                source_rows,
                journal.aggregation,
                FIVE_MINUTES,
                journal.source_weights,
                journal.native_offset,
            )
            journal.cursor = end.timestamp()
            # Journal both tables together: a crash after the short-term commit
            # must not skip the hourly summary when this cursor is resumed.
            if end.minute == 0:
                journal.pending_hourly = [
                    {
                        "start": (end - HOUR).timestamp(),
                        "sum": journal.aggregation.total,
                        "state": journal.aggregation.total + journal.native_offset,
                    }
                ]
            await self.stage_batch(
                [
                    {"start": row["start"].timestamp(), "state": row.get("state"), "sum": row.get("sum")}
                    for row in aggregated
                ],
                "5minute",
            )


def align_counter_states(rows: list[StatisticsRow], offset: float) -> list[StatisticsRow]:
    updated: list[StatisticsRow] = []
    changed = False
    for row in rows:
        state = row.get("state")
        total = row.get("sum")
        if state is None or total is None or not isfinite(state) or not isfinite(total):
            raise HistoryImportError("Cannot align incomplete group counter statistics")
        expected = total + offset
        if not isclose(state, expected, rel_tol=0, abs_tol=1e-9):
            if not isclose(state, total, rel_tol=0, abs_tol=1e-9):
                raise HistoryImportError(f"Conflicting group counter state at {row['start']}; nothing was overwritten")
            changed = True
        updated.append({"start": row["start"], "state": expected, "sum": total})
    return updated if changed else []


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
