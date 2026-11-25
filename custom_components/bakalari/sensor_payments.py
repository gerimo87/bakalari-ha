"""Coordinator-based class-fund / payments sensor for Bakalari."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity

from .children import Child
from .entity import BakalariEntity


class BakalariPaymentsSensor(BakalariEntity, SensorEntity):
    """Sensor showing class-fund balance for a specific child."""

    _attr_icon = "mdi:cash"
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "CZK"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: Any, child: Child) -> None:
        """Initialize the sensor for a specific child."""
        super().__init__(coordinator, child)
        self._attr_unique_id = f"{coordinator.entry.entry_id}:{child.key}:payments"
        self._attr_name = f"Platby - {child.short_name}"

    async def async_added_to_hass(self) -> None:
        """Register coordinator listener."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _summary(self) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        summary = (data.get("summary_by_child") or {}).get(self.child.key)
        return summary if isinstance(summary, dict) else None

    def _recent(self) -> list[Any]:
        data = self.coordinator.data or {}
        items = (data.get("payments_by_child") or {}).get(self.child.key, []) or []
        return items if isinstance(items, list) else []

    @property
    def native_value(self) -> float | None:
        """Return current class-fund balance."""
        summary = self._summary()
        if summary is None:
            return None
        balance = summary.get("balance")
        try:
            return float(balance) if balance is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Unavailable when summary could not be loaded."""
        if not super().available:
            return False
        return self._summary() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return balance details and recent movements."""
        summary = self._summary() or {}
        return {
            "child_key": self.child.key,
            "student_name_with_class": summary.get("student_name_with_class"),
            "balance": summary.get("balance"),
            "spent": summary.get("spent"),
            "recent_payments": self._recent(),
        }
