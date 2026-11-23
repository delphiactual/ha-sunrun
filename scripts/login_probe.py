"""Probe of Sunrun's passwordless login and token refresh, in two steps.

  login_probe.py request [email-or-phone]   sends one real code (SMS or email)
  login_probe.py verify <code>              finishes the login and tests refresh

Prints response shapes and key names only, never token values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from smoke_test import load_env

AUTH = "https://gateway.sunrun.com/portal-auth"
PERF = "https://gateway.sunrun.com/performance-api/v1"
# Holds the short-lived login session between the two steps, outside the repo.
STATE = Path(tempfile.gettempdir()) / "sunrun_login_probe.json"
TOKENS = Path(tempfile.gettempdir()) / "sunrun_probe_tokens.json"


def fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def shape(obj, depth=0):
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) if depth < 3 else type(v).__name__ for k, v in obj.items()}
    if isinstance(obj, list):
        return [len(obj), shape(obj[0], depth + 1) if obj else None]
    if isinstance(obj, str):
        return f"str[{len(obj)}]"
    return type(obj).__name__


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        return err.code, parsed if parsed is not None else raw[:160]


def find_keys(obj, names, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in names and isinstance(v, str):
                out.append((p, v))
            out += find_keys(v, names, p)
    return out


def normalize(raw: str) -> dict:
    raw = raw.strip()
    if "@" in raw:
        return {"email": raw, "phone": ""}
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    return {"email": "", "phone": "+" + digits}


def request_phase(raw: str) -> int:
    ident = normalize(raw)
    print("1. request-passwordless")
    status, body = call("POST", f"{AUTH}/request-passwordless", ident)
    print("  status:", status, "shape:", shape(body) if not isinstance(body, str) else body)
    if status >= 400 and isinstance(body, dict):
        print("  error keys:", list(body))
    if status >= 400:
        return 1
    session = body.get("token") if isinstance(body, dict) else None
    if not session:
        print("  no session token in response")
        return 1
    STATE.write_text(json.dumps({"ident": ident, "session": session}))
    print("\nCode sent. Now run: scripts/login_probe.py verify <code>")
    return 0


def main(code: str) -> int:
    state = json.loads(STATE.read_text())
    ident, session = state["ident"], state["session"]
    STATE.unlink(missing_ok=True)
    print("2. respond-passwordless")
    status, body = call(
        "POST", f"{AUTH}/respond-passwordless", {**ident, "code": code}, {"Authorization": session}
    )
    print("  status:", status, "shape:", shape(body) if not isinstance(body, str) else body)
    if status >= 400 or not isinstance(body, dict):
        return 1
    issued = time.time()
    data = body.get("data") or {}
    print("  lifetimes (non-secret):", describe_lifetimes(data))
    tokens = find_keys(body, {"accessToken", "refreshToken", "token", "idToken"})
    print(
        "  token-like fields:",
        [(p, f"{len(v)} chars, {v.count('.') + 1} segments") for p, v in tokens],
    )
    access = next((v for p, v in tokens if p.endswith("accessToken")), None)
    refresh = next((v for p, v in tokens if p.endswith("refreshToken")), None)
    if not access:
        print("  no access token found; stop and inspect the shape above")
        return 1
    # Kept so `refresh` can test after the access token expires. Delete when done.
    TOKENS.write_text(json.dumps({"access": access, "refresh": refresh, "data": data}))

    print("\n3. use the new access token")
    status, user = call("GET", f"{AUTH}/get-user", headers={"Authorization": access})
    print("  get-user:", status)
    sites = (user or {}).get("opportunitiesWithContracts") if isinstance(user, dict) else None
    site = str(sites[0]["prospect_id"]) if sites else None
    if site:
        end = date.today() + timedelta(days=1)
        url = (
            f"{PERF}/site-production-daily/{site}?startDate={end - timedelta(days=3)}&endDate={end}"
        )
        status, rows = call("GET", url, headers={"Authorization": access})
        print(
            "  site-production-daily:",
            status,
            "rows:",
            len(rows) if isinstance(rows, list) else shape(rows),
        )

    if not refresh:
        print("\nNo refresh token in the login response; refresh cannot be tested.")
        return 0

    print("\n4. refresh via get-user + RefreshToken header")
    for label, headers in (
        ("RefreshToken only", {"RefreshToken": refresh}),
        ("Authorization + RefreshToken", {"Authorization": access, "RefreshToken": refresh}),
    ):
        status, body2 = call("GET", f"{AUTH}/get-user", headers=headers)
        new = (
            dict(find_keys(body2, {"accessToken", "refreshToken"}))
            if isinstance(body2, dict)
            else {}
        )
        print(f"  [{label}] status: {status}; token fields returned: {sorted(new)}")
        for p, v in new.items():
            old = access if p.endswith("accessToken") else refresh
            print(f"    {p}: {'NEW' if fp(v) != fp(old) else 'same as before'} ({len(v)} chars)")
        if status == 200 and new:
            access2 = next((v for p, v in new.items() if p.endswith("accessToken")), None)
            if access2:
                s2, _ = call("GET", f"{AUTH}/get-user", headers={"Authorization": access2})
                print(f"    refreshed access token works: {s2}")
            s3, _ = call("GET", f"{AUTH}/get-user", headers={"Authorization": access})
            print(f"    original access token still works: {s3}")
            break

    print("\nIssued at (epoch):", int(issued))
    print("Later, once the access token has expired: login_probe.py refresh")
    return 0


def describe_lifetimes(data: dict) -> dict:
    """Iat/Exp may be seconds or milliseconds; report hours either way."""

    def secs(key):
        v = data.get(key)
        return None if not isinstance(v, int) else v / 1000 if v > 10**11 else v

    now = time.time()
    out = {}
    for name in ("access", "refresh"):
        iat, exp = secs(f"{name}TokenIat"), secs(f"{name}TokenExp")
        if iat is not None and exp is not None:
            out[name] = {
                "lifetime_h": round((exp - iat) / 3600, 2),
                "expires_in_h": round((exp - now) / 3600, 2),
            }
    return out


def refresh_phase() -> int:
    saved = json.loads(TOKENS.read_text())
    access, refresh = saved["access"], saved["refresh"]
    print("lifetimes now:", describe_lifetimes(saved["data"]))
    status, _ = call("GET", f"{AUTH}/get-user", headers={"Authorization": access})
    print("access token still accepted by get-user:", status)
    for label, headers in (
        ("Authorization + RefreshToken", {"Authorization": access, "RefreshToken": refresh}),
        ("RefreshToken only", {"RefreshToken": refresh}),
    ):
        status, body = call("GET", f"{AUTH}/get-user", headers=headers)
        new = (
            dict(find_keys(body, {"accessToken", "refreshToken"})) if isinstance(body, dict) else {}
        )
        extra = shape(body) if not isinstance(body, (dict, list)) else sorted(body)[:8]
        print(f"[{label}] status: {status}; token fields returned: {sorted(new)}; body: {extra}")
        for p, v in new.items():
            old = access if p.endswith("accessToken") else refresh
            print(f"  {p}: {'NEW' if fp(v) != fp(old) else 'same as before'} ({len(v)} chars)")
        access2 = next((v for p, v in new.items() if p.endswith("accessToken")), None)
        if access2:
            s2, _ = call("GET", f"{AUTH}/get-user", headers={"Authorization": access2})
            s3, _ = call("GET", f"{AUTH}/get-user", headers={"Authorization": access})
            print(f"  new access token works: {s2}; original still works: {s3}")
            refresh2 = next((v for p, v in new.items() if p.endswith("refreshToken")), refresh)
            TOKENS.write_text(
                json.dumps({"access": access2, "refresh": refresh2, "data": saved["data"]})
            )
            break
    return 0


if __name__ == "__main__":
    load_env()
    args = sys.argv[1:]
    if args[:1] == ["request"]:
        # The identifier can come from SUNRUN_LOGIN (env or .env) to keep it out of shell history.
        login = args[1] if len(args) > 1 else os.environ.get("SUNRUN_LOGIN", "")
        if not login:
            sys.exit("Usage: login_probe.py request [email-or-phone]  (or set SUNRUN_LOGIN)")
        sys.exit(request_phase(login))
    if args[:1] == ["verify"] and len(args) == 2:
        sys.exit(main(args[1].strip()))
    if args == ["refresh"]:
        sys.exit(refresh_phase())
    if args == ["cleanup"]:
        TOKENS.unlink(missing_ok=True)
        sys.exit("saved probe tokens deleted")
    sys.exit("Usage: login_probe.py request [email-or-phone] | verify <code> | refresh | cleanup")
