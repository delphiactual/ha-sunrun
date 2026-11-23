"""Standalone check of Sunrun's v1 API. Needs SUNRUN_TOKEN (env or .env); SUNRUN_SITE optional."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

BASE = "https://gateway.sunrun.com/performance-api/v1"
TOLERANCE = 0.0005
WINDOWS = (7, 14, 31, 62, 92, 183, 366)


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def get(path: str, site: str, token: str, start: datetime, end: datetime) -> list[dict]:
    query = urllib.parse.urlencode({"startDate": start.isoformat(), "endDate": end.isoformat()})
    req = urllib.request.Request(f"{BASE}/{path}/{site}?{query}", headers={"Authorization": token})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    if isinstance(body, dict):
        for key in ("data", "items", "results", "rows"):
            if isinstance(body.get(key), list):
                return body[key]
        raise SystemExit(f"Unexpected response shape from {path}: keys={list(body)}")
    return body


def find_site(token: str) -> str:
    req = urllib.request.Request(
        "https://gateway.sunrun.com/portal-auth/get-user", headers={"Authorization": token}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        opps = json.load(resp).get("opportunitiesWithContracts") or []
    if not opps:
        raise SystemExit("No sites found for this token")
    return str(opps[0]["prospect_id"])


def day_start(d: date, tz) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz)


def main() -> int:
    load_env()
    token, site = os.environ.get("SUNRUN_TOKEN"), os.environ.get("SUNRUN_SITE")
    if not token:
        print("Set SUNRUN_TOKEN (env or .env); SUNRUN_SITE is optional")
        return 2
    if not site:
        site = find_site(token)
        print("SUNRUN_SITE not set; using the first site on the account")

    tz = datetime.now().astimezone().tzinfo
    today = datetime.now(tz).date()
    end = day_start(today + timedelta(days=1), tz)

    # 1. daily history
    daily = get("site-production-daily", site, token, day_start(date(2015, 1, 1), tz), end)
    print(f"daily rows: {len(daily)}; sample row: {json.dumps(daily[0] if daily else None)}")
    date_key = next(
        (k for k in ("date", "timestamp", "startDate") if daily and k in daily[0]), None
    )
    if date_key is None:
        print("Cannot find a date field in daily rows; inspect the sample above")
        return 1
    by_day = {
        datetime.fromisoformat(r[date_key].replace("Z", "+00:00")).date(): r["pvSolar"]
        for r in daily
        if r.get("pvSolar") is not None
    }
    install = min(by_day)
    print(f"first daily date (install day): {install}")

    # 2. max minute window
    best = 0
    for days in WINDOWS:
        # Stop at today's start so a partial current day doesn't read as truncation.
        s = day_start(today - timedelta(days=days), tz)
        try:
            rows = get("site-production-minute", site, token, s, day_start(today, tz))
        except urllib.error.HTTPError as err:
            print(f"window {days:>3}d: HTTP {err.code}")
            break
        ok = len(rows) >= days * 96 * 0.9
        print(f"window {days:>3}d: {len(rows)} rows ({'ok' if ok else 'truncated'})")
        if not ok:
            break
        best = days
    print(f"max working window: {best} days")

    # 3. history depth: earliest day with minute data (assumes data is contiguous after it)
    def has_data(d: date) -> bool:
        try:
            return bool(
                get(
                    "site-production-minute",
                    site,
                    token,
                    day_start(d, tz),
                    day_start(d + timedelta(days=1), tz),
                )
            )
        except urllib.error.HTTPError:
            return False

    lo, hi = install, today
    if has_data(lo):
        earliest = lo
    else:
        while (hi - lo).days > 1:
            mid = lo + timedelta(days=(hi - lo).days // 2)
            if has_data(mid):
                hi = mid
            else:
                lo = mid
        earliest = hi
    print(f"minute data begins: {earliest} ({(today - earliest).days} days back)")

    # 4. last 3 complete days: minute sums vs daily
    start = day_start(today - timedelta(days=3), tz)
    minute = get("site-production-minute", site, token, start, end)
    if minute:
        print(f"sample minute row: {json.dumps(minute[0])}")
    sums: dict[date, float] = defaultdict(float)
    for r in minute:
        if r.get("pvSolar") is not None:
            sums[datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).date()] += r[
                "pvSolar"
            ]
    bad = False
    print(f"{'day':<12}{'minute sum':>12}{'daily':>12}{'diff':>10}")
    for i in (3, 2, 1):
        d = today - timedelta(days=i)
        m, dv = sums.get(d, 0.0), by_day.get(d)
        diff = None if dv is None else m - dv
        flag = "" if diff is not None and abs(diff) <= TOLERANCE else "  MISMATCH"
        bad |= bool(flag)
        print(
            f"{d!s:<12}{m:>12.3f}{dv if dv is not None else float('nan'):>12.3f}"
            f"{diff if diff is not None else float('nan'):>10.4f}{flag}"
        )
    print("PASS" if not bad else "FAIL: sums do not match, stop and report")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
