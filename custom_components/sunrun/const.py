"""Constants for the Sunrun integration."""

from datetime import timedelta

DOMAIN = "sunrun"
CONF_TOKEN = "token"
CONF_SITE_ID = "site_id"

UPDATE_INTERVAL = timedelta(minutes=60)
# Sunrun backfills late data, so recent days are re-read and re-written every update.
RESYNC_DAYS = 3
