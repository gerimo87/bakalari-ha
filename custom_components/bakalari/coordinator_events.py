"""Events (akce) coordinator for Bakalari API with an independent update interval."""

from __future__ import annotations

from datetime import timedelta
import logging
from random import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as ha_dt

from .api import BakalariClient
from .children import ChildrenIndex
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Custom option key for events polling. If not set, falls back to a safe default.
CONF_SCAN_INTERVAL_EVENTS = "scan_interval_events"
# Default to 6 hours for events polling (same cadence as timetable)
EVENTS_DEFAULT_SCAN_INTERVAL = 6 * 60 * 60  # 6h
# Look back window so multi-day events that started recently are still included
EVENTS_LOOKBACK_DAYS = 14


class BakalariEventsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator fetching school events (akce) per child."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        children: ChildrenIndex,
        clients: dict[str, BakalariClient],
    ) -> None:
        """Initialize the events coordinator."""
        self.hass = hass
        self.entry = entry

        self.children_index: ChildrenIndex = children
        self._clients: dict[str, BakalariClient] = clients

        base = int(
            entry.options.get(CONF_SCAN_INTERVAL_EVENTS, EVENTS_DEFAULT_SCAN_INTERVAL)
            or DEFAULT_SCAN_INTERVAL
        )
        jittered = int(base * (0.9 + 0.2 * random()))
        update_interval = timedelta(seconds=jittered)

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"{DOMAIN} events coordinator ({entry.entry_id})",
            update_interval=update_interval,
        )

        _LOGGER.debug(
            "[class=%s module=%s] Events coordinator initialized for entry_id=%s with %d child(ren). Interval=%ss",
            self.__class__.__name__,
            __name__,
            entry.entry_id,
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

    def select_events(
        self, child_key: str | None, limit: int | None = None
    ) -> list[Any]:
        """Return cached events for a child."""
        items: list[Any] = (self.data or {}).get("events_by_child", {}).get(
            child_key, []
        ) or []
        if limit is not None and limit >= 0:
            return items[:limit]
        return items

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch events for each configured child."""
        try:
            events_by_child: dict[str, list[Any]] = {}
            since = ha_dt.now().date() - timedelta(days=EVENTS_LOOKBACK_DAYS)

            for child in self.children_index.children:
                events_by_child[child.key] = await self._fetch_child_events(
                    child.key, since
                )
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(str(err)) from err
        else:
            return {
                "events_by_child": events_by_child,
                "events_since": since.isoformat(),
                "last_sync_ok": True,
            }

    async def _fetch_child_events(self, child_key: str, since: Any) -> list[Any]:
        """Fetch and return school events for a single child."""
        client = self.get_client(child_key)
        if client is None:
            return []

        events = await client.async_get_events(since)
        if not isinstance(events, list):
            return []
        return events
