from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DOMAIN, CONF_NAME, CONF_UNIQUE_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from custom_components.powercalc.analytics.analytics import collect_analytics
from custom_components.powercalc.const import (
    CONF_GROUP_ENERGY_IMPORT_HISTORY,
    CONF_GROUP_ENERGY_START_AT_ZERO,
    CONF_GROUP_TYPE,
    DATA_GROUP_TYPES,
    DOMAIN,
    GroupType,
)
from custom_components.powercalc.errors import SensorConfigurationError
from custom_components.powercalc.sensors.energy import EnergySensor
import custom_components.powercalc.sensors.group.custom as custom_group
import custom_components.powercalc.sensors.group.standby as standby_group
import custom_components.powercalc.sensors.group.subtract as subtract_group
from custom_components.powercalc.sensors.group.tracked_untracked import TrackedPowerSensorFactory


async def create_group_sensors(
    hass: HomeAssistant,
    sensor_config: ConfigType,
    config_entry: ConfigEntry | None,
    entities: list[Entity] | None = None,
) -> list[Entity]:
    sensors = await _create_group_sensors(hass, sensor_config, config_entry, entities)
    if sensor_config.get(CONF_GROUP_ENERGY_IMPORT_HISTORY):
        for sensor in sensors:
            if isinstance(sensor, EnergySensor) and sensor.group_history is not None:
                await _restore_completed_history(hass, sensor, sensor_config, config_entry)
    return sensors


async def _restore_completed_history(
    hass: HomeAssistant, sensor: EnergySensor, config: ConfigType, entry: ConfigEntry | None
) -> None:
    unique_id = str(sensor.unique_id)
    stored = await Store[dict[str, Any]](hass, 1, f"powercalc_group_history.{unique_id}").async_load()
    if not stored or not stored.get("runtime_handoff_complete"):
        return
    # This runs before any group entities are added. Completed imports must use
    # the ordinary lifecycle immediately, including its listener registration order.
    sensor.group_history = None
    missing = stored.get("aggregation", {}).get("missing_intervals", 0)
    sensor.history_attributes = {
        "history_import_status": "completed",
        "history_import_rows": stored.get("imported_rows", 0),
        "history_incomplete_intervals": missing,
    }
    if entry is not None and config.get(CONF_GROUP_TYPE) != GroupType.CUSTOM:
        config.pop(CONF_GROUP_ENERGY_START_AT_ZERO, None)
    ir.async_delete_issue(hass, DOMAIN, f"group_history_{unique_id}")
    if missing:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"group_history_partial_{unique_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="group_history_partial",
            translation_placeholders={"entity": sensor.entity_id, "intervals": str(missing)},
        )


async def _create_group_sensors(
    hass: HomeAssistant,
    sensor_config: ConfigType,
    config_entry: ConfigEntry | None,
    entities: list[Entity] | None = None,
) -> list[Entity]:
    """Create group sensors for a given sensor configuration."""
    group_type: GroupType = GroupType(sensor_config.get(CONF_GROUP_TYPE, GroupType.CUSTOM))
    collect_analytics(hass, config_entry).inc(DATA_GROUP_TYPES, group_type)

    if group_type == GroupType.DOMAIN:
        domain = sensor_config[CONF_DOMAIN]
        name: str = sensor_config.get(CONF_NAME, f"All {domain}")
        sensor_config.setdefault(CONF_UNIQUE_ID, f"powercalc_domaingroup_{domain}")
        sensor_config[CONF_GROUP_TYPE] = GroupType.DOMAIN
        return custom_group.create_group_sensors_custom(
            hass,
            name,
            sensor_config,
            set(),
            set(),
            force_create=True,
            history_entry=config_entry,
        )
    if group_type == GroupType.STANDBY:
        return standby_group.create_general_standby_sensors(hass, sensor_config)

    if group_type == GroupType.CUSTOM:
        if config_entry:
            return await custom_group.create_group_sensors_gui(
                hass=hass,
                entry=config_entry,
                sensor_config=sensor_config,
            )
        return custom_group.create_group_sensors_yaml(
            hass=hass,
            sensor_config=sensor_config,
            entities=entities or [],
        )

    if group_type == GroupType.SUBTRACT:
        return subtract_group.create_subtract_group_sensors(
            hass=hass,
            config=sensor_config,
            config_entry=config_entry,
        )

    if group_type == GroupType.TRACKED_UNTRACKED and config_entry:
        factory = TrackedPowerSensorFactory(hass, config_entry, sensor_config)
        return await factory.create_tracked_untracked_group_sensors()

    raise SensorConfigurationError(f"Group type {group_type} invalid")  # pragma: no cover
