"""Button entities: recall a device preset."""

from __future__ import annotations

import re

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TesiraConfigEntry
from .coordinator import TesiraForte
from .entity import TesiraEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TesiraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(TesiraPresetButton(coordinator, p) for p in coordinator.presets)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class TesiraPresetButton(TesiraEntity, ButtonEntity):
    """Sends DEVICE recallPreset <id> (or recallPresetByName "<name>")."""

    _attr_icon = "mdi:playlist-play"

    def __init__(self, coordinator: TesiraForte, preset: dict) -> None:
        super().__init__(coordinator)
        self._preset = preset
        pid, name = preset.get("id"), preset.get("name")
        ident = str(pid) if pid is not None else f"name_{_slug(name)}"
        self._attr_unique_id = f"{coordinator.serial}_preset_{ident}"
        self._attr_name = f"Recall {name or f'preset {pid}'}"

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_recall_preset(self._preset)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Preset recall failed: {err}") from err
