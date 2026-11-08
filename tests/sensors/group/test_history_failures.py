"""Recorder failures must stop an import without falsely acknowledging completion."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import StatisticsRow
from homeassistant.const import EVENT_RECORDER_5MIN_STATISTICS_GENERATED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.powercalc.const import (
    CONF_ENERGY_SENSOR_UNIT_PREFIX,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_TYPE,
    DOMAIN,
    GroupType,
    UnitPrefix,
)
from custom_components.powercalc.sensors.group.history import AggregationState, HistoryImportError
from custom_components.powercalc.sensors.group.history_import import GroupHistoryImport, ImportJournal, ImportPhase
from custom_components.powercalc.sensors.group.history_recorder import HistoryRecorder
from tests.common import create_mock_group_entry
from tests.sensors.group.test_history_recorder import mock_recorder_before_hass as mock_recorder_before_hass


@dataclass
class PendingGroup:
    entry: MockConfigEntry
    job: GroupHistoryImport
    boundary: datetime


@pytest.fixture
async def pending_group(hass: HomeAssistant, recorder_mock: Recorder, freezer: FrozenDateTimeFactory) -> PendingGroup:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_start()
    boundary = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    freezer.move_to(boundary + timedelta(minutes=1))
    hass.states.async_set(
        "sensor.source",
        "300",
        {
            "device_class": "energy",
            "state_class": "total",
            "unit_of_measurement": "kWh",
        },
    )
    await async_wait_recording_done(hass)
    freezer.move_to(boundary + timedelta(minutes=2))
    source = HistoryRecorder(hass, "sensor.source", "kWh")
    await source.import_batch([{"start": boundary.timestamp() - 300, "state": 300.0, "sum": 100.0}], "5minute")
    entry = await create_mock_group_entry(
        hass,
        "History",
        {
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.source"],
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_GROUP_ENERGY_START_AT_ZERO: False,
            CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
        },
        setup=False,
    )
    job = GroupHistoryImport(hass, f"{entry.unique_id}_energy", "sensor.history_energy", "kWh")
    job.journal = ImportJournal(
        job.recorder.target,
        "kWh",
        ["sensor.source"],
        boundary.timestamp(),
        cursor=boundary.timestamp(),
        phase=ImportPhase.SHORT_TERM,
        native_offset=0.0,
        short_term_seeded=True,
        imported_rows=1,
        aggregation=AggregationState(
            total=0.5,
            previous={
                "sensor.source": {
                    "start": boundary.timestamp() - 300,
                    "sum": 100.0,
                    "state": 300.0,
                }
            },
            known_intervals=1,
            last_start=boundary.timestamp() - 300,
        ),
    )
    await job.recorder.import_batch([{"start": boundary.timestamp() - 300, "state": 0.5, "sum": 0.5}], "5minute")
    await job.save()
    return PendingGroup(entry, job, boundary)


async def setup_pending_group(hass: HomeAssistant, pending: PendingGroup) -> None:
    assert await hass.config_entries.async_setup(pending.entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def assert_failure(hass: HomeAssistant, reason: str) -> None:
    state = hass.states.get("sensor.history_energy")
    assert state.state == "unknown"
    assert state.attributes["history_import_status"] == "failed"
    assert any(
        reason in issue.translation_placeholders.get("error", "")
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key == "group_history_failed"
    )


async def test_disabled_recorder_is_rejected(hass: HomeAssistant) -> None:
    with pytest.raises(HistoryImportError, match="Recorder must be enabled"):
        HistoryRecorder(hass, "sensor.target", "kWh")


async def test_recorder_exclusion_is_rejected(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    with (
        patch.object(recorder_mock, "entity_filter", return_value=False),
        pytest.raises(HistoryImportError, match="excluded"),
    ):
        HistoryRecorder(hass, "sensor.target", "kWh")


@pytest.mark.parametrize("failure", [False, True])
async def test_recorder_must_confirm_commit(hass: HomeAssistant, recorder_mock: Recorder, failure: bool) -> None:
    await hass.async_start()
    target = HistoryRecorder(hass, "sensor.target", "kWh")
    with (
        patch("custom_components.powercalc.sensors.group.history_recorder.import_statistics", return_value=failure),
        pytest.raises(HistoryImportError, match=r"commit|confirm"),
    ):
        await target.import_batch([{"start": 0.0, "state": 0.0, "sum": 0.0}], "hour")
    await target.require_empty_target()


async def test_foreign_metadata_and_new_rows_are_protected(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    await hass.async_start()
    target = HistoryRecorder(hass, "sensor.target", "kWh")
    await target.import_batch([], "hour")
    batch = [{"start": 0.0, "state": 0.0, "sum": 0.0}]
    await target.import_batch(batch, "hour")
    with pytest.raises(HistoryImportError, match="metadata"):
        await HistoryRecorder(hass, "sensor.target", "Wh").import_batch(batch, "hour")
    with pytest.raises(HistoryImportError, match="appeared"):
        await target.import_batch([{"start": 3600.0, "state": 1.0, "sum": 1.0}], "hour", first_batch=True)
    with pytest.raises(HistoryImportError, match="Conflicting"):
        await target.import_batch([{"start": 0.0, "state": None, "sum": None}], "hour")
    with pytest.raises(HistoryImportError, match="reference changed"):
        await target.verify_live_reference(dt_util.utcnow(), 0)


async def test_cancelled_import_can_finish_and_be_replayed(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    await hass.async_start()
    target = HistoryRecorder(hass, "sensor.target", "kWh")
    batch = [{"start": 0.0, "state": 0.0, "sum": 0.0}]
    with patch.object(recorder_mock, "queue_task") as queued:
        importing = asyncio.create_task(target.import_batch(batch, "hour"))
        await asyncio.sleep(0)
        importing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await importing
    # A queued worker can still commit after its caller was cancelled.
    recorder_mock.queue_task(queued.call_args.args[0])
    await target.synchronize()
    await target.import_batch(batch, "hour")
    assert (
        len(
            (await target.read([target.target], dt_util.utc_from_timestamp(0), dt_util.utc_from_timestamp(3600)))[
                target.target
            ]
        )
        == 1
    )


@pytest.mark.parametrize("change", [{"target": "sensor.other"}, {"unit": "Wh"}, {"phase": "broken"}])
async def test_invalid_or_moved_journal_is_rejected(pending_group: PendingGroup, change: dict[str, str]) -> None:
    job = pending_group.job
    stored = await job.store.async_load()
    await job.store.async_save({**stored, **change})
    with pytest.raises(HistoryImportError, match=r"changed|invalid"):
        await job.load(["sensor.source"])


async def test_hourly_foundation_must_be_reliable(pending_group: PendingGroup) -> None:
    job = pending_group.job
    await job.import_short_term(pending_group.boundary)
    job.journal.phase = ImportPhase.LIVE
    await job.import_short_term(pending_group.boundary)
    job.journal.phase = ImportPhase.HOURLY
    job.journal.aggregation.known_intervals = 0
    with pytest.raises(HistoryImportError, match="two adjacent"):
        await job.import_hourly()
    job.journal.aggregation.known_intervals = 1
    with pytest.raises(HistoryImportError, match="disagree"):
        await job.import_hourly()
    with pytest.raises(HistoryImportError, match="recent recorder reference"):
        await job.read_references(["sensor.missing"], pending_group.boundary)


@pytest.mark.parametrize("problem", ["members", "source", "history", "database"])
async def test_transition_failure_is_reported(hass: HomeAssistant, pending_group: PendingGroup, problem: str) -> None:
    pending = pending_group
    if problem == "members":
        pending.job.journal.sources = ["sensor.old"]
        await pending.job.save()
        await setup_pending_group(hass, pending)
        assert_failure(hass, "members changed")
    elif problem == "source":
        hass.states.async_remove("sensor.source")
        await setup_pending_group(hass, pending)
        assert_failure(hass, "No cumulative")
    elif problem == "history":
        with patch(
            "custom_components.powercalc.sensors.group.history_sensor.history.get_significant_states", return_value={}
        ):
            await setup_pending_group(hass, pending)
        assert_failure(hass, "state history is missing")
    else:
        failed = hass.loop.create_future()
        failed.set_result(False)
        with patch.object(pending.job.recorder.recorder, "async_db_ready", failed):
            await setup_pending_group(hass, pending)
        assert_failure(hass, "database is unavailable")


async def test_partial_import_warns_and_invalid_live_updates_stop_the_sensor(
    hass: HomeAssistant, pending_group: PendingGroup, freezer: FrozenDateTimeFactory
) -> None:
    pending_group.job.journal.aggregation.missing_intervals = 2
    await pending_group.job.save()
    await setup_pending_group(hass, pending_group)
    assert hass.states.get("sensor.history_energy").attributes["history_incomplete_intervals"] == 2
    assert any(issue.translation_key == "group_history_partial" for issue in ir.async_get(hass).issues.values())
    for service, data in [("reset_energy", {}), ("calibrate_energy", {"value": "10"})]:
        with pytest.raises(HomeAssistantError, match="not supported"):
            await hass.services.async_call(
                DOMAIN, service, {"entity_id": "sensor.history_energy", **data}, blocking=True
            )
    hass.states.async_remove("sensor.source")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.source", "1", {"unit_of_measurement": "W", "state_class": "total"})
    await hass.async_block_till_done()
    assert_failure(hass, "Unsupported energy unit")


async def test_transition_waits_for_recorder_event(hass: HomeAssistant, pending_group: PendingGroup) -> None:
    real_read = GroupHistoryImport.read_references
    delayed = True

    async def delayed_reference(
        job: GroupHistoryImport, sources: list[str], boundary: datetime
    ) -> dict[str, StatisticsRow]:
        nonlocal delayed
        if delayed:
            delayed = False
            hass.loop.call_soon(hass.bus.async_fire, EVENT_RECORDER_5MIN_STATISTICS_GENERATED)
            raise HistoryImportError("Source compilation is pending")
        return await real_read(job, sources, boundary)

    with patch.object(GroupHistoryImport, "read_references", delayed_reference):
        await setup_pending_group(hass, pending_group)
    assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(0.5)


async def test_transition_timeout_is_reported(hass: HomeAssistant, pending_group: PendingGroup) -> None:
    with (
        patch.object(GroupHistoryImport, "read_references", side_effect=HistoryImportError("No current data")),
        patch(
            "custom_components.powercalc.sensors.group.history_sensor.asyncio.timeout_at",
            return_value=asyncio.timeout(0),
        ),
    ):
        await setup_pending_group(hass, pending_group)
    assert_failure(hass, "Timed out")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("slow_step", ["bridge", "capture"])
async def test_crossing_an_interval_rebuilds_the_handoff(
    hass: HomeAssistant, pending_group: PendingGroup, freezer: FrozenDateTimeFactory, slow_step: str
) -> None:
    """A slow replay or storage operation must not start live collection on an old interval."""
    real_import = GroupHistoryImport.import_short_term
    real_save = GroupHistoryImport.save
    delayed = True
    boundary = pending_group.boundary

    async def advance_interval() -> None:
        nonlocal delayed
        delayed = False
        freezer.move_to(boundary + timedelta(minutes=6))
        do_adhoc_statistics(hass, start=boundary)
        await async_wait_recording_done(hass)

    async def slow_import(job: GroupHistoryImport, end: datetime) -> None:
        await real_import(job, end)
        if delayed and slow_step == "bridge":
            await advance_interval()

    async def slow_save(job: GroupHistoryImport) -> None:
        await real_save(job)
        if delayed and slow_step == "capture" and job.journal.phase == ImportPhase.LIVE:
            await advance_interval()

    with (
        patch.object(GroupHistoryImport, "import_short_term", slow_import),
        patch.object(GroupHistoryImport, "save", slow_save),
    ):
        await setup_pending_group(hass, pending_group)
    state = hass.states.get("sensor.history_energy")
    assert state.attributes["history_import_status"] == "completed"
    assert float(state.state) == pytest.approx(0.5)
    freezer.move_to(boundary + timedelta(minutes=7))
    hass.states.async_set(
        "sensor.source",
        "300.2",
        {
            "device_class": "energy",
            "state_class": "total",
            "unit_of_measurement": "kWh",
        },
    )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    freezer.move_to(boundary + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=boundary + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    rows = await pending_group.job.recorder.read(
        ["sensor.history_energy"], boundary, boundary + timedelta(minutes=10), "5minute"
    )
    assert [row["sum"] for row in rows["sensor.history_energy"]] == pytest.approx([0.5, 0.7])
