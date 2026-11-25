"""Binary sensors: device fault state and Signal Present meters."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TesiraConfigEntry
from .coordinator import ControlSpec, TesiraForte, is_true
from .entity import TesiraEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TesiraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        TesiraPresenceSensor(coordinator, spec)
        for spec in coordinator.specs
        if spec.platform == "binary_sensor"
    ]
    entities.append(TesiraFaultSensor(coordinator))
    async_add_entities(entities)


class TesiraPresenceSensor(TesiraEntity, BinarySensorEntity):
    """A Signal Present channel, or "any channel present" for the block."""

    def __init__(self, coordinator: TesiraForte, spec: ControlSpec) -> None:
        super().__init__(coordinator, spec)
        if spec.device_class:
            self._attr_device_class = BinarySensorDeviceClass(spec.device_class)

    @property
    def is_on(self) -> bool | None:
        return is_true(self.coordinator.data.get(self.spec.token))


class TesiraFaultSensor(TesiraEntity, BinarySensorEntity):
    """On when the Forte reports one or more active faults."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Fault"

    def __init__(self, coordinator: TesiraForte) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial}_fault"

    @property
    def is_on(self) -> bool:
        return self.coordinator.fault_active
