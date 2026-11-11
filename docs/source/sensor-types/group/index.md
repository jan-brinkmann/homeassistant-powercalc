# Groups

Powercalc provides several options to group individual power sensors into a single one which sums the total.
This can be very useful to get a glance about the consumption of all your lights together for example.

There are several group types available:

- [Custom group](custom.md)

Create a group with a custom name and add individual power and energy sensors to it.
You can also nest groups to create a hierarchy.

- [Domain group](domain.md)

Create a group for all the power entities of a given domain. For example all lights or media players.

- [Subtract group](subtract.md)

Use this group to subtract the power of one or more entities from another entity.

- [Difference group](difference.md)

Subtract the existing power and energy readings of one or more devices from a base device. Both group sensors use the corresponding member sensors.

- [Tracked/Untracked group](tracked-untracked.md)

This group is used to get a sum of all tracked or untracked power sensors.

- [Standby group](standby.md)

This group is created by default and sums all power when devices are in standby mode.

## Inherit energy history

When creating a custom or difference group, disable **Start at zero** to inherit the current energy total and the available hourly long-term statistics of its members. In YAML, use `group_energy_start_at_zero: false` on the group configuration. The default is `true`.

Custom groups sum the member energy histories. Difference groups take the base device's energy history and subtract the difference devices' histories. For custom groups that calculate energy from power, Powercalc needs a uniquely associated energy sensor for each power sensor. It uses Powercalc's configured associations, integral sensor sources, or an unambiguous energy sensor on the same device. If an association cannot be resolved, the import is skipped and a warning is logged.

Domain, subtract, tracked/untracked, and standby groups do not inherit member energy history.

The import converts energy units and preserves cumulative consumption across historical meter resets. Missing samples carry the member's last known values forward; a member contributes zero before its first available sample. Only available statistics can be inherited, so sensors without recorded history cannot supply historical consumption.

History is imported once when the energy sensor is first created. Reloading, restarting, or changing group members does not import history again. Existing group statistics are preserved. After initialization, the group's existing energy calculation continues as usual.
