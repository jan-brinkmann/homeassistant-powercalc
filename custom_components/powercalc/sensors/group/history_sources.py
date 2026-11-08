"""Resolve cumulative sources and their signs without counting a source twice."""

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DOMAIN, CONF_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from custom_components.powercalc.const import (
    ATTR_IS_GROUP,
    CONF_ALL,
    CONF_EXCLUDE_ENTITIES,
    CONF_FORCE_CALCULATE_GROUP_ENERGY,
    CONF_GROUP_ENERGY_BASE_ENTITY,
    CONF_GROUP_ENERGY_ENTITIES,
    CONF_GROUP_ENERGY_SUBTRACT_ENTITIES,
    CONF_GROUP_MEMBER_SENSORS,
    CONF_GROUP_TYPE,
    CONF_SENSOR_TYPE,
    CONF_SUB_GROUPS,
    CONF_SUBTRACT_ENTITIES,
    DATA_CONFIGURED_ENTITIES,
    DATA_DOMAIN_ENTITIES,
    DATA_GROUP_ENTITIES,
    DOMAIN,
    ENTRY_DATA_ENERGY_ENTITY,
    ENTRY_DATA_POWER_ENTITY,
    GroupType,
    SensorType,
)

from .custom import filter_entity_list_by_class, resolve_entity_ids_recursively
from .history import HistoryImportError

SUPPORTED_HISTORY_GROUPS = [GroupType.CUSTOM, GroupType.DOMAIN, GroupType.SUBTRACT]


async def resolve_history_sources(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    return set(await resolve_history_source_weights(hass, dict(entry.data), entry))


async def resolve_history_source_weights(
    hass: HomeAssistant, config: ConfigType, entry: ConfigEntry | None = None
) -> dict[str, int]:
    resolver = HistorySourceResolver(hass)
    resolved = await resolver.resolve_group(config, entry)
    if not resolved:
        raise HistoryImportError("No cumulative energy sources remain after resolving the group")
    nonzero = {source: weight for source, weight in resolved.items() if weight}
    # A completely cancelled expression is a known zero curve. Keep its source
    # grid as a reference; otherwise cancelled sources need no statistics.
    return nonzero or resolved


class HistorySourceResolver:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.energy_groups = {
            group.data[ENTRY_DATA_ENERGY_ENTITY]: group
            for group in hass.config_entries.async_entries(DOMAIN)
            if CONF_GROUP_TYPE in group.data and ENTRY_DATA_ENERGY_ENTITY in group.data
        }
        self.resolved_groups: dict[str, dict[str, int]] = {}
        self.active_groups: set[str] = set()
        self.runtime_groups = set(hass.data.get(DOMAIN, {}).get(DATA_GROUP_ENTITIES, {}))

    async def resolve_group(self, config: ConfigType, entry: ConfigEntry | None = None) -> dict[str, int]:
        group_key = entry.entry_id if entry else "root"
        title = str(config.get(CONF_NAME, group_key))
        if group_key in self.active_groups:
            raise HistoryImportError(f"Cyclic group reference: {title}")
        if group_key in self.resolved_groups:
            return self.resolved_groups[group_key]
        group_type = GroupType(config.get(CONF_GROUP_TYPE, GroupType.CUSTOM))
        if group_type not in SUPPORTED_HISTORY_GROUPS or config.get(CONF_FORCE_CALCULATE_GROUP_ENERGY):
            raise HistoryImportError(f"History requires a supported group with cumulative energy sources: {title}")
        self.active_groups.add(group_key)
        if group_type == GroupType.DOMAIN:
            result = await self.resolve_entities(self.resolve_domain(config))
        elif group_type == GroupType.SUBTRACT:
            result = await self.resolve_subtract(config)
        else:
            result = await self.resolve_custom(config, entry)
        self.active_groups.remove(group_key)
        self.resolved_groups[group_key] = result
        return result

    async def resolve_custom(self, config: ConfigType, entry: ConfigEntry | None) -> dict[str, int]:
        result: dict[str, int] = {}
        for member_id in config.get(CONF_GROUP_MEMBER_SENSORS) or []:
            member = self.hass.config_entries.async_get_entry(member_id)
            if member is None or not member.data.get(ENTRY_DATA_ENERGY_ENTITY):
                raise HistoryImportError(f"Group member has no existing energy sensor: {member_id}")
        for subgroup_id in config.get(CONF_SUB_GROUPS) or []:
            subgroup = self.hass.config_entries.async_get_entry(subgroup_id)
            if subgroup is None:
                raise HistoryImportError(f"Subgroup not found: {subgroup_id}")
            merge_unique_sources(result, await self.resolve_group(dict(subgroup.data), subgroup))
        sources = (
            await resolve_entity_ids_recursively(
                self.hass, entry, SensorDeviceClass.ENERGY, seen_entry_ids=set(config.get(CONF_SUB_GROUPS) or [])
            )
            if entry
            else set(config.get(CONF_GROUP_ENERGY_ENTITIES) or [])
        )
        merge_unique_sources(result, await self.resolve_entities(sources))
        return result

    async def resolve_entities(self, sources: set[str], *, reject_overlap: bool = False) -> dict[str, int]:
        result: dict[str, int] = {}
        for source in sorted(sources):
            if group := self.energy_groups.get(source):
                resolved = await self.resolve_group(dict(group.data), group)
            else:
                state = self.hass.states.get(source)
                if source in self.runtime_groups or (state and state.attributes.get(ATTR_IS_GROUP)):
                    raise HistoryImportError(f"Cannot resolve the members of group {source}; use configured groups")
                resolved = {source: 1}
            if reject_overlap and any(result.get(entity) and weight for entity, weight in resolved.items()):
                raise HistoryImportError("Subtracted groups overlap; select disjoint energy sources")
            merge_unique_sources(result, resolved)
        return result

    def resolve_domain(self, config: ConfigType) -> set[str]:
        domain = config.get(CONF_DOMAIN)
        if domain == CONF_ALL:
            registry = er.async_get(self.hass)
            sources: set[str] = set()
            for entity in registry.entities.values():
                device_class = entity.device_class or entity.original_device_class
                if (
                    device_class != SensorDeviceClass.ENERGY
                    or entity.disabled_by is not None
                    or entity.entity_id in self.energy_groups
                    or entity.entity_id in self.runtime_groups
                    or entity.platform == "group"
                ):
                    continue
                state = self.hass.states.get(entity.entity_id)
                if state and state.attributes.get(ATTR_IS_GROUP):
                    continue
                sources.add(entity.entity_id)
        else:
            entities = self.hass.data.get(DOMAIN, {}).get(DATA_DOMAIN_ENTITIES, {}).get(domain, [])
            sources = filter_entity_list_by_class(entities, SensorDeviceClass.ENERGY)
            # Persisted source IDs also work while integrations are still being
            # set up. The live entity is checked when capturing the handoff.
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if CONF_GROUP_TYPE in entry.data:
                    continue
                source = entry.data.get(CONF_ENTITY_ID) or ""
                if source.startswith(f"{domain}.") and (energy := entry.data.get(ENTRY_DATA_ENERGY_ENTITY)):
                    sources.add(energy)
        return sources - set(config.get(CONF_EXCLUDE_ENTITIES) or [])

    async def resolve_subtract(self, config: ConfigType) -> dict[str, int]:
        base = config.get(CONF_GROUP_ENERGY_BASE_ENTITY) or find_energy_counter(self.hass, config[CONF_ENTITY_ID])
        subtract_power = set(config.get(CONF_SUBTRACT_ENTITIES) or [])
        subtract_energy = config.get(CONF_GROUP_ENERGY_SUBTRACT_ENTITIES) or None
        if subtract_energy is None:
            subtract_energy = [find_energy_counter(self.hass, source) for source in sorted(subtract_power)]
        if len(set(subtract_energy)) != len(subtract_power):
            raise HistoryImportError("Select one cumulative energy counter for every subtracted power source")
        positive = await self.resolve_entities({base})
        negative = await self.resolve_entities(set(subtract_energy), reject_overlap=True)
        result = dict(positive)
        for source, weight in negative.items():
            result[source] = result.get(source, 0) - weight
        if any(abs(weight) > 1 for weight in result.values()):
            raise HistoryImportError("Overlapping subtracted groups would count an energy source more than once")
        return result


def merge_unique_sources(target: dict[str, int], sources: dict[str, int]) -> None:
    for source, weight in sources.items():
        if weight == 0:
            target.setdefault(source, 0)
            continue
        if source in target and target[source] not in [0, weight]:
            raise HistoryImportError(f"Overlapping groups use conflicting signs for {source}")
        target[source] = weight


def find_energy_counter(hass: HomeAssistant, power_entity_id: str) -> str:
    """Use explicit Powercalc associations; never guess from entity names or devices."""
    candidates: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        is_power_entity = entry.data.get(ENTRY_DATA_POWER_ENTITY) == power_entity_id
        is_real_power = (
            entry.data.get(CONF_SENSOR_TYPE) == SensorType.REAL_POWER
            and entry.data.get(CONF_ENTITY_ID) == power_entity_id
        )
        if not is_power_entity and not is_real_power:
            continue
        if energy := entry.data.get(ENTRY_DATA_ENERGY_ENTITY):
            candidates.add(energy)
    configured = hass.data.get(DOMAIN, {}).get(DATA_CONFIGURED_ENTITIES, {})
    for source, mapped in configured.items():
        entities = [entity for entity, _ in mapped]
        power_sources = filter_entity_list_by_class(entities, SensorDeviceClass.POWER)
        if source == power_entity_id or power_entity_id in power_sources:
            candidates.update(filter_entity_list_by_class(entities, SensorDeviceClass.ENERGY))
    if len(candidates) != 1:
        raise HistoryImportError(
            f"No unique existing energy counter for {power_entity_id}; select its energy counter explicitly"
        )
    return next(iter(candidates))
