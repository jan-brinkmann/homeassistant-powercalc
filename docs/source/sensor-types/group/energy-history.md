# Importing group energy history

When creating a **Custom**, **Domain based**, or **Subtract** group in the UI,
enable **Import energy long-term statistics from sources once**
to copy the known historical consumption of its sources into the new group energy sensor.
It is disabled by default and is not offered when editing an existing group.

For a Custom group, turn off **Start at zero**. Combining the two options is rejected: the live
counter starts with the current sum of its source counter readings, as a new Custom group with
the same sources and start setting would. Domain groups likewise add their source readings;
Subtract groups subtract the selected partial counters from the base counter. Historical
consumption is imported separately. **Force calculate energy** is incompatible.
The group must have its energy sensor enabled.

## Supported sources

The first implementation supports existing cumulative energy sensors with recorder consumption
statistics (`sum`) and compatible energy units, such as Wh, kWh, MWh and MJ. The recorder must
record both the sources and the group. External statistics without a corresponding live entity
are not supported.

Powercalc uses its normal custom group selection, including additional energy entities, member sensors,
devices, areas and floors. Supported configured subgroups are expanded to their underlying sources.
Repeated sources are counted once, including a subgroup selected alongside one of its sources.
Cyclic references, missing subgroups, unsupported group types and groups whose members cannot
be resolved are rejected. Power-only groups cannot use this option.

### Domain groups

A specific domain, such as `light`, uses the existing Powercalc energy counters belonging to
the source entities in that domain. Individual devices without an energy counter have no energy
history to contribute; enable their energy sensor first if they should be included.
The `all` domain uses enabled energy sensors from the entity registry. Group outputs are excluded
to avoid recursion and counting their members again. Both variants respect **Exclude entities**;
select energy entity IDs when excluding energy sources. Check exclusions carefully when the
installation contains both a whole-house meter and its submeters: different entity IDs can still
describe the same physical consumption.

The domain is resolved again on reload for future membership. The original historical source
selection remains frozen and is never imported again.

### Subtract groups

The history energy sensor calculates **base consumption minus the selected partial consumptions**.
The power sensor continues to subtract the selected power readings. Since enabling history uses
existing energy counters for both historical and live consumption, it does not integrate the
power difference to obtain the group's energy.

Each power source needs its matching existing cumulative energy counter. Powercalc can use its
own explicit power/energy associations automatically, including configured group counterparts.
For external sensors or ambiguous associations, select **Base energy counter** and
**Energy counters to subtract**. The latter must contain one counter for each subtracted power
source. Names or device membership are never used to guess the association.

For example, a 0.5 kWh base increase minus a 0.2 kWh partial increase contributes 0.3 kWh. Signed
corrections and negative net consumption are retained. Shared sources in the base and a subtracted
group cancel algebraically; complete cancellation produces a known zero curve. Separate subtracted
groups must be disjoint. Ambiguous overlaps or an expression that would count a source more than
once are rejected. Selecting subgroups with conflicting signs in an additive custom group is
also rejected.

A source being `unavailable` does not by itself prevent an import. Reliable recent recorder
references must still exist. Every selected source needs suitable consumption statistics and a
recent five-minute reference containing both `sum` and `state`. If a source stopped recording
long ago, lacks consumption statistics, or the transition history has already been purged, the
import fails rather than guessing the missing consumption. Restore recording and reload the
group once recent statistics are available.

Before creating a journal or writing the target, the import also checks that the last completed
hour agrees with the five-minute reference. If recorder has not finished its hourly rollup yet,
reload after it finishes. This failure leaves no partial target import or advanced import cursor.

This option imports energy statistics only. It does not recreate raw-state history, power
statistics, historical costs or historical utility-meter cycles. Existing sources are never
modified. Resetting or calibrating a group with imported history is not supported.

## Consumption and missing data

The import uses differences between adjacent hourly `sum` values on a UTC grid. It never adds
device counter readings to obtain historical consumption. For example, source increases of
0.3 kWh and 0.2 kWh in an hour contribute 0.5 kWh to the group, even if their counters started
at completely different values. Home Assistant has already handled device resets in `sum`;
negative statistical corrections remain signed changes and are not treated as new resets.

The first sample establishes a consumption reference with a statistics `sum` of zero. Its preceding
consumption cannot be inferred. Later sources likewise establish their own reference before
contributing. This historical consumption total is distinct from the live counter reading:
the initial live reading uses the source counter values even if their recorded history is incomplete.
For example, source readings of 300 kWh and 900 kWh produce an initial live counter of 1200 kWh,
while an imported 1 kWh of known consumption remains 1 kWh in the statistics `sum`.

All historical counter readings use the same constant offset as the live counter:
`state = sum + native_offset`. For multiple sources, the hourly group consumption is
the sum of their adjacent `sum` differences after unit conversion. It is accumulated
chronologically, then the counter offset is applied to the entire imported curve.
This avoids a step when Home Assistant's history chart joins hourly statistics to recent
sensor states. For example, a flat known-consumption total of 4.108 kWh and a live counter
of 4.1203 kWh require a 0.0123 kWh offset on every historical counter reading.
This offset adds no consumption to the energy dashboard. Historical counter readings
are the reconstructed group curve; raw source counter histories are not recreated.

The gap policy is deliberately conservative:

- A source contributes only when both adjacent hourly sums exist.
- A difference spanning missing hours is omitted. It is neither assigned to the first returning
  hour nor divided across the gap.
- Other sources with a known adjacent difference can still contribute in that hour. The result
  is explicitly reported as incomplete.
- An hour with a known zero difference remains a numeric statistic. A later reference hour
  with no known contribution, or an hour with no source samples, has no imported row.
  Unknown hours are not written as zero consumption or as `NULL` sums: a `NULL` reference can
  make Home Assistant calculate a false increase when a selected date range starts after it.

Consequently, incomplete imports can understate historical consumption. The **Repairs** warning and
the group's `history_incomplete_intervals` attribute report the number of incomplete intervals.
Different recording start dates can also produce this warning; it does not mean the import failed.
These gaps do not lower the initial live counter. If a source is currently unavailable, its last
known recorder counter reference is used for initialization; a group without history may instead
omit that unavailable source initially.
Charts may visually connect observations across gaps; such a line does not establish measured
consumption in the missing hours. Local time zones and daylight-saving changes do not alter the
UTC aggregation grid.

If no source provides adjacent hourly samples, there is no reliable consumption curve and the
import fails. A failed import is never reported as a partial success. Sources without suitable
statistics cause the whole import to stop; they are not silently dropped from the group.

## Progress, completion and recovery

The new energy sensor has an `unknown` value and no statistics state class while importing.
This keeps progress attributes visible without recording any consumption. Its attributes show
`history_import_status` (`hourly`, `short_term`, `completed` or `failed`) and
`history_import_rows`. Long histories are read and aggregated in 31-day batches outside the Home Assistant
event loop. A **Repairs** issue explains failures or incomplete history.
Import diagnostics remain visible when the normal extended group attributes are disabled.

Powercalc stores an atomic journal in Home Assistant storage with the frozen source list, actual target
entity/statistic ID, unit, time boundaries, aggregation references and pending batch.
Historical source signs are frozen alongside source IDs. The journal is read back after saving
to detect storage failures before proceeding. Each batch
is journalled before being queued, and checked after the recorder has committed it. Retrying
an interrupted batch checks its existing values before accepting it; unrelated target statistics
are never replaced. A pre-existing statistic with the new target ID is a conflict, even if the
old sensor no longer exists.

Conflict checks use an absolute tolerance in the target unit, independent of the counter's
offset. A large counter must not make a different existing value count as an identical retry.

To resume after a failure, resolve the cause and reload the integration. Keep the original group
members, source signs, entity ID and energy unit during an unfinished import. Do not delete the journal:
already imported rows would then be treated as existing target statistics rather than resumable
work. Failed imports can leave verified historical batches on their own new target; the sensor
keeps an `unknown` value until continuation is safe.

After completion, the historical source list is fixed. Changing group members affects future
consumption only. New members establish a current baseline, without adding their earlier
consumption. Changing whether a source is added or subtracted likewise establishes a fresh live
baseline for that source. Entity ID and native energy unit changes for these groups are currently rejected;
they require a coordinated statistics migration which this feature does not implement.
Later corrections to source statistics are not automatically synchronized into completed imports.

## Transition to live readings

Historical hours end at a frozen UTC hour boundary. Powercalc then imports the available
five-minute consumption bridge up to a completed recorder interval. This bridge must be complete;
missing short-term source data cannot be approximated for the live handoff.

Before writing the short-term bridge, Powercalc captures source counter readings and replays
the remaining open interval, including resets. It freezes a native counter offset so that the
initial live value matches the source counter total without adding that offset to consumption.
This offset is saved before importing the short-term reference, and survives interrupted startup.

At the boundary, the target's short-term `sum` equals the aggregated consumption total;
its `state` includes the native counter offset. Before publishing the first live reading,
Powercalc also aligns the already imported hourly states to this same offset in bounded batches.
Both the old and new row values are journalled before a state-only correction is queued.
Recorder verifies that existing rows match the old values or an already committed retry;
the consumption `sum` is preserved. The bridge establishes the live counter reference
without changing the imported consumption sums. Recorder's own import code refreshes its
short-term cache. The source counter references come from the same completed interval.
Existing raw source history is read for the remaining
open interval, while a listener buffers readings arriving during that read. These states are
used to continue the counter; they are not imported as historical raw group states.
Current member states are captured before awaiting that history read. This includes a reading
exactly at the query's exclusive end timestamp and preserves the order of later buffered readings,
including intermediate increments and resets during a reload.

The group starts normal recorder collection only after these references and its native total
are stored together. Live checkpoints likewise save the total and source references together
before publishing. On reload, source history since the checkpoint is replayed and already
processed readings are ignored. No historical import is started again.

Completed imports retain their original live counter origin on reload, since rebasing an already
running counter would introduce false consumption. Groups created by the previous implementation
with a stored native offset automatically receive the same one-time correction to their hourly
counter readings on reload. Their live reading, consumption sums, and source statistics remain
unchanged. Source history is not imported again. Already aligned batches are not shifted twice;
an interruption resumes from the persisted batch. Unexpected target values cause an error
instead of being overwritten. Groups created before native offsets were supported retain their
zero-based counter origin; creating a new group is necessary only to change that older live origin.

A separate persisted marker confirms that the first live group value was published and its
recorder queue was committed. If startup
is interrupted after capturing the source counters but before publication, recovery continues
the five-minute bridge to the current interval instead of shifting that consumption into a later
interval. Completed journals from the earlier storage format remain completed. Startup and live
callbacks use a single live writer so an older checkpoint cannot overwrite a newer one.

The native counter retains the precision of the imported sums to avoid a rounding difference
at the handoff. The configured precision remains the suggested display precision.

Live `total` sources retain signed corrections and explicit `last_reset` changes. For
`total_increasing` sources, the reset threshold follows Home Assistant's recorder semantics.
If recorder history needed for recovery is no longer available, the group stops with an error
instead of inventing a continuation.

## Implementation and compatibility

This change is based on `master` at `dc815072d`. The relevant group implementation, configuration
flow and minimum Home Assistant version were unchanged from Powercalc v1.27.0. This feature is
new; disabling it keeps the existing group implementation and configuration behavior.

The repository minimum is Home Assistant **2026.4.0** and its locked test version is **2026.10.4**.
Recorder metadata explicitly supplies `source="recorder"`, the actual entity ID as `statistic_id`,
`mean_type=StatisticMeanType.NONE`, `unit_class="energy"` and `has_sum=True`.
See the [Home Assistant statistics API change](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/).

Reads use `statistics_during_period`, metadata and last-statistics APIs. Writes use the core
`import_statistics(instance, metadata, statistics, table)` API inside recorder queue tasks,
including `StatisticsShortTerm` for the transition. Validation, import and read-back verification
are serialized with other recorder writers. Source reads are preceded by an explicit
`SynchronizeTask` when pending recorder writes matter; checking whether the queue is empty is
insufficient while a worker is processing a popped job. There are no direct SQL writes or
Home Assistant core modifications.

The recorder tests exercise the real SQLite recorder, five-minute compilation, hourly rollup,
unit conversion, interruption after commit, a new Home Assistant instance using the same
database, and member replacement after completion. They run against HA 2026.4.0 and 2026.10.4.
The 2026.4.0 test environment uses `lru-dict==1.4.1` in place of its pinned 1.3.0 because the
available Python 3.14 environment has no C compiler for that older package. Repository runtime
dependencies are unchanged. MySQL and PostgreSQL were not exercised.
