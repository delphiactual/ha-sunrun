"""Data coordinator for the Sunrun integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import statistics
from .api import SunrunAuthError, SunrunClient, SunrunError, day_start
from .const import CONF_SITE_ID, DOMAIN, RESYNC_DAYS, UPDATE_INTERVAL
from .stats_math import build_rows

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SunrunData:
    today_kwh: float
    last_data: datetime | None


class SunrunCoordinator(DataUpdateCoordinator[SunrunData]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SunrunClient) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self._client = client
        self._site_id = entry.data[CONF_SITE_ID]
        self._meta = statistics.metadata(self._site_id, entry.data[CONF_NAME])
        # Set once the full history is in, so rolling updates never anchor on a partial series.
        self._ready = asyncio.Event()
        self._backfill: asyncio.Task[None] | None = None

    async def _async_update_data(self) -> SunrunData:
        tz = dt_util.get_default_time_zone()
        today = dt_util.now().date()
        first_day = today - timedelta(days=RESYNC_DAYS)
        start = day_start(first_day, tz)
        try:
            slots = await self._client.get_minute(
                self._site_id, start, day_start(today + timedelta(days=1), tz)
            )
            if not self._ready.is_set() and await statistics.async_has_statistics(
                self.hass, self._meta
            ):
                self._ready.set()
            if self._ready.is_set():
                await self._sync_recent(slots, start, first_day, tz)
            else:
                self._ensure_backfill()
        except SunrunAuthError as err:
            raise ConfigEntryAuthFailed from err
        except SunrunError as err:
            raise UpdateFailed(str(err)) from err

        todays = [kwh for ts, kwh in slots if ts.astimezone(tz).date() == today]
        return SunrunData(
            today_kwh=round(sum(todays), 3),
            last_data=max((ts for ts, _ in slots), default=None),
        )

    async def _sync_recent(
        self, slots: list[tuple[datetime, float]], start: datetime, first_day: date, tz: tzinfo
    ) -> None:
        anchor = await statistics.async_anchor_sum(self.hass, self._meta, start)
        if anchor is None:
            # The series was cleared or never reached this far back, so rebuild it.
            self._ready.clear()
            self._ensure_backfill()
            return
        rows = build_rows(slots, {}, tz, minute_from=first_day, base_sum=anchor)
        statistics.async_write(self.hass, self._meta, rows)

    def _ensure_backfill(self) -> None:
        if self._backfill is None or self._backfill.done():
            self._backfill = self.config_entry.async_create_background_task(
                self.hass, self._run_backfill(), f"{DOMAIN} backfill"
            )

    async def _run_backfill(self) -> None:
        try:
            await self._backfill_history()
        except SunrunAuthError:
            self.config_entry.async_start_reauth(self.hass)
        except SunrunError as err:
            # The next scheduled update starts it again.
            _LOGGER.warning("Sunrun history backfill failed, will retry: %s", err)

    async def _backfill_history(self) -> None:
        tz = dt_util.get_default_time_zone()
        end = day_start(dt_util.now().date() + timedelta(days=1), tz)
        daily = await self._client.get_daily(self._site_id, day_start(date(2015, 1, 1), tz), end)
        if not daily:
            return
        install = min(daily)
        minute_from = await self._earliest_minute_day(install, dt_util.now().date(), tz)
        slots = await self._client.get_minute(self._site_id, day_start(minute_from, tz), end)
        rows = build_rows(slots, daily, tz, minute_from=minute_from, baseline=True)
        statistics.async_write(self.hass, self._meta, rows)
        self._ready.set()
        _LOGGER.info("Sunrun history written: %s hourly rows from %s", len(rows), install)

    async def _earliest_minute_day(self, install: date, today: date, tz: tzinfo) -> date:
        """Find where minute data begins, assuming it is contiguous after that point."""

        async def has_data(day: date) -> bool:
            rows = await self._client.get_minute(
                self._site_id, day_start(day, tz), day_start(day + timedelta(days=1), tz)
            )
            return bool(rows)

        if await has_data(install):
            return install
        low, high = install, today
        while (high - low).days > 1:
            mid = low + timedelta(days=(high - low).days // 2)
            if await has_data(mid):
                high = mid
            else:
                low = mid
        return high
