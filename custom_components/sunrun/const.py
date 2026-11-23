"""Constants for the Sunrun integration."""

from datetime import timedelta

DOMAIN = "sunrun"
CONF_PHONE = "phone"
CONF_TOKENS = "tokens"
CONF_SITE_ID = "site_id"
CONF_CODE = "code"
CONF_RESEND = "resend"

UPDATE_INTERVAL = timedelta(minutes=60)
# Sunrun backfills late data, so recent days are re-read and re-written every update.
RESYNC_DAYS = 3
