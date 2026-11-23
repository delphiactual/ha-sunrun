"""Minimal async client for Sunrun's customer portal API. No Home Assistant imports."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import aiohttp

GATEWAY = "https://gateway.sunrun.com"
# Minute requests fail with HTTP 500 somewhere above 68 days; stay well under it.
MAX_MINUTE_DAYS = 31
# Refresh a little early so a request never races the expiry.
EXPIRY_MARGIN = 60
TIMEOUT = aiohttp.ClientTimeout(total=60)


class SunrunError(Exception):
    """Base error for the Sunrun API."""


class SunrunAuthError(SunrunError):
    """The tokens were rejected and could not be refreshed."""


class SunrunConnectionError(SunrunError):
    """The API could not be reached or returned an unexpected response."""


class SunrunLoginError(SunrunError):
    """Sunrun refused to start or finish a login."""


class SunrunCodeError(SunrunLoginError):
    """The login code was wrong or expired."""


@dataclass(frozen=True, slots=True)
class Site:
    site_id: str
    name: str
    timezone: str | None


@dataclass(frozen=True, slots=True)
class Tokens:
    access: str
    refresh: str | None = None
    access_exp: float | None = None
    refresh_exp: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "refresh": self.refresh,
            "access_exp": self.access_exp,
            "refresh_exp": self.refresh_exp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tokens:
        return cls(
            data["access"], data.get("refresh"), data.get("access_exp"), data.get("refresh_exp")
        )

    @classmethod
    def from_response(cls, body: Any) -> Tokens | None:
        """Read the token block Sunrun nests under `data`, or None if there isn't one."""
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("accessToken"):
            return None
        return cls(
            data["accessToken"],
            data.get("refreshToken"),
            _epoch(data.get("accessTokenExp")),
            _epoch(data.get("refreshTokenExp")),
        )


@dataclass(frozen=True, slots=True)
class LoginResult:
    tokens: Tokens
    sites: list[Site]


def _epoch(value: Any) -> float | None:
    """Expiry fields may be seconds or milliseconds since the epoch."""
    if not isinstance(value, int | float):
        return None
    return value / 1000 if value > 10**11 else float(value)


def normalize_identifier(raw: str) -> dict[str, str]:
    """Turn a typed mobile number into the body Sunrun's login expects.

    Only phone login is offered; the email field stays in the body, empty, as the web app sends it.
    """
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return {"email": "", "phone": f"+{digits}"}
    if len(digits) == 10:
        return {"email": "", "phone": f"+1{digits}"}
    if len(digits) == 11 and digits.startswith("1"):
        return {"email": "", "phone": f"+{digits}"}
    raise ValueError("invalid phone number")


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


async def _post(
    session: aiohttp.ClientSession, url: str, body: dict[str, Any], headers: dict[str, str]
) -> tuple[int, Any]:
    try:
        async with session.post(url, json=body, headers=headers, timeout=TIMEOUT) as resp:
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                payload = None
            return resp.status, payload
    except (aiohttp.ClientError, TimeoutError) as err:
        raise SunrunConnectionError(str(err)) from err


async def request_code(
    session: aiohttp.ClientSession, identifier: dict[str, str], *, gateway: str = GATEWAY
) -> str:
    """Ask Sunrun to send a login code; returns the session token the next call needs."""
    status, body = await _post(
        session, f"{gateway}/portal-auth/request-passwordless", identifier, {}
    )
    if 400 <= status < 500:
        raise SunrunLoginError(f"HTTP {status}")
    if status >= 500 or not isinstance(body, dict) or not body.get("token"):
        raise SunrunConnectionError(f"Unexpected login response (HTTP {status})")
    return body["token"]


async def respond_code(
    session: aiohttp.ClientSession,
    identifier: dict[str, str],
    session_token: str,
    code: str,
    *,
    gateway: str = GATEWAY,
) -> LoginResult:
    status, body = await _post(
        session,
        f"{gateway}/portal-auth/respond-passwordless",
        {**identifier, "code": code},
        {"Authorization": session_token},
    )
    if 400 <= status < 500:
        raise SunrunCodeError(f"HTTP {status}")
    tokens = Tokens.from_response(body)
    if status >= 500 or tokens is None:
        raise SunrunConnectionError(f"Unexpected login response (HTTP {status})")
    return LoginResult(tokens, parse_sites(body))


class SunrunClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: Tokens,
        on_tokens: Callable[[Tokens], None] | None = None,
        *,
        gateway: str = GATEWAY,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._on_tokens = on_tokens
        self._gateway = gateway
        self._lock = asyncio.Lock()
        self._not_refreshable: str | None = None

    @property
    def tokens(self) -> Tokens:
        return self._tokens

    async def _fetch(self, url: str, params: dict[str, str] | None, headers: dict[str, str]) -> Any:
        try:
            async with self._session.get(
                url, params=params, headers=headers, timeout=TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise SunrunAuthError(f"HTTP {resp.status}")
                if resp.status >= 400:
                    raise SunrunConnectionError(f"HTTP {resp.status} from {url}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SunrunConnectionError(str(err)) from err

    async def _refresh(self, rejected: str) -> bool:
        """Swap in new tokens. Returns False if Sunrun will not issue any."""
        async with self._lock:
            if self._tokens.access != rejected:
                return True  # another request already refreshed while we waited
            if not self._tokens.refresh:
                return False
            # Mirrors the web app: get-user with the refresh token returns new tokens once
            # the access token has expired, and plain user data before that.
            headers = {"Authorization": rejected, "RefreshToken": self._tokens.refresh}
            try:
                body = await self._fetch(f"{self._gateway}/portal-auth/get-user", None, headers)
            except SunrunAuthError:
                return False
            new = Tokens.from_response(body)
            if new is None or new.access == rejected:
                return False
            self._tokens = Tokens(
                new.access,
                new.refresh or self._tokens.refresh,
                new.access_exp,
                new.refresh_exp or self._tokens.refresh_exp,
            )
            if self._on_tokens:
                self._on_tokens(self._tokens)
            return True

    async def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        expiry = self._tokens.access_exp
        if (
            expiry is not None
            and time.time() >= expiry - EXPIRY_MARGIN
            and self._tokens.access != self._not_refreshable
        ):
            if not await self._refresh(self._tokens.access):
                # The declared expiry may not be enforced, so stop retrying on every request;
                # a real rejection still triggers a refresh below.
                self._not_refreshable = self._tokens.access
        used = self._tokens.access
        try:
            return await self._fetch(url, params, {"Authorization": used})
        except SunrunAuthError:
            if not await self._refresh(used):
                raise
        return await self._fetch(url, params, {"Authorization": self._tokens.access})

    async def get_sites(self) -> list[Site]:
        return parse_sites(await self._get(f"{self._gateway}/portal-auth/get-user"))

    async def _production(
        self, kind: str, site_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        path = f"site-production-{kind}/{site_id}"
        params = {"startDate": start.isoformat(), "endDate": end.isoformat()}
        return _rows(await self._get(f"{self._gateway}/performance-api/v1/{path}", params), path)

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
