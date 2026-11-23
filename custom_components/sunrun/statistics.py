"""Reading and writing the external solar production statistic."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .const import DOMAIN

BATCH = 24 * 31
ANCHOR_LOOKBACK = timedelta(days=7)


def statistic_id(site_id: str) -> str:
    return f"{DOMAIN}:{site_id}_solar_production"


def metadata(site_id: str, name: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"{name} solar production",
        source=DOMAIN,
        statistic_id=statistic_id(site_id),
        unit_class="energy",
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


async def async_has_statistics(hass: HomeAssistant, meta: StatisticMetaData) -> bool:
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, meta["statistic_id"], True, {"sum"}
    )
    return bool(last.get(meta["statistic_id"]))


async def async_anchor_sum(
    hass: HomeAssistant, meta: StatisticMetaData, before: datetime
) -> float | None:
    """Running total of the last hour recorded before `before`, or None if there is none."""
    stat_id = meta["statistic_id"]
    found = await get_instance(hass).async_add_executor_job(
        lambda: statistics_during_period(
            hass, before - ANCHOR_LOOKBACK, before, {stat_id}, "hour", None, {"sum"}
        )
    )
    sums = [row["sum"] for row in found.get(stat_id, []) if row.get("sum") is not None]
    return sums[-1] if sums else None


def async_write(
    hass: HomeAssistant, meta: StatisticMetaData, rows: list[tuple[datetime, float]]
) -> None:
    for i in range(0, len(rows), BATCH):
        async_add_external_statistics(
            hass,
            meta,
            [StatisticData(start=start, sum=total) for start, total in rows[i : i + BATCH]],
        )
