"""One-time history initialization of the original group energy sensor."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging
from math import isclose
from typing import Any, cast

from homeassistant.components.recorder import history
from homeassistant.components.recorder.statistics import StatisticsRow
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_RECORDER_5MIN_STATISTICS_GENERATED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir, start
from homeassistant.util import dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

from custom_components.powercalc.const import (
    CONF_FORCE_CALCULATE_GROUP_ENERGY,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_TYPE,
    DOMAIN,
    UNAVAILABLE_STATES,
    GroupType,
)
from custom_components.powercalc.sensors.energy import VirtualEnergySensor
from custom_components.powercalc.unit import convert_to_decimal

from .custom import GroupedEnergySensor
from .history import HistoryImportError
from .history_import import GroupHistoryImport, ImportJournal, ImportPhase
from .history_seed import GroupHistorySeed
from .history_transition import apply_source_state, read_energy_value

_LOGGER = logging.getLogger(__name__)


class GroupHistoryInitializer:
    """Seed an ordinary sensor once, then leave all live processing to that sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        sensor: GroupedEnergySensor | VirtualEnergySensor,
        config: dict[str, Any],
        entry: ConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self.sensor = sensor
        self._sensor_config = config
        self._history_entry = entry
        self._group_type = GroupType(config.get(CONF_GROUP_TYPE, GroupType.CUSTOM))
        self._history_task: asyncio.Task[None] | None = None
        self._statistics_listener: CALLBACK_TYPE | None = None
        self._statistics_ready = asyncio.Event()
        self._source_weights: dict[str, int] = {}
        self._entities: set[str] = set()
        self._runtime_members: dict[str, State] = {}
        self._source_snapshot: dict[str, State] = {}
        self._runtime_started = False
        self._import: GroupHistoryImport

    @property
    def entity_id(self) -> str:
        return self.sensor.entity_id

    async def async_added_to_hass(self) -> None:
        self._state_class = self.sensor.state_class
        self.sensor.async_set_history_progress({"history_import_status": "hourly"}, importing=True)
        self.sensor.async_on_remove(start.async_at_start(self.hass, self.on_start))
        self.sensor.async_on_remove(self._cancel_tasks)

    async def on_start(self, _: HomeAssistant) -> None:
        self._statistics_listener = self.hass.bus.async_listen(
            EVENT_RECORDER_5MIN_STATISTICS_GENERATED, self._on_statistics
        )
        self._history_task = self.hass.async_create_background_task(
            self._start_history(), "powercalc group history", eager_start=False
        )

    @callback
    def _cancel_tasks(self) -> None:
        if self._statistics_listener:
            self._statistics_listener()
            self._statistics_listener = None
        if self._history_task and self._history_task is not asyncio.current_task():
            self._history_task.cancel()

    @callback
    def _on_statistics(self, _: Event) -> None:
        self._statistics_ready.set()

    async def _resolve_sources(self) -> None:
        from .history_sources import resolve_history_source_weights

        self._source_weights = await resolve_history_source_weights(self.hass, self._sensor_config, self._history_entry)
        if isinstance(self.sensor, GroupedEnergySensor):
            # Import the counters the original runtime actually listens to. A
            # directly selected group can have its own origin and update delay.
            # Expanding it for history while listening to its output afterwards
            # would count its delayed updates twice at the handoff.
            seen: set[str] = set()
            for entity_id in sorted(self.sensor.entities):
                underlying = await resolve_history_source_weights(
                    self.hass, {CONF_GROUP_TYPE: GroupType.CUSTOM, CONF_GROUP_ENERGY_ENTITIES: [entity_id]}
                )
                if seen.intersection(underlying):
                    raise HistoryImportError("Group counters overlap; select disjoint sources or configured subgroups")
                seen.update(underlying)
            if not seen:
                raise HistoryImportError("The original group has no cumulative energy sources")
            self._source_weights = dict.fromkeys(self.sensor.entities, 1)
        if self.entity_id in self._source_weights:
            raise HistoryImportError("The history group cannot use its own energy sensor as a source")
        self._entities = set(self._source_weights)

    def _capture_runtime_members(self) -> dict[str, State]:
        if not isinstance(self.sensor, GroupedEnergySensor):
            return {}
        states: dict[str, State] = {}
        journal = self._import.journal
        for entity_id in self.sensor.entities:
            if member := journal.members.get(entity_id):
                snapshot = self._source_snapshot.get(entity_id)
                if (
                    snapshot is not None
                    and snapshot.state not in UNAVAILABLE_STATES
                    and isclose(read_energy_value(snapshot, journal.unit), member["value"], rel_tol=0, abs_tol=1e-9)
                ):
                    states[entity_id] = snapshot
                    continue
                states[entity_id] = State(
                    entity_id,
                    str(member["value"]),
                    {"unit_of_measurement": journal.unit, "state_class": member["state_class"]},
                    last_updated=datetime.fromtimestamp(member["updated"], UTC),
                )
        return states

    async def _start_history(self) -> None:
        try:
            unique_id = str(self.sensor.unique_id)
            self._validate_initial_config()
            if isinstance(self.sensor, VirtualEnergySensor):
                self.sensor.prepare_group_history_unit()
            else:
                await self.sensor.init_domain_group()
            await self._resolve_sources()
            self._import = GroupHistoryImport(
                self.hass, unique_id, self.entity_id, str(self.sensor.native_unit_of_measurement), self._update_progress
            )
            if not await self._import.recorder.recorder.async_db_ready:
                raise HistoryImportError("Recorder database is unavailable")
            await self._import.load(sorted(self._entities), self._source_weights)
            journal = self._import.journal
            if journal.phase == ImportPhase.LIVE and not journal.live_started:
                journal.phase = ImportPhase.SHORT_TERM
            if journal.phase != ImportPhase.LIVE:
                frozen_weights = {source: journal.source_weights.get(source, 1) for source in journal.sources}
                if frozen_weights != self._source_weights:
                    raise HistoryImportError(
                        "Group members changed during an unfinished import; restore the original members and signs"
                    )
                self._update_progress()
                await self._import.import_hourly()
                await self._prepare_transition()
            else:
                # One-time migration of sensors which used the earlier separate
                # live implementation. Subsequent reloads use the original restore.
                await self._import.align_hourly_states()
                await self._migrate_legacy_checkpoint()
            seed = self._create_runtime_seed(journal)
            await self._start_original_runtime(seed)
            await self._import.recorder.synchronize()
            journal.live_started = True
            journal.runtime_handoff_complete = True
            await self._import.save()
            self._report_completion(journal)
        except (HomeAssistantError, ValueError, TimeoutError, SQLAlchemyError) as error:
            self._report_error(error)
            if self._runtime_started:
                # A failed handoff must not leave the original listeners running
                # while the durable journal still describes an unfinished import.
                await self.sensor.async_remove()
        finally:
            if self._statistics_listener:
                self._statistics_listener()
                self._statistics_listener = None

    def _report_completion(self, journal: ImportJournal) -> None:
        unique_id = str(self.sensor.unique_id)
        self._show_progress(journal)
        ir.async_delete_issue(self.hass, DOMAIN, f"group_history_{unique_id}")
        if missing := journal.aggregation.missing_intervals:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"group_history_partial_{unique_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="group_history_partial",
                translation_placeholders={"entity": self.entity_id, "intervals": str(missing)},
            )

    def _validate_initial_config(self) -> None:
        if self._sensor_config.get(CONF_FORCE_CALCULATE_GROUP_ENERGY) or (
            self._group_type == GroupType.CUSTOM and self._sensor_config.get(CONF_GROUP_ENERGY_START_AT_ZERO, True)
        ):
            raise HistoryImportError("History requires a new group without start-at-zero or force-calculation")

    def _create_runtime_seed(self, journal: ImportJournal) -> GroupHistorySeed:
        total = Decimal(str(journal.live_total)) + Decimal(str(journal.native_offset))
        if isinstance(self.sensor, GroupedEnergySensor):
            counter_total = Decimal(0)
            for state in self._runtime_members.values():
                counter_total += convert_to_decimal(
                    state.state, state.attributes.get("unit_of_measurement"), journal.unit
                ) or Decimal(0)
            # Preserve the original source decimals when the only difference is
            # recorder floating-point arithmetic. Otherwise a halfway value can
            # round differently even though both sensors use the same code.
            if isclose(float(total), float(counter_total), rel_tol=0, abs_tol=1e-9):
                total = counter_total
        return GroupHistorySeed(total, datetime.fromtimestamp(journal.live_since, UTC), self._runtime_members)

    async def _start_original_runtime(self, seed: GroupHistorySeed | None = None) -> None:
        if self._history_entry is not None and self._group_type != GroupType.CUSTOM:
            # Older history flows inserted this hidden option for every domain
            # and subtract group. Their original runtime uses its normal default.
            self._sensor_config.pop(CONF_GROUP_ENERGY_START_AT_ZERO, None)
        self.sensor._attr_state_class = self._state_class  # noqa: SLF001
        self.sensor.group_history = None
        self._runtime_started = True
        await self.sensor.async_start_group_runtime(seed)

    async def _prepare_transition(self) -> None:
        deadline = self.hass.loop.time() + 360
        while True:
            self._statistics_ready.clear()
            now = dt_util.utcnow()
            boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
            # Wait until the source recorder has compiled the just-finished interval.
            # Its event is emitted before commit, so follow it with a queue barrier.
            await self._import.recorder.synchronize()
            try:
                references = await self._import.read_references(self._import.journal.sources, boundary)
            except HistoryImportError:
                try:
                    async with asyncio.timeout_at(deadline):
                        await self._statistics_ready.wait()
                except TimeoutError as error:
                    raise HistoryImportError("Timed out waiting for complete short-term source statistics") from error
                continue
            native_offset = self._import.journal.native_offset
            if native_offset is None:
                native_offset = await self._initialize_native_offset(references, boundary)
            await self._import.align_hourly_states()
            await self._import.import_short_term(boundary)
            await self._import.recorder.verify_live_reference(
                boundary, self._import.journal.aggregation.total, native_offset
            )
            if dt_util.utcnow() >= boundary + timedelta(minutes=5):
                continue
            await self._capture_handoff(boundary)
            if dt_util.utcnow() >= boundary + timedelta(minutes=5):
                # The replay/storage operation crossed another completed interval.
                # Keep the target unknown and bridge that interval from recorder
                # before rebuilding the live references. No live state was published.
                self._import.journal.phase = ImportPhase.SHORT_TERM
                await self._import.save()
                continue
            return

    async def _capture_handoff(self, boundary: datetime) -> None:
        journal = self._import.journal
        captured = await self._capture_members(journal.aggregation.previous, boundary, journal.aggregation.total)
        self._runtime_members = self._capture_runtime_members()
        journal.live_since = captured.timestamp()
        journal.transition = boundary.timestamp()
        journal.phase = ImportPhase.LIVE
        await self._import.save()

    async def _initialize_native_offset(self, references: dict[str, StatisticsRow], boundary: datetime) -> float:
        """Match the initial live counter to its sources without changing consumption."""
        journal = self._import.journal
        # The bridge is validated in full before publication. Its cumulative sum
        # difference lets us anchor the native counter before writing target rows.
        total = journal.aggregation.total
        for source, reference in references.items():
            reference_sum = cast(float, reference["sum"])
            previous_sum = cast(float, journal.aggregation.previous[source]["sum"])
            total += (reference_sum - previous_sum) * self._source_weights[source]
        await self._capture_members(references, boundary, total)
        if isinstance(self.sensor, VirtualEnergySensor):
            journal.native_offset = 0.0
        else:
            snapshots = self._capture_runtime_members()
            native_total = sum(
                read_energy_value(state, journal.unit)
                for state in snapshots.values()
                if state.state not in UNAVAILABLE_STATES
            )
            journal.native_offset = native_total - journal.live_total
        await self._import.save()
        return journal.native_offset

    async def _capture_members(
        self, references: dict[str, StatisticsRow], boundary: datetime, total: float
    ) -> datetime:
        journal = self._import.journal
        journal.live_total = total
        journal.members = {}
        for source, reference in references.items():
            state = self.hass.states.get(source)
            if state is None or state.attributes.get("state_class") not in [
                SensorStateClass.TOTAL,
                SensorStateClass.TOTAL_INCREASING,
            ]:
                raise HistoryImportError(f"No cumulative source entity for {source}")
            last_reset = reference.get("last_reset")
            journal.members[source] = {
                "value": reference["state"],
                "state_class": state.attributes["state_class"],
                "last_reset": datetime.fromtimestamp(last_reset, UTC).isoformat() if last_reset is not None else None,
                "updated": boundary.timestamp() - 0.000001,
                "weight": self._source_weights[source],
            }
        captured_states = [state for source in self._entities if (state := self.hass.states.get(source)) is not None]
        self._source_snapshot = {state.entity_id: state for state in captured_states}
        captured = dt_util.utcnow()
        await self._replay_history(boundary, captured)
        # History excludes its end timestamp. Retain the snapshot taken before
        # awaiting I/O, including a reading exactly at that timestamp. Newer
        # readings must be applied in buffer order, especially across resets.
        for state in captured_states:
            apply_source_state(journal, state, self._source_weights[state.entity_id])
        return captured

    async def _migrate_legacy_checkpoint(self) -> None:
        journal = self._import.journal
        journal.members = {source: member for source, member in journal.members.items() if source in self._entities}
        captured_states = [state for source in self._entities if (state := self.hass.states.get(source)) is not None]
        self._source_snapshot = {state.entity_id: state for state in captured_states}
        captured = dt_util.utcnow()
        await self._replay_history(datetime.fromtimestamp(journal.live_since, UTC), captured)
        # Members added after completion establish a fresh baseline. They never
        # contribute historical usage, and never restart the historical import.
        for state in captured_states:
            apply_source_state(journal, state, self._source_weights[state.entity_id])
        journal.live_since = captured.timestamp()
        self._runtime_members = self._capture_runtime_members()
        await self._import.save()

    async def _replay_history(self, start: datetime, end: datetime) -> None:
        journal = self._import.journal
        if not journal.members:
            return
        await self._import.recorder.synchronize()
        cursor = start
        while cursor < end:
            batch_end = min(cursor + timedelta(hours=1), end)
            states = cast(
                dict[str, list[State]],
                await self._import.recorder.execute(
                    history.get_significant_states,
                    self.hass,
                    cursor,
                    batch_end,
                    list(journal.members),
                    include_start_time_state=True,
                    significant_changes_only=False,
                ),
            )
            for source in journal.members:
                if not states.get(source):
                    raise HistoryImportError(f"Recorder state history is missing for the live transition of {source}")
                for state in states[source]:
                    apply_source_state(journal, state, journal.members[source].get("weight", 1))
            cursor = batch_end

    def _show_progress(self, journal: ImportJournal) -> None:
        self.sensor.async_set_history_progress(
            {
                "history_import_status": journal.phase.value,
                "history_import_rows": journal.imported_rows,
                "history_incomplete_intervals": journal.aggregation.missing_intervals,
            }
        )

    def _update_progress(self) -> None:
        self._show_progress(self._import.journal)

    def _report_error(self, error: Exception) -> None:
        self.sensor.async_set_history_progress({"history_import_status": "failed"}, importing=True)
        _LOGGER.error("History import for %s failed: %s", self.entity_id, error)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"group_history_{self.sensor.unique_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="group_history_failed",
            translation_placeholders={"entity": self.entity_id, "error": str(error)},
        )
