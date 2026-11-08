"""Resolve history sources using the normal group resolver, with strict graph checks."""

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.powercalc.const import (
    ATTR_IS_GROUP,
    CONF_FORCE_CALCULATE_GROUP_ENERGY,
    CONF_GROUP_MEMBER_SENSORS,
    CONF_GROUP_TYPE,
    CONF_SUB_GROUPS,
    DOMAIN,
    ENTRY_DATA_ENERGY_ENTITY,
    GroupType,
)

from .custom import resolve_entity_ids_recursively
from .history import HistoryImportError


async def resolve_history_sources(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    energy_groups = {
        group.data[ENTRY_DATA_ENERGY_ENTITY]: group
        for group in hass.config_entries.async_entries(DOMAIN)
        if CONF_GROUP_TYPE in group.data and ENTRY_DATA_ENERGY_ENTITY in group.data
    }
    resolved: set[str] = set()
    visited: set[str] = set()
    active: set[str] = set()

    async def visit(group: ConfigEntry) -> None:
        if group.entry_id in active:
            raise HistoryImportError(f"Cyclic group reference: {group.title}")
        if group.entry_id in visited:
            return
        validate_group_members(hass, group)
        active.add(group.entry_id)
        for subgroup_id in group.data.get(CONF_SUB_GROUPS) or []:
            subgroup = hass.config_entries.async_get_entry(subgroup_id)
            if subgroup is None:
                raise HistoryImportError(f"Subgroup not found: {subgroup_id}")
            await visit(subgroup)
        # The existing resolver handles explicit entities, members, devices and areas.
        sources = await resolve_entity_ids_recursively(hass, group, SensorDeviceClass.ENERGY)
        for source in sources:
            if source in energy_groups:
                await visit(energy_groups[source])
            else:
                if (state := hass.states.get(source)) and state.attributes.get(ATTR_IS_GROUP):
                    raise HistoryImportError(
                        f"Cannot resolve the members of group {source}; use configured custom subgroups"
                    )
                resolved.add(source)
        active.remove(group.entry_id)
        visited.add(group.entry_id)

    await visit(entry)
    if not resolved:
        raise HistoryImportError("No cumulative energy sources were found")
    return resolved


def validate_group_members(hass: HomeAssistant, group: ConfigEntry) -> None:
    if group.data.get(CONF_GROUP_TYPE, GroupType.CUSTOM) != GroupType.CUSTOM or group.data.get(
        CONF_FORCE_CALCULATE_GROUP_ENERGY
    ):
        raise HistoryImportError(f"History requires a custom group with cumulative energy sources: {group.title}")
    for entry_id in group.data.get(CONF_GROUP_MEMBER_SENSORS) or []:
        member = hass.config_entries.async_get_entry(entry_id)
        if member is None or not member.data.get(ENTRY_DATA_ENERGY_ENTITY):
            raise HistoryImportError(f"Group member has no existing energy sensor: {entry_id}")
