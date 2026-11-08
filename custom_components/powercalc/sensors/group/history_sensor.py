"""Custom group with a journalled historical baseline and live continuation."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging
from typing import cast

from homeassistant.components.recorder import history
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import EVENT_RECORDER_5MIN_STATISTICS_GENERATED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import EventStateChangedData, async_track_state_change_event
from homeassistant.util import dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

from custom_components.powercalc.const import DOMAIN

from .custom import GroupedEnergySensor, GroupedSensor
from .history import HistoryImportError
from .history_import import GroupHistoryImport, ImportPhase
from .history_live import apply_source_state

_LOGGER = logging.getLogger(__name__)


class HistoryGroupedEnergySensor(GroupedEnergySensor):
    """Publish no energy value until recorder and the live checkpoint agree."""

    async def async_added_to_hass(self) -> None:
        # Unknown preserves diagnostic attributes; unavailable would hide progress.
        self._attr_native_value = None
        self._attr_state_class = None
        self._history_ready = False
        self._history_task: asyncio.Task[None] | None = None
        self._live_task: asyncio.Task[None] | None = None
        self._buffer: list[State] = []
        self._member_listener: CALLBACK_TYPE | None = None
        self._statistics_ready = asyncio.Event()
        self._import: GroupHistoryImport
        # This mode restores the total and member references from one atomic store.
        # GroupedEnergySensor's separate RestoreSensor/PreviousStateStore would mix
        # checkpoints after a crash, so intentionally use the common group lifecycle.
        await GroupedSensor.async_added_to_hass(self)

    async def on_start(self, _: HomeAssistant) -> None:
        self.async_on_remove(self.hass.bus.async_listen(EVENT_RECORDER_5MIN_STATISTICS_GENERATED, self._on_statistics))
        # Entity attributes are initialized after async_at_start invokes on_start.
        # In particular a restored journal may otherwise finish eagerly on reload.
        self._history_task = self.hass.async_create_background_task(
            self._start_history(), "powercalc group history", eager_start=False
        )
        self.async_on_remove(self._cancel_tasks)

    @callback
    def _cancel_tasks(self) -> None:
        if self._member_listener:
            self._member_listener()
            self._member_listener = None
        if self._history_task:
            self._history_task.cancel()
        if self._live_task:
            self._live_task.cancel()

    @callback
    def _on_statistics(self, _: Event) -> None:
        self._statistics_ready.set()

    async def _start_history(self) -> None:
        try:
            self._import = GroupHistoryImport(
                self.hass,
                str(self.unique_id),
                self.entity_id,
                str(self.native_unit_of_measurement),
                self._update_progress,
            )
            if not await self._import.recorder.recorder.async_db_ready:
                raise HistoryImportError("Recorder database is unavailable")
            await self._import.load(sorted(self._entities))
            if self._import.journal.phase == ImportPhase.LIVE and not self._import.journal.live_started:
                # A crash can follow a persisted capture before the first group
                # state is published. Rebuild the bridge to the current interval;
                # treating it as running live would shift usage into a later one.
                self._import.journal.phase = ImportPhase.SHORT_TERM
            if self._import.journal.phase != ImportPhase.LIVE:
                if set(self._import.journal.sources) != self._entities:
                    raise HistoryImportError(
                        "Group members changed during an unfinished import; restore the original members"
                    )
                self._update_progress()
                await self._import.import_hourly()
                await self._prepare_transition()
            else:
                await self._restore_live()
            self._attr_state_class = SensorStateClass.TOTAL
            self._publish_total()
            if not self._import.journal.live_started:
                await self._import.recorder.synchronize()
                self._import.journal.live_started = True
                await self._import.save()
            # Keep callbacks buffering until the publication marker is durable.
            # No live writer may race this final startup checkpoint.
            self._history_ready = True
            ir.async_delete_issue(self.hass, DOMAIN, f"group_history_{self.unique_id}")
            if missing := self._import.journal.aggregation.missing_intervals:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"group_history_partial_{self.unique_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="group_history_partial",
                    translation_placeholders={"entity": self.entity_id, "intervals": str(missing)},
                )
            self._schedule_live_updates()
        except (HomeAssistantError, ValueError, TimeoutError, SQLAlchemyError) as error:
            self._report_error(error)

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
                await self._import.read_references(self._import.journal.sources, boundary)
            except HistoryImportError:
                try:
                    async with asyncio.timeout_at(deadline):
                        await self._statistics_ready.wait()
                except TimeoutError as error:
                    raise HistoryImportError("Timed out waiting for complete short-term source statistics") from error
                continue
            await self._import.import_short_term(boundary)
            await self._import.recorder.verify_live_reference(boundary, self._import.journal.aggregation.total)
            if dt_util.utcnow() >= boundary + timedelta(minutes=5):
                continue
            await self._capture_live(boundary)
            if dt_util.utcnow() >= boundary + timedelta(minutes=5):
                # The replay/storage operation crossed another completed interval.
                # Keep the target unknown and bridge that interval from recorder
                # before rebuilding the live references. No live state was published.
                self._import.journal.phase = ImportPhase.SHORT_TERM
                await self._import.save()
                continue
            return

    def _listen_to_members(self) -> None:
        if self._member_listener:
            self._member_listener()
        self._member_listener = async_track_state_change_event(self.hass, self._entities, self.on_state_change)

    async def _capture_live(self, boundary: datetime) -> None:
        journal = self._import.journal
        journal.live_total = journal.aggregation.total
        journal.members = {}
        self._buffer.clear()
        for source, reference in journal.aggregation.previous.items():
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
            }
        self._listen_to_members()
        captured_states = [state for source in self._entities if (state := self.hass.states.get(source)) is not None]
        captured = dt_util.utcnow()
        await self._replay_history(boundary, captured)
        # History excludes its end timestamp. Retain the snapshot taken before
        # awaiting I/O, including a reading exactly at that timestamp. Newer
        # readings must be applied in buffer order, especially across resets.
        for state in captured_states:
            apply_source_state(journal, state)
        journal.live_since = captured.timestamp()
        journal.transition = boundary.timestamp()
        journal.phase = ImportPhase.LIVE
        await self._import.save()

    async def _restore_live(self) -> None:
        journal = self._import.journal
        journal.members = {source: member for source, member in journal.members.items() if source in self._entities}
        self._listen_to_members()
        captured_states = [state for source in self._entities if (state := self.hass.states.get(source)) is not None]
        captured = dt_util.utcnow()
        await self._replay_history(datetime.fromtimestamp(journal.live_since, UTC), captured)
        # Members added after completion establish a fresh baseline. They never
        # contribute historical usage, and never restart the historical import.
        for state in captured_states:
            apply_source_state(journal, state)
        journal.live_since = captured.timestamp()
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
                    apply_source_state(journal, state)
            cursor = batch_end

    @callback
    def on_state_change(self, event: Event[EventStateChangedData]) -> None:
        if (state := event.data.get("new_state")) is None:
            return
        self._buffer.append(state)
        self._schedule_live_updates()

    @callback
    def _schedule_live_updates(self) -> None:
        # Startup and state callbacks share one worker. A second writer could
        # save an older snapshot after a newer checkpoint has already committed.
        if self._history_ready and self._buffer and (self._live_task is None or self._live_task.done()):
            self._live_task = self.hass.async_create_task(self._process_buffer(), eager_start=False)

    async def _process_buffer(self) -> None:
        try:
            while self._buffer:
                pending, self._buffer = self._buffer, []
                for state in pending:
                    apply_source_state(self._import.journal, state)
                self._import.journal.live_since = max(state.last_updated.timestamp() for state in pending)
                await self._import.save()
                self._publish_total()
        except (HomeAssistantError, ValueError) as error:
            self._report_error(error)

    def _publish_total(self) -> None:
        self._attr_available = True
        # Keep the native counter at the precision of the imported sum. Rounding
        # here would introduce a fabricated difference at the recorder handoff;
        # suggested_display_precision still controls the usual UI presentation.
        self._native_value_exact = Decimal(str(self._import.journal.live_total))
        self._attr_native_value = self._native_value_exact
        self._update_progress()

    def _update_progress(self) -> None:
        journal = self._import.journal
        self._attr_extra_state_attributes = {
            **(self._attr_extra_state_attributes or {}),
            "history_import_status": journal.phase.value,
            "history_import_rows": journal.imported_rows,
            "history_incomplete_intervals": journal.aggregation.missing_intervals,
        }
        self.async_write_ha_state()

    def _report_error(self, error: Exception) -> None:
        self._history_ready = False
        if self._member_listener:
            self._member_listener()
            self._member_listener = None
        self._buffer.clear()
        self._attr_native_value = None
        self._attr_state_class = None
        self._attr_extra_state_attributes = {
            **(self._attr_extra_state_attributes or {}),
            "history_import_status": "failed",
        }
        self.async_write_ha_state()
        _LOGGER.error("History import for %s failed: %s", self.entity_id, error)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"group_history_{self.unique_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="group_history_failed",
            translation_placeholders={"entity": self.entity_id, "error": str(error)},
        )

    async def async_reset(self) -> None:
        raise HomeAssistantError("Reset is not supported for a group with imported history")

    async def async_calibrate(self, value: str) -> None:
        raise HomeAssistantError("Calibration is not supported for a group with imported history")
