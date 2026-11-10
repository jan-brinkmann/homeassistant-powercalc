"""Exercise the actual recorder, including its short-term statistics cache."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from _pytest.fixtures import SubRequest
from freezegun import freeze_time
from homeassistant import loader
from homeassistant.components.recorder import Recorder, migration
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_import_statistics,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir, recorder as recorder_helper
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.restore_state import async_get as get_restore_state_data
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import _SHUTDOWN_RUN_CALLBACK_THREADSAFE
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_test_home_assistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
    get_start_time,
    statistics_during_period,
)
from pytest_homeassistant_custom_component.typing import RecorderInstanceContextManager
from sqlalchemy.orm import Session

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
from custom_components.powercalc.sensors.group.history import AggregationState, HistoryImportError, aggregate_statistics
from custom_components.powercalc.sensors.group.history_import import GroupHistoryImport, ImportJournal, ImportPhase
from custom_components.powercalc.sensors.group.history_recorder import HistoryRecorder, Period
from custom_components.powercalc.sensors.group.history_sensor import GroupHistoryInitializer
from tests.common import create_mock_group_entry


@pytest.fixture
def mock_recorder_before_hass(request: SubRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    request.getfixturevalue("recorder_db_url")
    monkeypatch.setattr(migration, "Recorder", Recorder, raising=False)
    monkeypatch.setattr(recorder_helper, "Session", Session, raising=False)


@pytest.mark.parametrize("seed_short_term", [False, True])
async def test_recorder_continuation(hass: HomeAssistant, recorder_mock: Recorder, seed_short_term: bool) -> None:
    """Hourly import alone loses the historical sum on the first live interval."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_start()
    recorder_mock.engine.echo = False
    start = get_start_time(dt_util.utcnow()).replace(minute=0) + timedelta(hours=1)
    metadata = StatisticMetaData(
        statistic_id="sensor.imported_group",
        source="recorder",
        name=None,
        unit_of_measurement="kWh",
        unit_class="energy",
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
    )
    with freeze_time(start) as freezer:
        async_import_statistics(hass, metadata, [{"start": start - timedelta(hours=1), "state": 12.0, "sum": 12.0}])
        if seed_short_term:
            recorder_mock.async_import_statistics(
                metadata,
                [{"start": start - timedelta(minutes=5), "state": 12.0, "sum": 12.0}],
                StatisticsShortTerm,
            )
        await async_wait_recording_done(hass)
        for minute, value in [(1, 12.2), (6, 12.5), (11, 12.7)]:
            freezer.move_to(start + timedelta(minutes=minute))
            hass.states.async_set(
                "sensor.imported_group",
                value,
                {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
            )
            await async_wait_recording_done(hass)
            freezer.move_to(start + timedelta(minutes=minute + 4))
            do_adhoc_statistics(hass, start=start + timedelta(minutes=minute - 1))
            await async_wait_recording_done(hass)
        rows = await hass.async_add_executor_job(
            lambda: statistics_during_period(hass, start, statistic_ids={"sensor.imported_group"}, period="5minute")
        )
        expected = [12.2, 12.5, 12.7] if seed_short_term else [0.0, 0.3, 0.5]
        assert [row["sum"] for row in rows["sensor.imported_group"]] == pytest.approx(expected)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("unavailable,during_import", [(False, False), (True, False), (False, True)])
async def test_import_live_transition_and_reload(
    hass: HomeAssistant, recorder_mock: Recorder, unavailable: bool, during_import: bool
) -> None:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_start()
    recorder_mock.engine.echo = False
    boundary = get_start_time(dt_util.utcnow()).replace(minute=0) + timedelta(hours=1)
    attributes = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"}
    metadata = StatisticMetaData(
        statistic_id="sensor.source",
        source="recorder",
        name=None,
        unit_of_measurement="kWh",
        unit_class="energy",
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
    )
    done = asyncio.Event()

    @callback
    def changed(event: object) -> None:
        state = hass.states.get("sensor.history_energy")
        if state and state.attributes.get("history_import_status") in ["completed", "failed"]:
            done.set()

    hass.bus.async_listen("state_changed", changed)
    with freeze_time(boundary - timedelta(minutes=1), real_asyncio=True) as freezer:
        hass.states.async_set("sensor.source", "300.5", attributes)
        async_import_statistics(
            hass,
            metadata,
            [
                {"start": boundary - timedelta(hours=3), "state": 300.0, "sum": 100.0},
                {"start": boundary - timedelta(hours=2), "state": 300.3, "sum": 100.3},
                {"start": boundary - timedelta(hours=1), "state": 300.5, "sum": 100.5},
            ],
        )
        recorder_mock.async_import_statistics(
            metadata,
            [{"start": boundary - timedelta(minutes=5), "state": 300.5, "sum": 100.5}],
            StatisticsShortTerm,
        )
        await async_wait_recording_done(hass)
        freezer.move_to(boundary + timedelta(minutes=1))
        hass.states.async_set("sensor.source", "unavailable" if unavailable else "300.700001", attributes)
        await async_wait_recording_done(hass)
        freezer.move_to(boundary + timedelta(minutes=2))
        real_capture = GroupHistoryInitializer._capture_handoff  # noqa: SLF001

        async def capture_with_incoming_reading(sensor: GroupHistoryInitializer, boundary: datetime) -> None:
            importing = hass.states.get("sensor.history_energy")
            assert importing.state == "unknown"
            assert importing.attributes["history_import_status"] == "short_term"
            assert "state_class" not in importing.attributes
            await real_capture(sensor, boundary)
            if during_import:
                freezer.move_to(boundary + timedelta(minutes=3))
                hass.states.async_set("sensor.source", "300.8", attributes)
                await async_wait_recording_done(hass)

        with patch.object(GroupHistoryInitializer, "_capture_handoff", capture_with_incoming_reading):
            entry = await create_mock_group_entry(
                hass,
                "History",
                {
                    CONF_GROUP_ENERGY_ENTITIES: ["sensor.source"],
                    CONF_GROUP_TYPE: GroupType.CUSTOM,
                    CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
                    CONF_GROUP_ENERGY_START_AT_ZERO: False,
                    CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
                },
            )
            await hass.async_block_till_done(wait_background_tasks=True)
        group = hass.states.get("sensor.history_energy")
        expected_first = 0.5 if unavailable else (0.8 if during_import else 0.7)
        if unavailable:
            assert group.state == "unavailable"
        else:
            assert done.is_set()
            assert group.attributes["history_import_status"] == "completed"
            assert float(group.state) == pytest.approx(300 + expected_first)

        freezer.move_to(boundary + timedelta(minutes=5))
        do_adhoc_statistics(hass, start=boundary)
        await async_wait_recording_done(hass)
        freezer.move_to(boundary + timedelta(minutes=6))
        hass.states.async_set("sensor.source", "300.9", attributes)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
        assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(300.9)
        done.clear()
        # A reload reconstructs a new entity and loads the persisted total together
        # with its source references. It must never run another historical import.
        with patch.object(HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected reimport")):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
            assert done.is_set()
        assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(300.9)
        freezer.move_to(boundary + timedelta(minutes=10))
        do_adhoc_statistics(hass, start=boundary + timedelta(minutes=5))
        await async_wait_recording_done(hass)
        rows = await hass.async_add_executor_job(
            lambda: statistics_during_period(hass, boundary, statistic_ids={"sensor.history_energy"}, period="5minute")
        )
        expected_sums = [0.9] if unavailable else [expected_first, 0.9]
        assert [row["sum"] for row in rows["sensor.history_energy"]] == pytest.approx(expected_sums)

        # Exercise the recorder's real hourly rollup, not only its five-minute rows.
        for index, minute in enumerate(range(11, 60, 5), start=1):
            freezer.move_to(boundary + timedelta(minutes=minute))
            hass.states.async_set("sensor.source", str(300.9 + index / 10), attributes)
            await hass.async_block_till_done()
            await async_wait_recording_done(hass)
            freezer.move_to(boundary + timedelta(minutes=minute + 4))
            do_adhoc_statistics(hass, start=boundary + timedelta(minutes=minute - 1))
            await async_wait_recording_done(hass)
        rows = await hass.async_add_executor_job(
            lambda: statistics_during_period(hass, boundary, statistic_ids={"sensor.history_energy"}, period="hour")
        )
        assert [row["sum"] for row in rows["sensor.history_energy"]] == pytest.approx([1.9])

        # A replacement member starts with its own baseline; earlier history remains
        # frozen and the imported group total does not jump to the replacement meter.
        freezer.move_to(boundary + timedelta(minutes=61))
        hass.states.async_set("sensor.replacement", "1000", attributes)
        await async_wait_recording_done(hass)
        with patch.object(HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected reimport")):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_GROUP_ENERGY_ENTITIES: ["sensor.replacement"]}
            )
            await hass.async_block_till_done(wait_background_tasks=True)
        assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(1301.9)
        freezer.move_to(boundary + timedelta(minutes=62))
        hass.states.async_set("sensor.replacement", "1000.2", attributes)
        await hass.async_block_till_done()
        assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(1302.1)


async def test_import_batches_are_idempotent_and_detect_conflicts(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    await hass.async_start()
    start = get_start_time(dt_util.utcnow()).replace(minute=0) - timedelta(hours=2)
    importer = HistoryRecorder(hass, "sensor.target", "kWh")
    await importer.require_empty_target()
    batch = [{"start": start.timestamp(), "state": 0.0, "sum": 0.0}]
    await importer.import_batch(batch, "hour")
    await importer.import_batch(batch, "hour")
    with pytest.raises(HistoryImportError, match="already exist"):
        await importer.require_empty_target()
    with pytest.raises(HistoryImportError, match="Conflicting"):
        await importer.import_batch([{**batch[0], "sum": 10.0}], "hour")
    assert len((await importer.read(["sensor.target"], start, start + timedelta(hours=1)))["sensor.target"]) == 1


async def test_gap_does_not_create_a_spike_in_a_later_date_range(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    """The energy dashboard must retain the pre-gap sum as its query reference."""
    await hass.async_start()
    start = get_start_time(dt_util.utcnow()).replace(minute=0) - timedelta(hours=10)
    source_rows = [
        {"start": (start + timedelta(hours=index)).timestamp(), "sum": value}
        for index, value in enumerate([100, 101, None, 200, 200.2, 200.2])
    ]
    aggregated = aggregate_statistics(["sensor.source"], {"sensor.source": source_rows}, AggregationState())
    target = HistoryRecorder(hass, "sensor.target", "kWh")
    await target.import_batch(
        [{"start": row["start"].timestamp(), "sum": row["sum"], "state": row["state"]} for row in aggregated], "hour"
    )
    rows = await hass.async_add_executor_job(
        lambda: statistics_during_period(
            hass,
            start + timedelta(hours=4),
            statistic_ids={"sensor.target"},
            period="hour",
            types={"change"},
        )
    )
    assert [row["change"] for row in rows["sensor.target"]] == pytest.approx([0.2, 0])


async def test_recorder_converts_source_units_before_aggregation(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    await hass.async_start()
    start = get_start_time(dt_util.utcnow()).replace(minute=0) - timedelta(hours=3)
    for source, unit, values in [("sensor.a", "Wh", [12000, 12300]), ("sensor.b", "MJ", [3600, 3600.72])]:
        recorder = HistoryRecorder(hass, source, unit)
        await recorder.import_batch(
            [
                {"start": (start + timedelta(hours=i)).timestamp(), "sum": value, "state": value}
                for i, value in enumerate(values)
            ],
            "hour",
        )
    target = HistoryRecorder(hass, "sensor.target", "kWh")
    rows = await target.read(["sensor.a", "sensor.b"], start, start + timedelta(hours=2))
    aggregated = aggregate_statistics(["sensor.a", "sensor.b"], rows, AggregationState())
    assert [row["sum"] for row in aggregated] == pytest.approx([0, 0.5])


@pytest.mark.parametrize("invalid", [{"sum": None}, {"state": None}, {"start": 1.0}])
async def test_incomplete_short_term_bridge_is_rejected(
    hass: HomeAssistant, recorder_mock: Recorder, invalid: StatisticsRow
) -> None:
    await hass.async_start()
    boundary = get_start_time(dt_util.utcnow()).replace(minute=0) - timedelta(hours=2)
    source = HistoryRecorder(hass, "sensor.source", "kWh")
    await source.import_batch([{"start": boundary.timestamp(), "sum": 100.2, "state": 300.2, **invalid}], "5minute")
    job = GroupHistoryImport(hass, "invalid_bridge", "sensor.target", "kWh")
    job.journal = ImportJournal(
        "sensor.target",
        "kWh",
        ["sensor.source"],
        boundary.timestamp(),
        cursor=boundary.timestamp(),
        phase=ImportPhase.SHORT_TERM,
        native_offset=0.0,
    )
    with pytest.raises(HistoryImportError, match="incomplete"):
        await job.import_short_term(boundary + timedelta(minutes=5))
    assert not await job.recorder.read(["sensor.target"], boundary, boundary + timedelta(minutes=5), "5minute")


async def test_resume_after_recorder_commit_before_checkpoint(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    """An acknowledged batch can be safely retried after a checkpoint write fails."""
    await hass.async_start()
    boundary = get_start_time(dt_util.utcnow()).replace(minute=0)
    source = HistoryRecorder(hass, "sensor.source", "kWh")
    await source.import_batch(
        [
            {"start": (boundary - timedelta(hours=3 - i)).timestamp(), "sum": 100 + i * 0.2, "state": 300 + i * 0.2}
            for i in range(3)
        ],
        "hour",
    )
    await source.import_batch(
        [{"start": (boundary - timedelta(minutes=5)).timestamp(), "sum": 100.4, "state": 300.4}], "5minute"
    )
    with freeze_time(boundary + timedelta(minutes=1)):
        job = GroupHistoryImport(hass, "resumable", "sensor.resumable", "kWh")
        await job.load(["sensor.source"])
        job.journal.cursor = (boundary - timedelta(hours=3)).timestamp()
        await job.save()
        real_save = job.save

        async def fail_checkpoint_after_commit() -> None:
            if job.journal.imported_rows:
                raise HistoryImportError("Simulated crash after recorder commit")
            await real_save()

        with (
            patch.object(job, "save", side_effect=fail_checkpoint_after_commit),
            patch("custom_components.powercalc.sensors.group.history_import.BATCH_DURATION", timedelta(hours=2)),
            pytest.raises(HistoryImportError, match="Simulated crash"),
        ):
            await job.import_hourly()
        resumed = GroupHistoryImport(hass, "resumable", "sensor.resumable", "kWh")
        await resumed.load(["sensor.changed_member"])
        assert resumed.journal.sources == ["sensor.source"]
        assert resumed.journal.pending
        await resumed.import_hourly()
        assert not resumed.journal.pending
        rows = await resumed.recorder.read(["sensor.resumable"], boundary - timedelta(hours=3), boundary)
        assert [row["sum"] for row in rows["sensor.resumable"]] == pytest.approx([0, 0.2, 0.4])
        assert len(rows["sensor.resumable"]) == 3


async def test_failed_journal_write_prevents_recorder_import(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    await hass.async_start()
    job = GroupHistoryImport(hass, "failed_storage", "sensor.target", "kWh")
    job.journal = ImportJournal("sensor.target", "kWh", ["sensor.source"], 0)
    # Home Assistant's storage API logs certain write failures and returns normally.
    with patch.object(job.store, "async_save"), pytest.raises(HistoryImportError, match="checkpoint"):
        await job.stage_batch([{"start": 0.0, "state": 0.0, "sum": 0.0}], "hour")
    await job.recorder.require_empty_target()


async def test_missing_statistics_reports_failure_without_completion(
    hass: HomeAssistant, recorder_mock: Recorder
) -> None:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_start()
    hass.states.async_set(
        "sensor.source", "100", {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"}
    )
    await create_mock_group_entry(
        hass,
        "History",
        {
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.source"],
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_GROUP_ENERGY_START_AT_ZERO: False,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    group = hass.states.get("sensor.history_energy")
    assert group.state == "unknown"
    assert group.attributes["history_import_status"] == "failed"
    assert "state_class" not in group.attributes
    assert any(
        domain == DOMAIN and issue.translation_key == "group_history_failed"
        for (domain, _), issue in ir.async_get(hass).issues.items()
    )


async def test_resume_bridge_after_short_term_commit(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    """The hourly rollup remains pending if the short-term bridge was committed first."""
    await hass.async_start()
    boundary = get_start_time(dt_util.utcnow()).replace(minute=0) - timedelta(hours=2)
    source = HistoryRecorder(hass, "sensor.source", "kWh")
    await source.import_batch(
        [
            {
                "start": (boundary + timedelta(minutes=i * 5)).timestamp(),
                "state": 300 + (i + 1) / 10,
                "sum": 100 + (i + 1) / 10,
            }
            for i in range(12)
        ],
        "5minute",
    )
    job = GroupHistoryImport(hass, "bridge", "sensor.target", "kWh")
    job.journal = ImportJournal(
        "sensor.target",
        "kWh",
        ["sensor.source"],
        boundary.timestamp(),
        cursor=boundary.timestamp(),
        phase=ImportPhase.SHORT_TERM,
        native_offset=0.0,
        imported_rows=1,
        aggregation=AggregationState(
            previous={"sensor.source": {"start": boundary.timestamp() - 300, "state": 300, "sum": 100}}
        ),
    )
    real_import = job.recorder.import_batch

    async def interrupt_hourly(rows: list[StatisticsRow], period: Period, *, first_batch: bool = False) -> None:
        if period == "hour":
            raise HistoryImportError("Interrupted hourly rollup")
        await real_import(rows, period, first_batch=first_batch)

    with (
        patch.object(job.recorder, "import_batch", interrupt_hourly),
        pytest.raises(HistoryImportError, match="Interrupted"),
    ):
        await job.import_short_term(boundary + timedelta(hours=1))
    resumed = GroupHistoryImport(hass, "bridge", "sensor.target", "kWh")
    await resumed.load(["sensor.source"])
    assert resumed.journal.pending_hourly
    await resumed.import_short_term(boundary + timedelta(hours=1))
    rows = await resumed.recorder.read(["sensor.target"], boundary, boundary + timedelta(hours=1))
    assert [row["sum"] for row in rows["sensor.target"]] == pytest.approx([1.2])


@pytest.mark.timeout(60)
@pytest.mark.parametrize("persistent_database", [True])
async def test_completed_import_survives_home_assistant_restart(
    hass: HomeAssistant, async_test_recorder: RecorderInstanceContextManager
) -> None:
    """Restart HA and its recorder against the same database and persisted journal."""
    boundary = get_start_time(dt_util.utcnow()).replace(minute=0) + timedelta(hours=1)
    attributes = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"}
    with freeze_time(boundary - timedelta(minutes=1), real_asyncio=True) as freezer:
        async with async_test_recorder(hass):
            assert await async_setup_component(hass, "sensor", {})
            await hass.async_start()
            hass.states.async_set("sensor.source", "300.5", attributes)
            source = HistoryRecorder(hass, "sensor.source", "kWh")
            await source.import_batch(
                [
                    {"start": (boundary - timedelta(hours=3 - i)).timestamp(), "sum": value, "state": 200 + value}
                    for i, value in enumerate([100, 100.3, 100.5])
                ],
                "hour",
            )
            await source.import_batch(
                [{"start": (boundary - timedelta(minutes=5)).timestamp(), "sum": 100.5, "state": 300.5}], "5minute"
            )
            freezer.move_to(boundary + timedelta(minutes=1))
            hass.states.async_set("sensor.source", "300.7", attributes)
            await async_wait_recording_done(hass)
            freezer.move_to(boundary + timedelta(minutes=2))
            entry = await create_mock_group_entry(
                hass,
                "History",
                {
                    CONF_GROUP_ENERGY_ENTITIES: ["sensor.source"],
                    CONF_GROUP_TYPE: GroupType.CUSTOM,
                    CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
                    CONF_GROUP_ENERGY_START_AT_ZERO: False,
                    CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
                },
            )
            await hass.async_block_till_done(wait_background_tasks=True)
            assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(300.7)
            freezer.move_to(boundary + timedelta(minutes=5))
            do_adhoc_statistics(hass, start=boundary)
            await async_wait_recording_done(hass)
            assert await hass.config_entries.async_unload(entry.entry_id)
            # The HA test helper disables the restore-state store and periodic
            # dumps. Enable real storage here to exercise the original restore path.
            restore_data = get_restore_state_data(hass)
            restore_data.store = Store(hass, 1, "core.restore_state", encoder=JSONEncoder)
            await restore_data.async_dump_states()
            await hass.async_stop(force=True)

        # The pytest loop is reused for the second instance. Apply the same loop
        # cleanup as async_test_home_assistant would on leaving the first context.
        delattr(hass.loop, _SHUTDOWN_RUN_CALLBACK_THREADSAFE)
        # No recorder caches, entity objects or group reference stores are reused.
        async with async_test_home_assistant(config_dir=hass.config.config_dir) as restarted:
            restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
            restore_data = get_restore_state_data(restarted)
            restore_data.store = Store(restarted, 1, "core.restore_state", encoder=JSONEncoder)
            await restore_data.async_load()
            try:
                async with async_test_recorder(restarted):
                    assert await async_setup_component(restarted, "sensor", {})
                    await restarted.async_start()
                    freezer.move_to(boundary + timedelta(minutes=6))
                    restarted.states.async_set("sensor.source", "300.9", attributes)
                    restored_entry = MockConfigEntry(
                        domain=entry.domain,
                        entry_id=entry.entry_id,
                        unique_id=entry.unique_id,
                        title=entry.title,
                        data=dict(entry.data),
                    )
                    restored_entry.add_to_hass(restarted)
                    with patch.object(
                        HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected reimport")
                    ):
                        assert await restarted.config_entries.async_setup(restored_entry.entry_id)
                        await restarted.async_block_till_done(wait_background_tasks=True)
                    assert float(restarted.states.get("sensor.history_energy").state) == pytest.approx(300.9)
                    for minute, value in [(10, 300.9), (15, 301.1)]:
                        freezer.move_to(boundary + timedelta(minutes=minute - 4))
                        restarted.states.async_set("sensor.source", str(value), attributes)
                        await restarted.async_block_till_done()
                        await async_wait_recording_done(restarted)
                        freezer.move_to(boundary + timedelta(minutes=minute))
                        do_adhoc_statistics(restarted, start=boundary + timedelta(minutes=minute - 5))
                        await async_wait_recording_done(restarted)
                    target = HistoryRecorder(restarted, "sensor.history_energy", "kWh")
                    rows = await target.read([target.target], boundary, boundary + timedelta(minutes=15), "5minute")
                    assert [row["sum"] for row in rows[target.target]] == pytest.approx([0.7, 0.9, 1.1])
                    assert await restarted.config_entries.async_unload(restored_entry.entry_id)
            finally:
                await restarted.async_stop(force=True)
