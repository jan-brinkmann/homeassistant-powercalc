"""Regression cases for events arriving at the historical/live handoff."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.powercalc.sensors.group.history import HistoryImportError
from custom_components.powercalc.sensors.group.history_import import GroupHistoryImport, ImportPhase
from custom_components.powercalc.sensors.group.history_recorder import HistoryRecorder
from custom_components.powercalc.sensors.group.history_sensor import HistoryGroupedEnergySensor
from tests.sensors.group.test_history_failures import PendingGroup, pending_group as pending_group, setup_pending_group
from tests.sensors.group.test_history_recorder import mock_recorder_before_hass as mock_recorder_before_hass

ATTRIBUTES = {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}


async def test_reading_at_capture_time_is_included(
    hass: HomeAssistant, pending_group: PendingGroup, freezer: FrozenDateTimeFactory
) -> None:
    """Recorder history excludes its end timestamp, but the current reading is known."""
    hass.states.async_set("sensor.source", "300.2", ATTRIBUTES)
    await async_wait_recording_done(hass)
    await setup_pending_group(hass, pending_group)
    assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(0.7)
    freezer.move_to(pending_group.boundary + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=pending_group.boundary)
    await async_wait_recording_done(hass)
    rows = await pending_group.job.recorder.read(
        ["sensor.history_energy"], pending_group.boundary, pending_group.boundary + timedelta(minutes=5), "5minute"
    )
    assert [row["sum"] for row in rows["sensor.history_energy"]] == pytest.approx([0.7])


@pytest.mark.timeout(60)
async def test_reload_replays_all_buffered_reset_readings(
    hass: HomeAssistant, pending_group: PendingGroup, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pending_group(hass, pending_group)
    assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(0.5)
    freezer.move_to(pending_group.boundary + timedelta(minutes=3))
    replay = HistoryGroupedEnergySensor._replay_history  # noqa: SLF001

    async def replay_with_incoming_reset(sensor: HistoryGroupedEnergySensor, start: datetime, end: datetime) -> None:
        await replay(sensor, start, end)
        reset = pending_group.boundary.isoformat()
        for second, value, attributes in [
            [1, "301", ATTRIBUTES],
            [2, "0.1", {**ATTRIBUTES, "last_reset": reset}],
            [3, "0.2", {**ATTRIBUTES, "last_reset": reset}],
        ]:
            freezer.move_to(pending_group.boundary + timedelta(minutes=3, seconds=second))
            hass.states.async_set("sensor.source", value, attributes)
            await hass.async_block_till_done()

    with patch.object(HistoryGroupedEnergySensor, "_replay_history", replay_with_incoming_reset):
        assert await hass.config_entries.async_reload(pending_group.entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(1.7)
    freezer.move_to(pending_group.boundary + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=pending_group.boundary)
    await async_wait_recording_done(hass)
    rows = await pending_group.job.recorder.read(
        ["sensor.history_energy", "sensor.source"],
        pending_group.boundary,
        pending_group.boundary + timedelta(minutes=5),
        "5minute",
    )
    assert rows["sensor.source"][0]["sum"] == pytest.approx(101.2)
    assert rows["sensor.history_energy"][0]["sum"] == pytest.approx(1.7)


@pytest.mark.timeout(60)
async def test_startup_and_live_updates_keep_one_durable_checkpoint(
    hass: HomeAssistant, pending_group: PendingGroup, freezer: FrozenDateTimeFactory
) -> None:
    capture = HistoryGroupedEnergySensor._capture_live  # noqa: SLF001
    save = Store.async_save
    injected = False

    async def capture_with_buffer(sensor: HistoryGroupedEnergySensor, boundary: datetime) -> None:
        await capture(sensor, boundary)
        freezer.move_to(boundary + timedelta(minutes=2, seconds=1))
        hass.states.async_set("sensor.source", "300.2", ATTRIBUTES)
        await hass.async_block_till_done()

    async def delayed_write(store: Store, data: dict) -> None:
        nonlocal injected
        if (
            store.key.startswith("powercalc_group_history.")
            and data.get("live_total") == pytest.approx(0.7)
            and not injected
        ):
            injected = True
            freezer.move_to(pending_group.boundary + timedelta(minutes=2, seconds=2))
            hass.states.async_set("sensor.source", "300.4", ATTRIBUTES)
            await hass.async_block_till_done()
        await save(store, data)

    with (
        patch.object(HistoryGroupedEnergySensor, "_capture_live", capture_with_buffer),
        patch.object(Store, "async_save", delayed_write),
    ):
        await setup_pending_group(hass, pending_group)
    state = hass.states.get("sensor.history_energy")
    assert injected
    assert state.attributes["history_import_status"] == "completed"
    assert float(state.state) == pytest.approx(0.9)
    restored = GroupHistoryImport(hass, str(pending_group.entry.unique_id) + "_energy", "sensor.history_energy", "kWh")
    await restored.load(["sensor.source"])
    assert restored.journal.live_total == pytest.approx(0.9)
    assert restored.journal.members["sensor.source"]["value"] == pytest.approx(300.4)


@pytest.mark.timeout(60)
async def test_interrupted_capture_resumes_the_short_term_bridge(
    hass: HomeAssistant, pending_group: PendingGroup, freezer: FrozenDateTimeFactory
) -> None:
    """A persisted capture does not prove the group ever started live collection."""
    hass.states.async_set("sensor.source", "300.2", ATTRIBUTES)
    await async_wait_recording_done(hass)
    save = GroupHistoryImport.save

    async def crash_after_capture(job: GroupHistoryImport) -> None:
        await save(job)
        if job.journal.phase == ImportPhase.LIVE:
            raise asyncio.CancelledError

    with patch.object(GroupHistoryImport, "save", crash_after_capture):
        await setup_pending_group(hass, pending_group)
    assert hass.states.get("sensor.history_energy").state == "unknown"
    assert await hass.config_entries.async_unload(pending_group.entry.entry_id)
    freezer.move_to(pending_group.boundary + timedelta(minutes=3))
    hass.states.async_set("sensor.source", "300.3", ATTRIBUTES)
    await async_wait_recording_done(hass)
    freezer.move_to(pending_group.boundary + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=pending_group.boundary)
    await async_wait_recording_done(hass)
    freezer.move_to(pending_group.boundary + timedelta(minutes=6))
    hass.states.async_set("sensor.source", "300.4", ATTRIBUTES)
    await async_wait_recording_done(hass)
    await setup_pending_group(hass, pending_group)
    assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(0.9)
    freezer.move_to(pending_group.boundary + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=pending_group.boundary + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    rows = await pending_group.job.recorder.read(
        ["sensor.history_energy"], pending_group.boundary, pending_group.boundary + timedelta(minutes=10), "5minute"
    )
    assert [row["sum"] for row in rows["sensor.history_energy"]] == pytest.approx([0.8, 0.9])


async def test_missing_latest_hour_can_be_retried_before_any_target_write(
    hass: HomeAssistant, pending_group: PendingGroup
) -> None:
    boundary = pending_group.boundary
    source = HistoryRecorder(hass, "sensor.source", "kWh")
    await source.import_batch(
        [
            {"start": (boundary - timedelta(hours=3)).timestamp(), "sum": 99.0, "state": 299.0},
            {"start": (boundary - timedelta(hours=2)).timestamp(), "sum": 99.5, "state": 299.5},
        ],
        "hour",
    )
    job = GroupHistoryImport(hass, "delayed_hour", "sensor.delayed_hour", "kWh")
    with pytest.raises(HistoryImportError, match=r"Hourly.*reference"):
        await job.load(["sensor.source"])
    await job.recorder.require_empty_target()
    assert await job.store.async_load() is None
    # Recorder finishes the latest hourly rollup after the failed preflight.
    await source.import_batch(
        [
            {"start": (boundary - timedelta(hours=1)).timestamp(), "sum": 100.0, "state": 300.0},
        ],
        "hour",
    )
    await job.load(["sensor.source"])
    job.journal.cursor = (boundary - timedelta(hours=3)).timestamp()
    await job.import_hourly()
    rows = await job.recorder.read([job.recorder.target], boundary - timedelta(hours=3), boundary)
    assert [row["sum"] for row in rows[job.recorder.target]] == pytest.approx([0, 0.5, 1])


async def test_large_counter_offsets_do_not_hide_conflicting_statistics(
    hass: HomeAssistant, pending_group: PendingGroup
) -> None:
    recorder = HistoryRecorder(hass, "sensor.large_counter", "Wh")
    await recorder.import_batch([{"start": 0.0, "state": 1e12, "sum": 1e12}], "hour")
    with pytest.raises(HistoryImportError, match="Conflicting"):
        await recorder.import_batch([{"start": 0.0, "state": 1e12, "sum": 1e12 + 0.5}], "hour")


async def test_completed_legacy_journal_does_not_reimport(hass: HomeAssistant, pending_group: PendingGroup) -> None:
    await setup_pending_group(hass, pending_group)
    stored = await pending_group.job.store.async_load()
    assert stored is not None
    legacy = {
        key: value for key, value in stored.items() if key not in ["live_started", "native_offset", "short_term_seeded"]
    }
    await pending_group.job.store.async_save(legacy)
    with patch.object(HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected reimport")):
        assert await hass.config_entries.async_reload(pending_group.entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(0.5)
