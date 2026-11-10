# Subtract group

Use this group to subtract the power of one or more entities from another entity.

You select a base entity using the `entity_id` option and then add one or more entities to subtract from the base entity using the `subtract_entities` option.

## Create group with GUI

You can create a new group with the GUI using this button.

[![config_flow_start](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=powercalc)

When this is not working.

- Go to `Settings` -> `Devices & Services`
- Click `Add integration`
- Search and click `Powercalc`

Select `Group` -> `Subtract` and follow the instructions.

### Import existing energy history

Enable **Import energy long-term statistics from sources once** when creating the group to import
the base energy consumption minus the selected partial consumptions. The imported total initializes
the ordinary energy sensor. Live energy then uses the original integration of the group's power
difference, with the same method and settings as a group without imported history.

Existing Powercalc power/energy associations are used automatically. Otherwise select the matching
**Base energy counter** and one **Energy counter to subtract** for each selected power source.
Power history cannot supply missing energy history. Negative net consumption and signed corrections
are preserved in imported statistics; overlapping subtracted groups are rejected. The energy
counter selectors are only needed for creation and do not appear in the ordinary options flow.

See [Importing group energy history](energy-history.md) for the complete requirements and recovery
behavior. Changing the sources later affects future consumption and never starts another history import.

## Create group with YAML

```yaml
powercalc:
  sensors:
    - create_group: Subtract
      group_type: subtract
      entity_id: sensor.a
      subtract_entities:
        - sensor.b
        - sensor.c
```
