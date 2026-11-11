# Difference group

A difference group creates a power sensor and an energy sensor from existing sensors on a base device and one or more difference devices:

- Power = base device power − total power of the difference devices.
- Energy = base device energy − total energy of the difference devices.

Both values come from the device sensors. The group does not integrate power to calculate energy.

## Create a group

In **Settings → Devices & services → Add integration → Powercalc**, select **Group → Difference group**. Select the base device, the difference devices, and whether the energy sensor should **Start at zero**.

Each device must have at least one enabled power sensor and one enabled energy sensor. Powercalc uses the same device member selection as standard groups, including existing Powercalc sensors and sensors from other integrations. If a device has multiple eligible sensors of the same kind, their values are combined. Utility meters and Powercalc standby energy sensors are excluded.

Select each device only once. Devices whose resolved sensors overlap cannot be used together.

## Energy totals and history

With **Start at zero** enabled, the energy sensor begins at zero and accumulates the base device's energy increases minus those of the difference devices.

With **Start at zero** disabled, it starts at the current energy difference and inherits the available long-term statistics once, when the sensor is first created. Historical states and consumption sums are subtracted after converting their units. Reloads, restarts, and member changes do not repeat the import.

Subsequent energy updates use the standard group's handling of meter resets: a decreasing member counter contributes no increase for that update and becomes the new baseline. Device resets therefore do not produce jumps from subtracting raw counter totals. The resulting group total may decrease or become negative when the difference devices consume more than the base device.

## YAML configuration

Use device IDs from Home Assistant's device registry:

```yaml
powercalc:
  sensors:
    - create_group: Remaining consumption
      group_type: difference
      base_device: BASE_DEVICE_ID
      difference_devices:
        - FIRST_DEVICE_ID
        - SECOND_DEVICE_ID
      group_energy_start_at_zero: false
```

The group always creates both sensors, including when energy sensor creation is disabled globally. The force-calculate-energy option does not apply to difference groups.
