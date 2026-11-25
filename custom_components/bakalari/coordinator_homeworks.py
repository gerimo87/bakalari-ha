"""Homeworks coordinator for Bakalari API."""

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
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_SCAN_INTERVAL_HOMEWORKS = "scan_interval_homeworks"
HOMEWORKS_DEFAULT_SCAN_INTERVAL = 3600  # 1h
HOMEWORKS_LOOKBACK_DAYS = 14
HOMEWORKS_LOOKAHEAD_DAYS = 30


class BakalariHomeworksCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator fetching homeworks per child."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        children: ChildrenIndex,
        clients: dict[str, BakalariClient],
    ) -> None:
        """Initialize the homeworks coordinator."""
        self.hass = hass
        self.entry = entry
        self.children_index: ChildrenIndex = children
        self._clients: dict[str, BakalariClient] = clients

        base = int(
            entry.options.get(
                CONF_SCAN_INTERVAL_HOMEWORKS, HOMEWORKS_DEFAULT_SCAN_INTERVAL
            )
            or DEFAULT_SCAN_INTERVAL
        )
        jittered = int(base * (0.9 + 0.2 * random()))

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"{DOMAIN} homeworks coordinator ({entry.entry_id})",
            update_interval=timedelta(seconds=jittered),
        )

        _LOGGER.debug(
            "[class=%s module=%s] Homeworks coordinator initialized (%d children, %ss)",
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

    def select_homeworks(
        self, child_key: str | None, limit: int | None = None
    ) -> list[Any]:
        """Return cached homeworks for a child."""
        items: list[Any] = (self.data or {}).get("homeworks_by_child", {}).get(
            child_key, []
        ) or []
        if limit is not None and limit >= 0:
            return items[:limit]
        return items

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch homeworks for each configured child."""
        try:
            today = ha_dt.now().date()
            date_from = today - timedelta(days=HOMEWORKS_LOOKBACK_DAYS)
            date_to = today + timedelta(days=HOMEWORKS_LOOKAHEAD_DAYS)
            homeworks_by_child: dict[str, list[Any]] = {}

            for child in self.children_index.children:
                homeworks_by_child[child.key] = await self._fetch_child(
                    child.key, date_from, date_to
                )
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(str(err)) from err
        else:
            return {
                "homeworks_by_child": homeworks_by_child,
                "homeworks_from": date_from.isoformat(),
                "homeworks_to": date_to.isoformat(),
                "last_sync_ok": True,
            }

    async def _fetch_child(
        self, child_key: str, date_from: Any, date_to: Any
    ) -> list[Any]:
        """Fetch homeworks for one child."""
        client = self.get_client(child_key)
        if client is None:
            return []
        items = await client.async_get_homeworks(date_from, date_to)
        return items if isinstance(items, list) else []
