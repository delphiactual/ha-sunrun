"""Minimal async client for Sunrun's customer portal API. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import aiohttp

GATEWAY = "https://gateway.sunrun.com"
PERFORMANCE = f"{GATEWAY}/performance-api/v1"
# Minute requests fail with HTTP 500 somewhere above 68 days; stay well under it.
MAX_MINUTE_DAYS = 31


class SunrunError(Exception):
    """Base error for the Sunrun API."""


class SunrunAuthError(SunrunError):
    """The token was rejected."""


class SunrunConnectionError(SunrunError):
    """The API could not be reached or returned an unexpected response."""


@dataclass(frozen=True, slots=True)
class Site:
    site_id: str
    name: str
    timezone: str | None


def parse_sites(user: dict[str, Any]) -> list[Site]:
    sites = []
    for opp in user.get("opportunitiesWithContracts") or []:
        site_id = opp.get("prospect_id")
        if not site_id:
            continue
        street, city = opp.get("facility_address_line1"), opp.get("facility_address_city")
        name = ", ".join(p for p in (street, city) if p) or f"Site {site_id}"
        sites.append(Site(str(site_id), name, opp.get("timeZone")))
    return sites


def _rows(body: Any, path: str) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "items", "results", "rows"):
            if isinstance(body.get(key), list):
                return body[key]
    raise SunrunConnectionError(f"Unexpected response shape from {path}")


def parse_timestamp(value: str) -> datetime | date:
    """Daily rows carry a bare date, minute rows an offset-aware datetime."""
    if len(value) == 10:
        return date.fromisoformat(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SunrunClient:
    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    async def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        try:
            async with self._session.get(
                url,
                params=params,
                headers={"Authorization": self._token},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status in (401, 403):
                    raise SunrunAuthError(f"HTTP {resp.status}")
                if resp.status >= 400:
                    raise SunrunConnectionError(f"HTTP {resp.status} from {url}")
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SunrunConnectionError(str(err)) from err

    async def get_sites(self) -> list[Site]:
        return parse_sites(await self._get(f"{GATEWAY}/portal-auth/get-user"))

    async def _production(
        self, kind: str, site_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        path = f"site-production-{kind}/{site_id}"
        params = {"startDate": start.isoformat(), "endDate": end.isoformat()}
        return _rows(await self._get(f"{PERFORMANCE}/{path}", params), path)

    async def get_daily(self, site_id: str, start: datetime, end: datetime) -> dict[date, float]:
        rows = await self._production("daily", site_id, start, end)
        return {
            parse_timestamp(r["timestamp"]): r["pvSolar"]
            for r in rows
            if r.get("pvSolar") is not None
        }

    async def get_minute(
        self, site_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Return (interval start, kWh) rows, chunking long ranges under the API limit."""
        # The API treats endDate as inclusive of that whole day, so a chunk ending at
        # midnight would return the next chunk's first day again and double-count it.
        out: dict[datetime, float] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=MAX_MINUTE_DAYS), end)
            last_instant = chunk_end - timedelta(milliseconds=1)
            for r in await self._production("minute", site_id, cursor, last_instant):
                if r.get("pvSolar") is not None:
                    out[parse_timestamp(r["timestamp"])] = r["pvSolar"]
            cursor = chunk_end
        return sorted(out.items())


def day_start(d: date, tz: tzinfo) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=tz)
