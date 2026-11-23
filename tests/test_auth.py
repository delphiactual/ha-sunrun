"""Tests for the login helpers and token refresh in the API client."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

# Loaded by path so the package __init__ (which needs Home Assistant) is not imported.
_path = Path(__file__).parent.parent / "custom_components" / "sunrun" / "api.py"
_spec = importlib.util.spec_from_file_location("sunrun_api", _path)
api = importlib.util.module_from_spec(_spec)
sys.modules["sunrun_api"] = api  # dataclasses look the module up by name
_spec.loader.exec_module(api)

SITE_USER = {"opportunitiesWithContracts": [{"prospect_id": "42", "facility_address_city": "Town"}]}


@pytest.mark.parametrize(
    ("raw", "phone"),
    [
        ("7195551234", "+17195551234"),
        ("(719) 555-1234", "+17195551234"),
        ("719-555-1234", "+17195551234"),
        ("1 719 555 1234", "+17195551234"),
        ("+17195551234", "+17195551234"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_normalize_identifier_accepts_common_phone_formats(raw, phone):
    assert api.normalize_identifier(raw) == {"email": "", "phone": phone}


@pytest.mark.parametrize("raw", ["", "12345", "someone@example.com", "abcdefghij", "2195551234567"])
def test_normalize_identifier_rejects_junk(raw):
    with pytest.raises(ValueError):
        api.normalize_identifier(raw)


def test_tokens_round_trip_and_millisecond_expiry():
    body = {"data": {"accessToken": "a", "refreshToken": "r", "accessTokenExp": 1_800_000_000_000}}
    tokens = api.Tokens.from_response(body)
    assert tokens.access_exp == 1_800_000_000.0
    assert tokens.refresh_exp is None
    assert api.Tokens.from_dict(tokens.as_dict()) == tokens
    assert api.Tokens.from_response({"data": {}}) is None


class FakeSunrun:
    """Just enough of the portal API to exercise login and refresh."""

    def __init__(self) -> None:
        self.valid_access = {"old"}
        self.valid_refresh = "refresh-1"
        self.refresh_calls = 0
        self.refresh_attempts = 0
        self.rotate = True
        self.issue_tokens = True
        self.daily_auth: list[str] = []
        self.app = web.Application()
        self.app.router.add_post("/portal-auth/request-passwordless", self.request)
        self.app.router.add_post("/portal-auth/respond-passwordless", self.respond)
        self.app.router.add_get("/portal-auth/get-user", self.get_user)
        self.app.router.add_get("/performance-api/v1/site-production-daily/{site}", self.daily)

    async def request(self, req):
        body = await req.json()
        if body.get("phone") != "+17195551234":
            return web.json_response({"message": "no"}, status=400)
        return web.json_response({"token": "session-1"})

    async def respond(self, req):
        body = await req.json()
        if req.headers.get("Authorization") != "session-1" or body.get("code") != "123456":
            return web.json_response({"message": "Invalid Code"}, status=400)
        data = {"accessToken": "old", "refreshToken": "refresh-1", "accessTokenExp": 4_000_000_000}
        return web.json_response({"data": data, **SITE_USER})

    async def get_user(self, req):
        access = req.headers.get("Authorization", "")
        if req.headers.get("RefreshToken"):
            self.refresh_attempts += 1
        if access in self.valid_access:
            return web.json_response(SITE_USER)
        if req.headers.get("RefreshToken") == self.valid_refresh and self.issue_tokens:
            self.refresh_calls += 1
            new = f"access-{self.refresh_calls}"
            self.valid_access = {new}
            data = {"accessToken": new, "accessTokenExp": 4_000_000_000}
            if self.rotate:
                self.valid_refresh = f"refresh-{self.refresh_calls + 1}"
                data["refreshToken"] = self.valid_refresh
            return web.json_response({"data": data, **SITE_USER})
        return web.json_response({"message": "expired"}, status=401)

    async def daily(self, req):
        self.daily_auth.append(req.headers.get("Authorization", ""))
        if req.headers.get("Authorization") not in self.valid_access:
            return web.json_response({"message": "expired"}, status=401)
        return web.json_response([{"timestamp": "2026-11-22", "pvSolar": 8.5}])


def run(scenario):
    """Run `scenario(fake, session, gateway)` against a local fake API."""

    async def inner():
        fake = FakeSunrun()
        async with TestServer(fake.app) as server, aiohttp.ClientSession() as session:
            return await scenario(fake, session, str(server.make_url("")).rstrip("/"))

    return asyncio.run(inner())


def start_end():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return now - timedelta(days=2), now


def test_login_returns_tokens_and_sites():
    async def scenario(fake, session, gw):
        ident = api.normalize_identifier("719-555-1234")
        token = await api.request_code(session, ident, gateway=gw)
        result = await api.respond_code(session, ident, token, "123456", gateway=gw)
        return token, result

    token, result = run(scenario)
    assert token == "session-1"
    assert result.tokens.access == "old"
    assert result.tokens.refresh == "refresh-1"
    assert [s.site_id for s in result.sites] == ["42"]


def test_wrong_code_and_refused_number_raise_distinct_errors():
    async def scenario(fake, session, gw):
        ident = api.normalize_identifier("719-555-1234")
        with pytest.raises(api.SunrunCodeError):
            await api.respond_code(session, ident, "session-1", "000000", gateway=gw)
        with pytest.raises(api.SunrunLoginError):
            await api.request_code(session, api.normalize_identifier("719-555-9999"), gateway=gw)

    run(scenario)


def test_401_refreshes_retries_and_saves_the_new_tokens():
    saved = []

    async def scenario(fake, session, gw):
        fake.valid_access = {"other"}  # the stored "old" token has expired
        client = api.SunrunClient(session, api.Tokens("old", "refresh-1"), saved.append, gateway=gw)
        return await client.get_daily("42", *start_end()), client

    daily, client = run(scenario)
    assert list(daily.values()) == [8.5]
    assert client.tokens.access == "access-1"
    assert client.tokens.refresh == "refresh-2"
    assert saved == [client.tokens]


def test_refresh_keeps_the_old_refresh_token_when_none_is_rotated():
    async def scenario(fake, session, gw):
        fake.valid_access, fake.rotate = {"other"}, False
        client = api.SunrunClient(session, api.Tokens("old", "refresh-1"), gateway=gw)
        await client.get_daily("42", *start_end())
        return client

    assert run(scenario).tokens.refresh == "refresh-1"


def test_failed_refresh_raises_auth_error():
    async def scenario(fake, session, gw):
        fake.valid_access, fake.issue_tokens = {"other"}, False
        client = api.SunrunClient(session, api.Tokens("old", "refresh-1"), gateway=gw)
        with pytest.raises(api.SunrunAuthError):
            await client.get_daily("42", *start_end())

    run(scenario)


def test_no_refresh_token_means_immediate_auth_error():
    async def scenario(fake, session, gw):
        fake.valid_access = {"other"}
        client = api.SunrunClient(session, api.Tokens("old"), gateway=gw)
        with pytest.raises(api.SunrunAuthError):
            await client.get_daily("42", *start_end())
        assert fake.refresh_calls == 0

    run(scenario)


def test_concurrent_requests_share_one_refresh():
    async def scenario(fake, session, gw):
        fake.valid_access = {"other"}
        client = api.SunrunClient(session, api.Tokens("old", "refresh-1"), gateway=gw)
        await asyncio.gather(*(client.get_daily("42", *start_end()) for _ in range(4)))
        return fake

    assert run(scenario).refresh_calls == 1


def test_unenforced_expiry_is_not_refreshed_on_every_request():
    async def scenario(fake, session, gw):
        fake.issue_tokens = False  # token is still accepted, but past its declared expiry
        stale = api.Tokens("old", "refresh-1", access_exp=time.time() - 10)
        client = api.SunrunClient(session, stale, gateway=gw)
        for _ in range(3):
            await client.get_daily("42", *start_end())
        return fake

    fake = run(scenario)
    assert fake.refresh_attempts == 1
    assert fake.daily_auth == ["old"] * 3


def test_known_expiry_refreshes_before_the_request_is_rejected():
    async def scenario(fake, session, gw):
        fake.valid_access = {"other"}
        expired = api.Tokens("old", "refresh-1", access_exp=time.time() - 10)
        client = api.SunrunClient(session, expired, gateway=gw)
        await client.get_daily("42", *start_end())
        return fake

    fake = run(scenario)
    assert fake.refresh_calls == 1
    assert fake.daily_auth == ["access-1"]  # the expired token was never sent
