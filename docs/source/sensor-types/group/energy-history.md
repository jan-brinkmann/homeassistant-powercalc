# Importing group energy history

When creating a **Custom**, **Domain based**, or **Subtract** group in the UI,
enable **Import energy long-term statistics from sources once** to initialize
its new energy sensor from existing consumption statistics. The option is disabled
by default and is only offered during creation.

The original group implementation handles all live readings after initialization.
Importing history does not select a different energy calculation, update interval,
rounding rule, outlier filter, restore mechanism, or calibration/reset implementation.

| Group | Original live energy implementation |
| --- | --- |
| Custom group with cumulative energy sources | Adds source counter deltas using `GroupedEnergySensor`. |
| Domain `all` | Uses the original grouped energy counter and its normal member selection. |
| A specific domain, such as `light` | Integrates the domain power sensor using `VirtualEnergySensor`. |
| Subtract group | Integrates the group's power difference using `VirtualEnergySensor`. |

For Custom groups, disable **Start at zero** when importing history. Their initial
counter matches the source counter total, with recorded references available for
temporarily unavailable sources. Domain `all` also receives a counter baseline.
Power-integrating groups start with the imported consumption total and a matching
time reference. These initial values are the intended difference from a newly
created group without historical consumption. Future calculations use the same
code and settings in both cases.

The energy sensor must be enabled. **Force calculate energy** cannot be combined
with history import at creation. Later configuration changes use the ordinary
options flow and never repeat the import.

## Historical sources

The import requires existing cumulative energy sensors with recorder consumption
statistics (`sum`) and compatible energy units, such as Wh, kWh, MWh and MJ.
Recorder must record the sources and the new group. External statistics without
a corresponding live entity are not supported.

For Custom groups, Powercalc uses the original selected energy counters, including
member sensors, additional energy entities, devices, areas, floors, and configured
subgroups. The original subgroup resolution deduplicates member IDs. A group energy
counter selected directly uses its own statistics, origin, and publication timing;
its historical inputs are not expanded into different live sources. Directly
selecting both a group output and its underlying members is rejected for import,
since the original runtime would count them twice. Cycles, unresolved groups,
incompatible group types, and ambiguous signed overlaps are also rejected.

For a specific domain, historical sources are the existing Powercalc energy
counters belonging to that domain's source entities. For `all`, the imported
counters must be the counters selected by the original grouped energy runtime.
Exclusions apply. If that original selection contains no cumulative sources, or
would count overlapping groups twice, a reliable counter handoff cannot be made
and import is rejected. History import never silently broadens live membership.

For Subtract groups, historical consumption is **base consumption minus the selected
partial consumptions**. Each power operand needs its matching existing energy counter
for the import. Powercalc uses its explicit power/energy associations automatically.
For external or ambiguous sources, select **Base energy counter** and one **Energy
counter to subtract** per subtracted power source. Names and device membership are
not used to guess associations. These energy selectors are creation-only inputs;
subsequent live energy comes from the ordinary integration of the power difference.

Shared underlying sources in the base and a subtracted group cancel algebraically.
Complete cancellation gives a known zero historical curve. Separate subtracted groups
must be disjoint. Ambiguous overlaps and expressions counting a source more than once
are rejected. Different entity IDs can still measure the same physical consumption;
check the source selection when combining whole-house meters and submeters.

An `unavailable` source is retained if suitable statistics and recent recorder
references exist. Every source needs an hourly consumption history and a recent
five-minute reference containing both `sum` and `state`. If the latest hourly rollup
has not caught up with the five-minute reference, the import fails before writing
the target. Reload once recorder has completed it. Missing transition references
or purged source history cause an error rather than an estimated continuation.

## Aggregation and missing data

Powercalc converts all sources to the target energy unit and uses a UTC hourly grid.
For each source it subtracts the previous adjacent hourly `sum` from the current one.
These signed differences are added to the cumulative group consumption. For example,
0.3 kWh and 0.2 kWh in the same hour contribute 0.5 kWh, irrespective of the sources'
original counter offsets. Subtracted sources contribute with a negative sign.

Home Assistant has already handled source resets in its consumption sums. Negative
statistical corrections are retained and are not treated as resets or clamped to zero.
The first sample establishes a reference; its preceding consumption is unknown.
Later sources also establish a reference before contributing.

The gap policy is conservative:

- A source contributes only when both adjacent hourly consumption sums exist.
- Consumption spanning missing hours is omitted, without distribution or interpolation.
- Other sources with known adjacent differences can still contribute in that hour.
- A known zero difference remains numeric. An hour with no known contribution after
  the initial reference has no imported row. Unknown hours are not stored as zero or
  `NULL` sums, which can produce false changes in Home Assistant date-range queries.

The **Repairs** warning and `history_incomplete_intervals` attribute identify partial
history. Different source recording start dates can also produce this warning.
It means historical consumption can be understated, not that the import failed.
If no source supplies adjacent hourly samples, the import fails without reporting
partial success. Local time zones and daylight-saving changes do not change the UTC grid.

The displayed historical counter uses `state = sum + native_offset`, with the same
constant counter offset throughout the hourly history, five-minute bridge, and
initial live value. For example, a flat known-consumption total of 4.108 kWh and an
initial grouped counter of 4.1203 kWh require a 0.0123 kWh offset on historical states.
This adds no consumption. It prevents a step when the history chart joins hourly
statistics to recent sensor states. Raw historical group states are not recreated.

## Initialization and handoff

The sensor remains `unknown` without a statistics state class during import.
Its attributes report `history_import_status` (`hourly`, `short_term`, `completed`,
or `failed`), `history_import_rows`, and `history_incomplete_intervals`. Import
attributes remain available with extended group attributes disabled; after handoff,
availability follows the original sensor's rules.

Historical hours stop at a frozen UTC hour boundary. Powercalc then imports a
complete five-minute bridge up to a finished recorder interval. Existing raw energy
source history covers the remaining open interval, including changes at the read's
exclusive end timestamp. These readings prepare the initial value and source
references only. They are not an additional live calculation or raw-history import.

Before publication, Powercalc aligns the hourly counter states and short-term
reference with the initial counter origin. Consumption sums are preserved. The
normal grouped counter receives its initial value and member comparison states in
`PreviousStateStore`. Source decimal values are retained at the handoff so that the
original rounding rules also agree at halfway values. The normal integration sensor receives its initial total and
time reference, so it does not integrate an already imported period again.

Then the original sensor lifecycle registers its normal listeners and timers.
Grouped counters use their existing publication throttling (60 seconds by default).
Power-integrating sensors retain the configured integration method, energy update
interval, precision, and outlier filtering. Reset, calibration, unavailable readings,
member changes, and reloads follow the original implementation in both variants.

After the first publication has been committed by recorder, the journal records
`runtime_handoff_complete`. There is no history-specific live worker or live
checkpoint writer. On later reloads, the factory reads this marker before adding
any entities and starts the original restore path directly, without a deferred
history worker. This preserves the original power/energy listener registration
order as well. Source energy history and import-only counter associations are not resolved again.
The completed sensor no longer requires recorder access merely to restart its runtime.

Groups created with the earlier separate live implementation receive a one-time
handoff of their last total and member references to the original sensor. Their
existing history is retained. Older journals with an unaligned hourly counter origin
are corrected once before handoff. Subsequent reloads never repeat either operation.

## Persistence, failures and recovery

The import journal stores frozen sources and signs, the actual target entity ID,
unit, time boundaries, aggregation references, pending payload, and handoff progress.
An atomic save is read back before proceeding. Long histories and state alignment
are processed in bounded 31-day batches outside the Home Assistant event loop.

Each batch is journalled before it is queued and verified after recorder commits it.
A retry accepts the exact pending values. State-only alignment journals both old and
new values, preserves `sum`, and rejects unexpected target changes. Source statistics
are never modified. A pre-existing statistic with a new group's target ID is a
conflict even if its former entity no longer exists.

To resume a failure, resolve its cause and reload the integration. Keep the original
sources, target ID, and unit during an unfinished import. Do not delete its journal:
verified historical batches would then appear to be unrelated target data. An
interrupted initial capture resumes the five-minute bridge before handing off.

After handoff, the historical source selection stays frozen. Configuration changes
only affect the original live runtime. Source statistic corrections are not
synchronized into completed imports. Normal restore-state and group comparison-state
storage replace the development version's separate live journal; the history journal
must not be used to restore a later live total.

Only energy statistics are imported. Power history, raw group states, costs, and
historical utility-meter cycles are not reconstructed.

## Compatibility

The change is based on `master` at `dc815072d`. The relevant original group code and
minimum Home Assistant version were unchanged from Powercalc v1.27.0. Live calculations
continue to use those original implementations; the new code supplies initialization.

Tests run against Home Assistant **2026.4.0** and **2026.10.4**. Metadata explicitly
supplies `source="recorder"`, the actual entity ID as `statistic_id`,
`mean_type=StatisticMeanType.NONE`, `unit_class="energy"`, and `has_sum=True`.
Reading and writing use recorder APIs and serialized queue tasks, with no direct SQL
writes or Home Assistant core changes. Real SQLite tests cover statistics compilation,
hourly rollup, unit conversion, interruption, restart, and paired groups with and
without history, including their update intervals and power integration.

The 2026.4.0 test environment uses `lru-dict==1.4.1` instead of its pinned 1.3.0 because
the available Python 3.14 environment has no C compiler for that older package.
Repository runtime dependencies are unchanged. MySQL and PostgreSQL were not exercised.
