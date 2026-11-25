"""Coordinator-based homeworks sensor for Bakalari."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity

from .children import Child
from .entity import BakalariEntity


def _get_homeworks_for_child(coord: Any, child_key: str) -> list[Any]:
    """Get homeworks list for the child from coordinator data."""
    data = coord.data or {}
    by_child = data.get("homeworks_by_child") or {}
    items: list[Any] = by_child.get(child_key, []) or []
    return items


class BakalariHomeworksSensor(BakalariEntity, SensorEntity):
    """Sensor showing homeworks for a specific child."""

    _attr_icon = "mdi:notebook-edit-outline"
    _attr_has_entity_name = True

    def __init__(self, coordinator: Any, child: Child) -> None:
        """Initialize the sensor for a specific child."""
        super().__init__(coordinator, child)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{child.key}:homeworks"
        self._attr_name = f"Úkoly - {child.short_name}"

    async def async_added_to_hass(self) -> None:
        """Register coordinator listener."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def native_value(self) -> int:
        """Return number of open (not closed) homeworks."""
        items = _get_homeworks_for_child(self.coordinator, self.child.key)
        return sum(1 for hw in items if isinstance(hw, dict) and not hw.get("closed"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return homeworks as attributes."""
        items = _get_homeworks_for_child(self.coordinator, self.child.key)
        open_items = [
            hw for hw in items if isinstance(hw, dict) and not hw.get("closed")
        ]
        unfinished = [
            hw for hw in open_items if not hw.get("finished")
        ]
        return {
            "child_key": self.child.key,
            "homeworks": items,
            "open_count": len(open_items),
            "unfinished_count": len(unfinished),
            "total_homeworks_cached": len(items) if isinstance(items, list) else 0,
        }
