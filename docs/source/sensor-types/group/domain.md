# Domain group

Powercalc makes it easy to create a group sensors for all entities of a given domain with the `create_domain_groups` option, or you can use the GUI, select `Group` -> `Domain group`.
For example let's assume you want group sensors for all your lights and media players you can use the following configuration.

```yaml
powercalc:
  create_domain_groups:
    - light
    - media_player
```

!!! note

    This will only include all virtual power sensors created with powercalc, not any other power sensors already available in your HA installation. This is because Powercalc cannot know the source for any given power sensor.

You can also utilize this option to create a group to sum all energy sensors of your HA installation. Use `all` for that.

```yaml
powercalc:
  create_domain_groups:
    - all
```

## Import existing energy history

When creating a domain group in the UI, you can enable **Import energy long-term statistics from
sources once**. The group imports the consumption statistics of its existing energy sources and
continues from that total. The `all` domain excludes group outputs to prevent duplicate counting.
Exclusions also apply to the historical source selection.

See [Importing group energy history](energy-history.md) for source requirements, missing-data
handling and the transition to live readings. Reloading or changing the domain does not import
history again.
