"""Historical counter readings and live readings need one common origin."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
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
    GroupType,
    UnitPrefix,
)
from custom_components.powercalc.sensors.group.history import HistoryImportError
from custom_components.powercalc.sensors.group.history_import import (
    GroupHistoryImport,
    ImportJournal,
    ImportPhase,
    align_counter_states,
)
from custom_components.powercalc.sensors.group.history_recorder import HistoryRecorder
from tests.common import create_mock_group_entry
from tests.sensors.group.test_history_recorder import mock_recorder_before_hass as mock_recorder_before_hass


@dataclass
class FlatHistoryGroup:
    entry: MockConfigEntry
    boundary: datetime


@pytest.fixture
async def flat_history_group(
    hass: HomeAssistant, recorder_mock: Recorder, freezer: FrozenDateTimeFactory
) -> FlatHistoryGroup:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_start()
    boundary = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    attributes = {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    freezer.move_to(boundary - timedelta(minutes=1))
    for entity_id, first, current in [["sensor.a", 0.0073, 2.5123], ["sensor.b", 0.005, 1.608]]:
        recorder = HistoryRecorder(hass, entity_id, "kWh")
        await recorder.import_batch(
            [
                {
                    "start": (boundary - timedelta(hours=5 - index)).timestamp(),
                    "state": value,
                    "sum": value,
                }
                for index, value in enumerate([first, current, current, current, current])
            ],
            "hour",
        )
        await recorder.import_batch(
            [{"start": boundary.timestamp() - 300, "state": current, "sum": current}], "5minute"
        )
        hass.states.async_set(entity_id, str(current), attributes)
    await async_wait_recording_done(hass)
    freezer.move_to(boundary + timedelta(minutes=2))
    entry = await create_mock_group_entry(
        hass,
        "History",
        {
            CONF_GROUP_TYPE: GroupType.CUSTOM,
            CONF_GROUP_ENERGY_ENTITIES: ["sensor.a", "sensor.b"],
            CONF_GROUP_ENERGY_START_AT_ZERO: False,
            CONF_GROUP_ENERGY_IMPORT_HISTORY: True,
            CONF_ENERGY_SENSOR_UNIT_PREFIX: UnitPrefix.KILO,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.history_energy").attributes["history_import_status"] == "completed"
    return FlatHistoryGroup(entry, boundary)


async def test_flat_multiple_source_history_has_no_handoff_step(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup, freezer: FrozenDateTimeFactory
) -> None:
    """Reproduce 4.108 kWh historical states followed by a false jump to 4.1203 kWh."""
    boundary = flat_history_group.boundary
    target = HistoryRecorder(hass, "sensor.history_energy", "kWh")
    rows = await target.read([target.target], boundary - timedelta(hours=3), boundary)
    assert [row["state"] for row in rows[target.target]] == pytest.approx([4.1203] * 3)
    assert [row["sum"] for row in rows[target.target]] == pytest.approx([4.108] * 3)
    assert float(hass.states.get(target.target).state) == pytest.approx(4.1203)
    freezer.move_to(boundary + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=boundary)
    await async_wait_recording_done(hass)
    short_term = await target.read([target.target], boundary, boundary + timedelta(minutes=5), "5minute")
    assert short_term[target.target][0]["state"] == pytest.approx(4.1203)
    assert short_term[target.target][0]["sum"] == pytest.approx(4.108)
    changes = await target.execute(
        statistics_during_period,
        hass,
        boundary - timedelta(hours=3),
        boundary,
        {target.target},
        "hour",
        {"energy": "kWh"},
        types={"change"},
    )
    assert [row["change"] for row in changes[target.target]] == pytest.approx([0, 0, 0])


async def test_completed_import_from_previous_version_is_aligned_once(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup
) -> None:
    """Repair owned hourly states without changing live totals or importing sources again."""
    entry = flat_history_group.entry
    boundary = flat_history_group.boundary
    job = GroupHistoryImport(hass, f"{entry.unique_id}_energy", "sensor.history_energy", "kWh")
    await job.load(["sensor.a", "sensor.b"])
    rows = (await job.recorder.read([job.journal.target], boundary - timedelta(hours=5), boundary))[job.journal.target]
    # Recreate the exact output and journal format of the preceding implementation.
    async_import_statistics(
        hass,
        job.recorder.metadata,
        [
            {"start": datetime.fromtimestamp(row["start"], dt_util.UTC), "state": row["sum"], "sum": row["sum"]}
            for row in rows
        ],
    )
    await job.recorder.synchronize()
    stored = await job.store.async_load()
    legacy = {
        key: value
        for key, value in stored.items()
        if key
        not in ["pending_previous", "state_alignment_cursor", "state_alignment_complete", "runtime_handoff_complete"]
    }
    await job.store.async_save(legacy)
    source_rows = await job.recorder.read(["sensor.a", "sensor.b"], boundary - timedelta(hours=5), boundary)
    with patch.object(GroupHistoryImport, "import_hourly", side_effect=AssertionError("Unexpected source reimport")):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get(job.journal.target).state) == pytest.approx(4.1203)
    corrected = (await job.recorder.read([job.journal.target], boundary - timedelta(hours=5), boundary))[
        job.journal.target
    ]
    assert [row["state"] for row in corrected] == pytest.approx([0.0123, *[4.1203] * 4])
    assert [row["sum"] for row in corrected] == [row["sum"] for row in rows]
    assert await job.recorder.read(["sensor.a", "sensor.b"], boundary - timedelta(hours=5), boundary) == source_rows
    await job.load([])
    assert job.journal.state_alignment_complete
    with patch.object(HistoryRecorder, "import_batch", side_effect=AssertionError("Unexpected repeated correction")):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("offset", [12.3, -12.3])
async def test_alignment_resumes_after_commit_before_checkpoint(
    hass: HomeAssistant, recorder_mock: Recorder, offset: float
) -> None:
    await hass.async_start()
    boundary = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    start = boundary - timedelta(hours=3)
    job = GroupHistoryImport(hass, "alignment_crash", "sensor.target", "kWh")
    rows = [
        {"start": (start + timedelta(hours=index)).timestamp(), "state": total, "sum": total}
        for index, total in enumerate([0, 0.3, 0.5])
    ]
    await job.recorder.import_batch(rows, "hour")
    job.journal = ImportJournal(
        "sensor.target",
        "kWh",
        ["sensor.source"],
        boundary.timestamp(),
        phase=ImportPhase.LIVE,
        native_offset=offset,
        state_alignment_cursor=start.timestamp(),
        imported_rows=3,
    )
    save = job.save

    async def interrupt_after_commit() -> None:
        if job.journal.pending_previous:
            await save()
        else:
            raise HistoryImportError("Interrupted after alignment commit")

    with (
        patch.object(job, "save", interrupt_after_commit),
        patch("custom_components.powercalc.sensors.group.history_import.BATCH_DURATION", timedelta(hours=2)),
        pytest.raises(HistoryImportError, match="Interrupted"),
    ):
        await job.align_hourly_states()
    resumed = GroupHistoryImport(hass, "alignment_crash", "sensor.target", "kWh")
    await resumed.load([])
    assert resumed.journal.pending_previous
    await resumed.align_hourly_states()
    corrected = (await resumed.recorder.read(["sensor.target"], start, boundary))["sensor.target"]
    assert [row["state"] for row in corrected] == pytest.approx([offset, offset + 0.3, offset + 0.5])
    assert [row["sum"] for row in corrected] == [row["sum"] for row in rows]
    assert resumed.journal.imported_rows == 3


@pytest.mark.parametrize("invalid", [{"state": None}, {"sum": None}, {"state": float("inf")}, {"state": 4.0}])
def test_alignment_rejects_unexpected_counter_states(invalid: StatisticsRow) -> None:
    with pytest.raises(HistoryImportError, match=r"incomplete|Conflicting"):
        align_counter_states([{"start": 0.0, "sum": 1.0, "state": 1.0, **invalid}], 2.0)


def test_alignment_handles_a_partially_corrected_batch() -> None:
    assert align_counter_states([{"start": 0.0, "sum": 1.0, "state": 3.0}], 2.0) == []
    rows = [{"start": 0.0, "sum": 1.0, "state": 3.0}, {"start": 3600.0, "sum": 1.5, "state": 1.5}]
    assert [row["state"] for row in align_counter_states(rows, 2.0)] == [3.0, 3.5]


async def test_alignment_requires_an_initialized_origin(hass: HomeAssistant, recorder_mock: Recorder) -> None:
    await hass.async_start()
    job = GroupHistoryImport(hass, "no_alignment_origin", "sensor.target", "kWh")
    job.journal = ImportJournal("sensor.target", "kWh", [], 3600)
    with pytest.raises(HistoryImportError, match="counter reference"):
        await job.align_hourly_states()


@pytest.mark.parametrize("conflict", ["sum", "missing", "changed"])
async def test_recorder_alignment_protects_consumption_and_conflicting_rows(
    hass: HomeAssistant, recorder_mock: Recorder, conflict: str
) -> None:
    await hass.async_start()
    recorder = HistoryRecorder(hass, "sensor.target", "kWh")
    old = {"start": 0.0, "sum": 1.0, "state": 1.0}
    await recorder.import_batch([old], "hour")
    new = {**old, "state": 3.0}
    previous = [old]
    if conflict == "sum":
        new["sum"] = 2.0
    elif conflict == "missing":
        new["start"] = 3600.0
        previous = [{**old, "start": 3600.0}]
    else:
        previous = [{**old, "state": 4.0}]
    with pytest.raises(HistoryImportError, match=r"preserve|disappeared|Conflicting"):
        await recorder.import_batch([new], "hour", previous_rows=previous)
    rows = await recorder.read([recorder.target], dt_util.utc_from_timestamp(0), dt_util.utc_from_timestamp(7200))
    assert len(rows[recorder.target]) == 1
    assert rows[recorder.target][0]["state"] == 1.0
    assert rows[recorder.target][0]["sum"] == 1.0


async def test_legacy_handoff_can_establish_a_new_member_baseline(
    hass: HomeAssistant, flat_history_group: FlatHistoryGroup
) -> None:
    entry = flat_history_group.entry
    job = GroupHistoryImport(hass, f"{entry.unique_id}_energy", "sensor.history_energy", "kWh")
    stored = await job.store.async_load()
    stored.pop("runtime_handoff_complete")
    await job.store.async_save(stored)
    hass.states.async_set(
        "sensor.replacement", "1000", {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"}
    )
    with patch.object(GroupHistoryImport, "import_hourly", side_effect=AssertionError("Unexpected source reimport")):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_GROUP_ENERGY_ENTITIES: ["sensor.replacement"]}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get("sensor.history_energy").state) == pytest.approx(4.1203)
    await job.load([])
    assert job.journal.runtime_handoff_complete
