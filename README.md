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

### Signing in

Enter the mobile number on your Sunrun account. Sunrun texts you a 6-digit code, the same
sign-in as my.sunrun.com. Enter it and your site is found automatically (you pick one if the
account has several). If the code is wrong or expired, tick "Send a new code" and submit.

The sign-in gives Home Assistant a short-lived access token (24 hours as declared by Sunrun)
and a long-lived refresh token. The integration refreshes when Sunrun rejects the access
token. If Sunrun stops accepting the tokens, Home Assistant shows a re-authentication prompt and
texts a new code to the stored number. Nothing else changes. Only tokens and your number are
stored, never a password.

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
