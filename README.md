# Sunrun for Home Assistant

Writes your Sunrun solar production into Home Assistant as hourly long-term statistics, so the
Energy dashboard shows each day's production on the right day and hour, back to install day.

It reads Sunrun's `performance-api/v1` (15-minute `pvSolar` readings and daily totals). It
replaces setups that scrape `cumulative-production/daily`, whose days are dated one day late.

## What you get

- External statistic `sunrun:<site>_solar_production` (kWh, cumulative), hourly in local time.
- Full history on first run. Minute data is used as far back as it exists. Anything older uses
  daily totals, one row at 23:00 local on the correct day, so the running total stays continuous.
- Every hour the last 3 days are re-read and rewritten, because Sunrun backfills late data.
- Sensors: today's production (kWh) and the timestamp of the latest data.

## Install

1. HACS > three-dot menu > Custom repositories > add `https://github.com/delphiactual/ha-sunrun`
   as an Integration, then install **Sunrun**.
2. Restart Home Assistant.
3. Settings > Devices & services > Add integration > **Sunrun**.

### Getting the token

Sign in at <https://my.sunrun.com>, open the browser developer tools, go to Application (Chrome)
or Storage (Firefox) > Local Storage > `https://my.sunrun.com`, and copy the value of
`AUTH_TOKEN`. Paste it into the setup form. Your site is looked up from the token, and if the
account has more than one system you pick one.

The token's lifetime is not known and the integration cannot refresh it yet. If Sunrun rejects
it, Home Assistant shows a re-authentication prompt: paste a fresh token and nothing else
changes.

## Switching over from a scraped sensor

1. Add the integration and wait for the first run to finish. The history is written in the
   background and can take a few minutes.
2. Developer tools > Statistics: confirm `sunrun:<site>_solar_production` exists and its recent
   days match the Sunrun app.
3. Settings > Dashboards > Energy > Solar panels: replace the old source with
   `sunrun:<site>_solar_production`.
4. Remove the old scraped sensor, then clear its statistics under Developer tools > Statistics
   (the fix option next to the entity).

## Notes

- Only production is available. Sunrun returns no consumption, import or export readings.
- If you delete the statistic, the next update rebuilds the full history.
- Not affiliated with Sunrun. This uses the same API as the customer web app, which may change.

## Development

```
pip install -e ".[dev]"
ruff check . && pytest
```

`scripts/smoke_test.py` checks the API with `SUNRUN_TOKEN` and `SUNRUN_SITE` set (in the
environment or a git-ignored `.env`).
