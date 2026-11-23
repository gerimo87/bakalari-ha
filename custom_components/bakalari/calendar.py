"""Calendar platform for Bakalari - timetable and events per child (coordinator-backed).

Refactored to use:
- Shared ChildrenIndex (single source of truth for children)
- Timetable/events-specific DataUpdateCoordinator (no direct API calls from entities)
- Events are rebuilt whenever the relevant coordinator updates
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .children import Child, ChildrenIndex
from .const import DOMAIN
from .coordinator_events import BakalariEventsCoordinator
from .coordinator_timetable import BakalariTimetableCoordinator
from .entity import BakalariEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bakalari calendar entities from a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coord_tt: BakalariTimetableCoordinator = data["timetable"]
    children_index: ChildrenIndex = data["children"]
    child_list: list[Child] = list(children_index.children)

    entities: list[CalendarEntity] = [
        BakalariTimetableCalendar(coord_tt, child) for child in child_list
    ]

    coord_events: BakalariEventsCoordinator | None = data.get("events")
    if coord_events is not None:
        entities.extend(
            BakalariEventsCalendar(coord_events, child) for child in child_list
        )

    async_add_entities(entities)


class BakalariTimetableCalendar(BakalariEntity, CalendarEntity):
    """Calendar entity representing child's school timetable (coordinator-backed)."""

    _attr_icon = "mdi:calendar-school"
    _attr_translation_key = "timetable"

    def __init__(self, coordinator: BakalariTimetableCoordinator, child: Child) -> None:
        """Initialize the calendar entity."""
        super().__init__(coordinator, child)

        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}:{child.key}:timetable_calendar"
        )
        self._attr_name = f"Rozvrh - {child.short_name}"

        # Internal cache of CalendarEvent objects built from coordinator timetable weeks.
        self._events_cache: list[CalendarEvent] = []
        self._next_event: CalendarEvent | None = None
        self._last_source_version: int = 0  # simple monotonic marker we derive

    # ------------- CalendarEntity API -------------

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event (if any)."""
        return self._next_event

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        # Ensure cache is current with coordinator data
        self._ensure_events_current()

        start = dt_util.as_utc(start_date)
        end = dt_util.as_utc(end_date)

        result: list[CalendarEvent] = []
        for ev in self._events_cache:
            ev_start = _ensure_utc(ev.start)
            ev_end = _ensure_utc(ev.end) if ev.end else ev_start
            # Overlap test (inclusive)
            overlaps = (ev_start < end) and (ev_end > start)
            if overlaps:
                result.append(ev)

        result.sort(key=lambda e: _ensure_utc(e.start))
        return result

    async def async_update(self) -> None:
        """Manual update trigger (rare). Request coordinator refresh."""
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to Home Assistant."""
        await super().async_added_to_hass()
        # Build initial cache from current coordinator data.
        self._rebuild_events()
        self._compute_next_event()

    # ------------- Coordinator callbacks -------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator data changes."""
        self._rebuild_events()
        self._compute_next_event()
        self.async_write_ha_state()

    # ------------- Cache management -------------

    def _ensure_events_current(self) -> None:
        """Verify cached events align with current coordinator timetable snapshot."""
        weeks = self._get_child_weeks()
        version = _weeks_version_marker(weeks)
        if version != self._last_source_version:
            _LOGGER.debug(
                "[class=%s module=%s] Rebuilding events cache for child_key=%s (ver %s -> %s)",
                self.__class__.__name__,
                __name__,
                self.child.key,
                self._last_source_version,
                version,
            )
            self._rebuild_events(weeks=weeks, version=version)

    def _rebuild_events(
        self,
        *,
        weeks: list[Any] | None = None,
        version: int | None = None,
    ) -> None:
        """Rebuild events cache from coordinator weeks."""
        weeks = weeks if weeks is not None else self._get_child_weeks()
        try:
            events: list[CalendarEvent] = []
            for w in weeks:
                events.extend(_convert_week_to_events(w))
            # Deduplicate by (start,end,summary,location)
            dedup: dict[tuple[str, str, str, str], CalendarEvent] = {}
            for ev in events:
                k = (
                    _ensure_utc(ev.start).isoformat(),
                    _ensure_utc(ev.end).isoformat() if ev.end else "",
                    ev.summary or "",
                    ev.location or "",
                )
                # Keep earliest version if duplicates appear
                if k not in dedup:
                    dedup[k] = ev
            final_events = list(dedup.values())
            final_events.sort(key=lambda e: _ensure_utc(e.start))
            self._events_cache = final_events
            self._last_source_version = (
                version if version is not None else _weeks_version_marker(weeks)
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "[class=%s module=%s] Failed rebuilding events for child_key=%s:",
                self.__class__.__name__,
                __name__,
                self.child.key,
            )
            self._events_cache = []

    def _compute_next_event(self) -> None:
        """Compute the next upcoming event from cache."""
        now = dt_util.utcnow()
        upcoming = [ev for ev in self._events_cache if _ensure_utc(ev.start) >= now]
        upcoming.sort(key=lambda e: _ensure_utc(e.start))
        self._next_event = upcoming[0] if upcoming else None

    # ------------- Data extraction helpers -------------

    def _get_child_weeks(self) -> list[Any]:
        """Fetch raw timetable weeks list for the child from timetable coordinator data."""
        data = self.coordinator.data or {}
        weeks = data.get("timetable_by_child", {}).get(self.child.key, [])
        if not isinstance(weeks, list):
            return []
        return weeks


class BakalariEventsCalendar(BakalariEntity, CalendarEntity):
    """Calendar entity representing child's school events / akce."""

    _attr_icon = "mdi:calendar-star"
    _attr_translation_key = "events"

    def __init__(self, coordinator: BakalariEventsCoordinator, child: Child) -> None:
        """Initialize the events calendar entity."""
        super().__init__(coordinator, child)

        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}:{child.key}:events_calendar"
        )
        self._attr_name = f"Akce - {child.short_name}"

        self._events_cache: list[CalendarEvent] = []
        self._next_event: CalendarEvent | None = None
        self._last_source_version: int = 0

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming school event (if any)."""
        return self._next_event

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return school events within a datetime range."""
        self._ensure_events_current()

        start = dt_util.as_utc(start_date)
        end = dt_util.as_utc(end_date)

        result: list[CalendarEvent] = []
        for ev in self._events_cache:
            ev_start = _ensure_utc(ev.start)
            ev_end = _ensure_utc(ev.end) if ev.end else ev_start
            overlaps = (ev_start < end) and (ev_end > start)
            if overlaps:
                result.append(ev)

        result.sort(key=lambda e: _ensure_utc(e.start))
        return result

    async def async_update(self) -> None:
        """Manual update trigger (rare). Request coordinator refresh."""
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to Home Assistant."""
        await super().async_added_to_hass()
        self._rebuild_events()
        self._compute_next_event()

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator data changes."""
        self._rebuild_events()
        self._compute_next_event()
        self.async_write_ha_state()

    def _ensure_events_current(self) -> None:
        """Rebuild cache when coordinator snapshot changes."""
        raw_events = self._get_child_events()
        version = _events_version_marker(raw_events)
        if version != self._last_source_version:
            self._rebuild_events(raw_events=raw_events, version=version)

    def _rebuild_events(
        self,
        *,
        raw_events: list[Any] | None = None,
        version: int | None = None,
    ) -> None:
        """Rebuild CalendarEvent cache from normalized event dicts."""
        raw_events = (
            raw_events if raw_events is not None else self._get_child_events()
        )
        try:
            events: list[CalendarEvent] = []
            for item in raw_events:
                events.extend(_convert_school_event_to_calendar_events(item))

            dedup: dict[tuple[str, str, str, str], CalendarEvent] = {}
            for ev in events:
                k = (
                    _event_start_key(ev.start),
                    _event_start_key(ev.end) if ev.end else "",
                    ev.summary or "",
                    ev.uid or "",
                )
                if k not in dedup:
                    dedup[k] = ev
            final_events = list(dedup.values())
            final_events.sort(key=lambda e: _ensure_utc(e.start))
            self._events_cache = final_events
            self._last_source_version = (
                version
                if version is not None
                else _events_version_marker(raw_events)
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "[class=%s module=%s] Failed rebuilding school events for child_key=%s:",
                self.__class__.__name__,
                __name__,
                self.child.key,
            )
            self._events_cache = []

    def _compute_next_event(self) -> None:
        """Compute the next upcoming event from cache."""
        now = dt_util.utcnow()
        upcoming = [ev for ev in self._events_cache if _ensure_utc(ev.start) >= now]
        upcoming.sort(key=lambda e: _ensure_utc(e.start))
        self._next_event = upcoming[0] if upcoming else None

    def _get_child_events(self) -> list[Any]:
        """Fetch normalized events list for the child from events coordinator data."""
        data = self.coordinator.data or {}
        items = data.get("events_by_child", {}).get(self.child.key, [])
        if not isinstance(items, list):
            return []
        return items


# ---------------- Conversion helpers (adapted from legacy calendar) ----------------


def _convert_week_to_events(raw: Any) -> list[CalendarEvent]:
    """Convert a timetable week object into CalendarEvent list (delegates per atom)."""
    if raw is None:
        return []
    week = raw
    try:
        hours = getattr(week, "hours", {}) or {}
        days = getattr(week, "days", []) or []
    except Exception:  # noqa: BLE001
        _LOGGER.error("[module=%s] Timetable week structure invalid.", __name__)
        return []

    events: list[CalendarEvent] = []
    for day in days:
        date_part = getattr(day, "date", None)
        if date_part is None:
            continue
        day_date = date_part.date() if isinstance(date_part, datetime) else date_part
        atoms = getattr(day, "atoms", []) or []
        for atom in atoms:
            ev = _atom_to_event(day_date, atom, hours, week)
            if ev:
                events.append(ev)

    events.sort(key=lambda e: _ensure_utc(e.start))
    return events


def _atom_to_event(
    day_date: date | datetime,
    atom: Any,
    hours: dict[Any, Any],
    week: Any,
) -> CalendarEvent | None:
    """Build a CalendarEvent for a single timetable atom (reduced complexity)."""
    try:
        hour = _resolve_hour(atom, hours)
        if hour is None:
            return None
        times = _resolve_times(day_date, hour)
        if times is None:
            return None
        start, end = times
        subj, teach, room, groups = _safe_resolve_entities(week, atom)
        summary = _label_subject(subj) or "Hodina"
        description = _build_description(
            teach=teach,
            groups=groups,
            theme=getattr(atom, "theme", None),
            change=getattr(atom, "change", None),
        )
        location = _label_room(room)
        return CalendarEvent(
            start=start,
            end=end or start,
            summary=summary,
            description=description,
            location=location,
        )
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug(
            "[module=%s] Skipping atom -> event due to error: %s",
            __name__,
            e,
        )
        return None


def _resolve_hour(atom: Any, hours: dict[Any, Any]) -> Any | None:
    """Return hour object for atom or None."""
    hour_id = getattr(atom, "hour_id", None)
    if hour_id is None:
        return None
    return hours.get(hour_id)


def _resolve_times(
    day_date: date | datetime, hour: Any
) -> tuple[datetime, datetime] | None:
    """Compute start/end UTC datetimes from hour item."""
    start = _combine_local_utc(day_date, getattr(hour, "begin_time", ""))
    if start is None:
        return None
    end = _combine_local_utc(day_date, getattr(hour, "end_time", ""))
    return (start, end or start)


def _safe_resolve_entities(week: Any, atom: Any) -> tuple[Any, Any, Any, list[Any]]:
    """Resolve (subject, teacher, room, groups) with error shielding."""
    try:
        subj, teach, room, groups = week.resolve(atom)  # type: ignore[attr-defined]
    except Exception:
        return None, None, None, []
    else:
        return subj, teach, room, groups


def _format_change(change: Any) -> str | None:
    """Format change object into a short label."""
    if change is None:
        return None
    ch_type = getattr(change, "change_type", None) or ""
    ch_desc = getattr(change, "description", None) or ""
    ch_time = getattr(change, "time", None)
    if not (ch_type or ch_desc or ch_time):
        return None
    label = f"Změna: {ch_type} | {ch_desc}".strip()
    if ch_time:
        label += f" ({ch_time})"
    return label


def _build_description(
    teach: Any,
    groups: list[Any],
    theme: Any,
    change: Any,
) -> str | None:
    """Compose description string from resolved entities."""
    parts: list[str] = []
    teach_label = _label_teacher(teach)
    if teach_label:
        parts.append(f"Učitel: {teach_label}")
    groups_label = _label_groups(groups)
    if groups_label:
        parts.append(f"Skupina: {groups_label}")
    if theme:
        parts.append(str(theme))
    change_label = _format_change(change)
    if change_label:
        parts.append(change_label)
    return " | ".join(parts) if parts else None


def _convert_school_event_to_calendar_events(item: Any) -> list[CalendarEvent]:
    """Expand one Bakaláři event (with Times[]) into CalendarEvent entries."""
    if not isinstance(item, dict):
        return []

    title = item.get("title") or "Akce"
    description = _build_school_event_description(item)
    location = _label_rooms_list(item.get("rooms") or [])
    event_id = item.get("id") or ""
    times = item.get("times") or []
    if not isinstance(times, list) or not times:
        return []

    results: list[CalendarEvent] = []
    for idx, slot in enumerate(times):
        if not isinstance(slot, dict):
            continue
        converted = _school_time_slot_to_event(
            title=title,
            description=description,
            location=location,
            event_id=event_id,
            slot_index=idx,
            slot=slot,
        )
        if converted is not None:
            results.append(converted)
    return results


def _school_time_slot_to_event(
    *,
    title: str,
    description: str | None,
    location: str | None,
    event_id: str,
    slot_index: int,
    slot: dict[str, Any],
) -> CalendarEvent | None:
    """Convert a single Times entry into a CalendarEvent."""
    start_raw = slot.get("start")
    end_raw = slot.get("end")
    whole_day = bool(slot.get("whole_day"))

    start_dt = _parse_iso_datetime(start_raw)
    end_dt = _parse_iso_datetime(end_raw)
    if start_dt is None:
        return None
    if end_dt is None:
        end_dt = start_dt

    uid = f"{event_id}:{slot_index}" if event_id else None

    if whole_day:
        start_d = start_dt.date()
        # HA all-day end is exclusive
        end_d = end_dt.date() + timedelta(days=1)
        if end_d <= start_d:
            end_d = start_d + timedelta(days=1)
        return CalendarEvent(
            start=start_d,
            end=end_d,
            summary=title,
            description=description,
            location=location,
            uid=uid,
        )

    return CalendarEvent(
        start=dt_util.as_utc(start_dt),
        end=dt_util.as_utc(end_dt),
        summary=title,
        description=description,
        location=location,
        uid=uid,
    )


def _build_school_event_description(item: dict[str, Any]) -> str | None:
    """Compose description for a school event calendar entry."""
    parts: list[str] = []
    event_type = item.get("event_type") or {}
    if isinstance(event_type, dict):
        type_label = event_type.get("name") or event_type.get("abbrev")
        if type_label:
            parts.append(str(type_label))
    description = item.get("description")
    if description:
        parts.append(str(description))
    note = item.get("note")
    if note:
        parts.append(str(note))
    teachers = _label_items_list(item.get("teachers") or [])
    if teachers:
        parts.append(f"Učitel: {teachers}")
    classes = _label_items_list(item.get("classes") or [])
    if classes:
        parts.append(f"Třída: {classes}")
    return " | ".join(parts) if parts else None


def _label_items_list(items: list[Any]) -> str | None:
    """Join abbrev/name from normalized item dicts."""
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("abbrev") or item.get("name") or ""
        if label:
            labels.append(str(label))
    return ", ".join(labels) if labels else None


def _label_rooms_list(items: list[Any]) -> str | None:
    """Join room labels."""
    return _label_items_list(items)


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ISO datetime string (with or without offset) into aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return parsed


# ---------------- Utility functions (mostly unchanged) ----------------


def _combine_local_utc(d: date, hhmm: str) -> datetime | None:
    """Combine a date with HH:MM string and return timezone-aware UTC datetime."""
    try:
        parts = str(hhmm).split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        local = datetime.combine(d, time(hour=h, minute=m))
        local = local.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt_util.as_utc(local)
    except Exception:
        return None


def _label_subject(subj: Any) -> str | None:
    if subj is None:
        return None
    return getattr(subj, "abbrev", None) or getattr(subj, "name", None)


def _label_teacher(teach: Any) -> str | None:
    if teach is None:
        return None
    return getattr(teach, "abbrev", None) or getattr(teach, "name", None)


def _label_room(room: Any) -> str | None:
    if room is None:
        return None
    return getattr(room, "abbrev", None) or getattr(room, "name", None)


def _label_groups(groups: Any) -> str | None:
    try:
        items = [
            getattr(g, "abbrev", None) or getattr(g, "name", None) or ""
            for g in (groups or [])
        ]
        items = [i for i in items if i]
        return ",".join(items) if items else None
    except Exception:
        return None


def _ensure_utc(val: date | datetime) -> datetime:
    """Ensure date/datetime is timezone-aware UTC. Dates are treated as local midnight."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return dt_util.as_utc(val)
        return val.astimezone(dt_util.UTC)
    local = datetime.combine(val, time.min).replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_utc(local)


def _event_start_key(val: date | datetime | None) -> str:
    """Stable string key for calendar event start/end (date or datetime)."""
    if val is None:
        return ""
    if isinstance(val, datetime):
        return _ensure_utc(val).isoformat()
    return val.isoformat()


def _weeks_version_marker(weeks: Iterable[Any]) -> int:
    """Create a lightweight monotonic-ish marker from list of week objects."""
    marker = 0
    for idx, w in enumerate(weeks):
        marker ^= (id(w) & 0xFFFF_FFFF) ^ (idx << 8)
    return marker & 0xFFFF_FFFF


def _events_version_marker(events: Iterable[Any]) -> int:
    """Create a lightweight marker from list of event dicts."""
    marker = 0
    for idx, item in enumerate(events):
        if isinstance(item, dict):
            key = f"{item.get('id', '')}:{item.get('date_changed', '')}:{item.get('title', '')}"
            marker ^= hash(key) & 0xFFFF_FFFF
        else:
            marker ^= (id(item) & 0xFFFF_FFFF) ^ (idx << 8)
    return marker & 0xFFFF_FFFF
