"""Payments / class-fund coordinator for Bakalari API."""

from __future__ import annotations

from datetime import timedelta
import logging
from random import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BakalariClient
from .children import ChildrenIndex
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_SCAN_INTERVAL_PAYMENTS = "scan_interval_payments"
PAYMENTS_DEFAULT_SCAN_INTERVAL = 6 * 60 * 60  # 6h
PAYMENTS_RECENT_LIMIT = 20


class BakalariPaymentsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator fetching class-fund balance and recent payments per child."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        children: ChildrenIndex,
        clients: dict[str, BakalariClient],
    ) -> None:
        """Initialize the payments coordinator."""
        self.hass = hass
        self.entry = entry
        self.children_index: ChildrenIndex = children
        self._clients: dict[str, BakalariClient] = clients

        base = int(
            entry.options.get(
                CONF_SCAN_INTERVAL_PAYMENTS, PAYMENTS_DEFAULT_SCAN_INTERVAL
            )
            or DEFAULT_SCAN_INTERVAL
        )
        jittered = int(base * (0.9 + 0.2 * random()))

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"{DOMAIN} payments coordinator ({entry.entry_id})",
            update_interval=timedelta(seconds=jittered),
        )

        _LOGGER.debug(
            "[class=%s module=%s] Payments coordinator initialized (%d children, %ss)",
            self.__class__.__name__,
            __name__,
            len(self.children_index.children),
            jittered,
        )

    def get_client(self, child_key: str) -> BakalariClient | None:
        """Return a client for a given child."""
        client = self._clients.get(child_key)
        if not client:
            _LOGGER.error(
                "[class=%s module=%s] Failed to get client for child %s",
                self.__class__.__name__,
                __name__,
                child_key,
                stacklevel=2,
            )
            return None
        return client

    def select_summary(self, child_key: str | None) -> dict[str, Any] | None:
        """Return cached payments summary for a child."""
        return (self.data or {}).get("summary_by_child", {}).get(child_key)

    def select_payments(
        self, child_key: str | None, limit: int | None = None
    ) -> list[Any]:
        """Return cached recent payment items for a child."""
        items: list[Any] = (self.data or {}).get("payments_by_child", {}).get(
            child_key, []
        ) or []
        if limit is not None and limit >= 0:
            return items[:limit]
        return items

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch payments summary and recent movements for each child."""
        try:
            summary_by_child: dict[str, Any] = {}
            payments_by_child: dict[str, list[Any]] = {}

            for child in self.children_index.children:
                summary, payments = await self._fetch_child(child.key)
                summary_by_child[child.key] = summary
                payments_by_child[child.key] = payments
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(str(err)) from err
        else:
            return {
                "summary_by_child": summary_by_child,
                "payments_by_child": payments_by_child,
                "last_sync_ok": True,
            }

    async def _fetch_child(
        self, child_key: str
    ) -> tuple[dict[str, Any] | None, list[Any]]:
        """Fetch summary + recent payments for one child."""
        client = self.get_client(child_key)
        if client is None:
            return None, []

        summary = await client.async_get_payments_summary()
        if summary is not None and not isinstance(summary, dict):
            summary = None

        payments = await client.async_get_payments_list()
        if not isinstance(payments, list):
            payments = []
        return summary, payments[:PAYMENTS_RECENT_LIMIT]
