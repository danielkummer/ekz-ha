"""EKZ Power Usage sensors - reads scraped CSV files from the ekz-ha scraper.

Uses a DataUpdateCoordinator so all sensors share a single file read per
interval. This is important when data lives on a remote machine (NFS / rsync):
files are read exactly once per cycle, and long-term statistics are injected
exactly once per cycle — regardless of how many sensors are configured.

Sensors exposed:
  sensor.ekz_latest_day_kwh     - Most recent day with data (total kWh)
  sensor.ekz_latest_day_ht_kwh  - Most recent day HT (peak) kWh
  sensor.ekz_latest_day_nt_kwh  - Most recent day NT (off-peak) kWh
  sensor.ekz_current_month_kwh  - Current month accumulated kWh
  sensor.ekz_year_to_date_kwh   - Year-to-date kWh (sum of monthly values)
  sensor.ekz_latest_bill_chf    - Most recent invoice amount (CHF)
  sensor.ekz_total_billed_chf   - Sum of all known invoice amounts (CHF)

Long-term statistics injected (updated every poll interval):
  ekz_power:monthly_cost_chf    - state=per-bill CHF, sum=cumulative YTD CHF
  These power the statistics-graph Lovelace cards for monthly bar and YTD line.

Configuration (configuration.yaml):

  sensor:
    - platform: ekz_power
      data_dir: /media/ekz-ha/csv    # path as seen by HA (NFS / local)
      bills_dir: /media/ekz-ha/bills  # optional; defaults to ../bills
      scan_interval: 3600               # re-read interval in seconds (default 3600)
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_SCAN_INTERVAL, UnitOfEnergy
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

_LOGGER = logging.getLogger(__name__)

CONF_DATA_DIR = "data_dir"
CONF_BILLS_DIR = "bills_dir"

# Locale-independent month name map (EN + DE) for parsing CSV Zeitraum strings.
_MONTH_NAMES: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "januar": 1, "februar": 2, "märz": 3,
    "mai": 5, "juni": 6, "juli": 7,
    "oktober": 10, "dezember": 12,
}


_KWH_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_kwh(raw: str) -> str:
    """Strip annotation suffixes like '(geschätzt)' before float parsing."""
    return _KWH_ANNOTATION_RE.sub("", raw).replace(",", ".").strip()


def _kwh_col(row: dict[str, str], *cols: str) -> str:
    """Return cleaned value from the first matching column that has data."""
    for col in cols:
        val = _clean_kwh(row.get(col, ""))
        if val:
            return val
    return ""


def _parse_zeitraum_ym(zeitraum: str) -> tuple[int, int] | None:
    """Parse 'May 2026', 'Mai 2026', '2026-05' into (year, month). Locale-safe."""
    z = zeitraum.strip()
    year_m = re.search(r"\b(\d{4})\b", z)
    if not year_m:
        return None
    year = int(year_m.group(1))
    for name, month in _MONTH_NAMES.items():
        if name in z.lower():
            return (year, month)
    numeric = re.search(r"\b(0?[1-9]|1[0-2])\b", z.replace(year_m.group(1), ""))
    if numeric:
        return (year, int(numeric.group(1)))
    return None

# External statistic ID injected into HA recorder for chart cards
STAT_ID_MONTHLY_COST = "ekz_power:monthly_cost_chf"

DEFAULT_SCAN_INTERVAL = timedelta(hours=1)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_DATA_DIR): cv.string,
        vol.Optional(CONF_BILLS_DIR): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): cv.time_period,
    }
)


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities, discovery_info=None
):
    data_dir = config[CONF_DATA_DIR]
    bills_dir = config.get(CONF_BILLS_DIR) or str(Path(data_dir).parent / "bills")
    scan_interval: timedelta = config[CONF_SCAN_INTERVAL]

    reader = _EkzReader(Path(data_dir), Path(bills_dir))
    coordinator = EkzCoordinator(hass, reader, scan_interval)

    # Initial fetch — reads files and injects statistics before entities are added
    await coordinator.async_refresh()

    async_add_entities(
        [
            EkzDailySensor(coordinator, "Gesamt [kWh]", "ekz_latest_day_kwh",     "EKZ Latest Day"),
            EkzDailySensor(coordinator, "HT [kWh]",     "ekz_latest_day_ht_kwh",  "EKZ Latest Day HT"),
            EkzDailySensor(coordinator, "NT [kWh]",     "ekz_latest_day_nt_kwh",  "EKZ Latest Day NT"),
            EkzMonthSensor(coordinator),
            EkzYearSensor(coordinator),
            EkzLatestBillSensor(coordinator),
            EkzTotalBilledSensor(coordinator),
        ]
    )




class EkzCoordinator(DataUpdateCoordinator["_EkzReader"]):
    """Reads all EKZ data files once per poll interval.

    All sensors subscribe to this coordinator; when it fetches new data every
    sensor updates atomically from the same snapshot. Works identically whether
    data_dir is a local path, NFS mount, or rsync-populated directory.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        reader: "_EkzReader",
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="ekz_power",
            update_interval=update_interval,
        )
        self._reader = reader

    async def _async_update_data(self) -> "_EkzReader":
        # Read CSVs in a thread (blocking I/O — may be network-backed)
        await self.hass.async_add_executor_job(self._reader.update)
        # Inject/refresh HA long-term statistics for chart cards
        await _inject_bill_statistics(self.hass, self._reader.bills)
        return self._reader




async def _inject_bill_statistics(
    hass: HomeAssistant, bills: list[dict]
) -> None:
    """Upsert bill CHF amounts into HA recorder long-term statistics.

    state = per-bill amount  → monthly bar chart
    sum   = running total    → year-to-date line chart

    async_add_external_statistics upserts by (statistic_id, start), so calling
    this on every coordinator refresh is safe and idempotent.
    """
    try:
        from homeassistant.components.recorder.models import (
            StatisticData,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )
    except ImportError:
        _LOGGER.debug("Recorder not available — skipping statistics injection")
        return

    valid: list[tuple[str, float]] = []
    for b in bills:
        amount_str = b.get("amount_chf", "").strip()
        period_end = b.get("period_end", "").strip()
        if not amount_str or not period_end:
            continue
        try:
            valid.append((period_end, float(amount_str)))
        except ValueError:
            continue

    if not valid:
        return

    valid.sort(key=lambda x: x[0])

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name="EKZ Monthly Cost",
        source="ekz_power",
        statistic_id=STAT_ID_MONTHLY_COST,
        unit_of_measurement="CHF",
    )

    stats: list[StatisticData] = []
    cumulative = 0.0
    for period_end, amount in valid:
        cumulative = round(cumulative + amount, 2)
        try:
            dt = datetime.fromisoformat(period_end).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
            )
        except ValueError:
            continue
        stats.append(StatisticData(start=dt, state=amount, sum=cumulative))

    async_add_external_statistics(hass, metadata, stats)
    _LOGGER.debug(
        "Injected %d bill statistic(s) into recorder (ytd=%.2f CHF)",
        len(stats),
        cumulative,
    )




class _EkzReader:
    """Reads EKZ CSV files from disk and caches parsed results."""

    _STALE_THRESHOLD_HOURS = 48

    def __init__(self, data_dir: Path, bills_dir: Path) -> None:
        self._dir = data_dir
        self._bills_dir = bills_dir
        self.daily: dict[str, Any] = {}
        self.monthly: dict[str, float] = {}
        self.yearly: dict[str, float] = {}
        self.bills: list[dict[str, str]] = []
        self.daily_age_h: float | None = None
        self.monthly_age_h: float | None = None

    def update(self) -> None:
        self.daily = self._parse_daily()
        self.monthly = self._parse_monthly()
        self.yearly = self._parse_yearly()
        self.bills = self._parse_bills()

    def _latest(self, pattern: str) -> Path | None:
        files = sorted(self._dir.glob(pattern), reverse=True)
        return files[0] if files else None

    @staticmethod
    def _read(path: Path) -> list[dict[str, str]]:
        lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                 if not l.startswith("sep=")]
        return list(csv.DictReader(lines, delimiter=";"))

    def _parse_daily(self) -> dict[str, Any]:
        path = self._latest("*_daily.csv")
        if not path:
            self.daily_age_h = None
            return {}
        self.daily_age_h = (
            datetime.now(tz=timezone.utc)
            - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        ).total_seconds() / 3600
        for row in reversed(self._read(path)):
            val_str = _kwh_col(row, "Gesamt [kWh]", "Verbrauch [kWh]")
            if not val_str:
                continue
            try:
                return {
                    "date":   row["Zeitraum"],
                    "gesamt": float(val_str),
                    # HT/NT may not exist in new-format CSVs; default to None.
                    "ht":     float(_clean_kwh(row.get("HT [kWh]", "") or "")) if _clean_kwh(row.get("HT [kWh]", "")) else None,
                    "nt":     float(_clean_kwh(row.get("NT [kWh]", "") or "")) if _clean_kwh(row.get("NT [kWh]", "")) else None,
                    "source": path.name,
                }
            except ValueError:
                continue
        return {}

    def _parse_monthly(self) -> dict[str, float]:
        path = self._latest("*_monthly.csv")
        if not path:
            self.monthly_age_h = None
            return {}
        self.monthly_age_h = (
            datetime.now(tz=timezone.utc)
            - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        ).total_seconds() / 3600
        out: dict[str, float] = {}
        for row in self._read(path):
            val = _kwh_col(row, "Gesamt [kWh]", "Verbrauch [kWh]")
            if val:
                try:
                    out[row["Zeitraum"]] = float(val)
                except ValueError:
                    pass
        return out

    def _parse_yearly(self) -> dict[str, float]:
        path = self._latest("*_yearly.csv")
        if not path:
            return {}
        out: dict[str, float] = {}
        for row in self._read(path):
            # Yearly CSV may use Gesamt [kWh] (old) or ET [kWh] (new smart meter)
            val = _kwh_col(row, "Gesamt [kWh]", "ET [kWh]", "Verbrauch [kWh]")
            if val:
                try:
                    out[row["Zeitraum"]] = float(val)
                except ValueError:
                    pass
        return out

    def _parse_bills(self) -> list[dict[str, str]]:
        path = self._bills_dir / "bills.csv"
        if not path.exists():
            return []
        try:
            return list(csv.DictReader(
                path.read_text(encoding="utf-8").splitlines(), delimiter=";"
            ))
        except Exception as exc:
            _LOGGER.warning("Could not read bills.csv: %s", exc)
            return []

    def current_month_kwh(self) -> float | None:
        """Return kWh for the current calendar month, or None if not yet in the CSV.

        Uses locale-independent (year, month) tuple matching so it works correctly
        regardless of the system locale or whether the portal exports English or
        German month names.
        """
        today = date.today()
        current_ym = (today.year, today.month)
        for zeitraum, value in self.monthly.items():
            ym = _parse_zeitraum_ym(zeitraum)
            if ym == current_ym:
                return value
        return None

    def year_to_date_kwh(self) -> float | None:
        """Sum of all monthly kWh values for the current calendar year only."""
        current_year = date.today().year
        values = []
        for zeitraum, value in self.monthly.items():
            ym = _parse_zeitraum_ym(zeitraum)
            if ym and ym[0] == current_year:
                values.append(value)
        return round(sum(values), 2) if values else None

    def latest_bill(self) -> dict[str, str] | None:
        valid = [r for r in self.bills if r.get("amount_chf")]
        return sorted(valid, key=lambda r: r.get("period_end", ""))[-1] if valid else None

    def total_billed_chf(self) -> float | None:
        amounts = []
        for row in self.bills:
            v = row.get("amount_chf", "").strip()
            if v:
                try:
                    amounts.append(float(v))
                except ValueError:
                    pass
        return round(sum(amounts), 2) if amounts else None

    def monthly_costs(self) -> list[dict]:
        """Bills sorted chronologically with running cumulative totals.

        Useful as a sensor attribute for apexcharts-card data_generator.
        """
        result = []
        cumulative = 0.0
        for b in sorted(self.bills, key=lambda b: b.get("period_end", "")):
            amount_str = b.get("amount_chf", "").strip()
            if not amount_str:
                continue
            try:
                amount = float(amount_str)
            except ValueError:
                continue
            cumulative = round(cumulative + amount, 2)
            result.append({
                "period_start":   b.get("period_start", ""),
                "period_end":     b.get("period_end", ""),
                "amount_chf":     amount,
                "cumulative_chf": cumulative,
                "type":           b.get("type", ""),
            })
        return result




class _EkzBase(CoordinatorEntity["EkzCoordinator"], SensorEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def _reader(self) -> _EkzReader:
        return self.coordinator.data




class EkzDailySensor(_EkzBase):
    """kWh value for the most recent day that has data."""

    def __init__(
        self,
        coordinator: "EkzCoordinator",
        column: str,
        unique_id: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._column = column
        self._attr_unique_id = unique_id
        self._attr_name = name

    @property
    def native_value(self):
        col_map = {"Gesamt [kWh]": "gesamt", "HT [kWh]": "ht", "NT [kWh]": "nt"}
        return self._reader.daily.get(col_map.get(self._column, "gesamt"))

    @property
    def extra_state_attributes(self):
        r = self._reader
        attrs: dict[str, Any] = {
            "data_date":   r.daily.get("date"),
            "source_file": r.daily.get("source"),
        }
        if r.daily_age_h is not None:
            attrs["data_age_hours"] = round(r.daily_age_h, 1)
            if r.daily_age_h > r._STALE_THRESHOLD_HOURS:
                attrs["stale"] = True
        return attrs


class EkzMonthSensor(_EkzBase):
    """Accumulated kWh for the current calendar month."""

    _attr_unique_id = "ekz_current_month_kwh"
    _attr_name = "EKZ Current Month"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self):
        return self._reader.current_month_kwh()

    @property
    def extra_state_attributes(self):
        r = self._reader
        attrs: dict[str, Any] = {
            "month":      date.today().strftime("%B %Y"),
            "all_months": r.monthly,
        }
        if r.monthly_age_h is not None:
            attrs["data_age_hours"] = round(r.monthly_age_h, 1)
            if r.monthly_age_h > r._STALE_THRESHOLD_HOURS:
                attrs["stale"] = True
        return attrs


class EkzYearSensor(_EkzBase):
    """Year-to-date kWh (sum of all months in the current annual CSV)."""

    _attr_unique_id = "ekz_year_to_date_kwh"
    _attr_name = "EKZ Year to Date"
    _attr_state_class = SensorStateClass.TOTAL

    @property
    def native_value(self):
        return self._reader.year_to_date_kwh()

    @property
    def extra_state_attributes(self):
        r = self._reader
        attrs: dict[str, Any] = {
            "year":               date.today().year,
            "monthly_breakdown":  r.monthly,
            "yearly_totals":      r.yearly,
        }
        if r.monthly_age_h is not None:
            attrs["data_age_hours"] = round(r.monthly_age_h, 1)
            if r.monthly_age_h > r._STALE_THRESHOLD_HOURS:
                attrs["stale"] = True
        return attrs


class EkzLatestBillSensor(CoordinatorEntity["EkzCoordinator"], SensorEntity):
    """Amount of the most recent EKZ invoice in CHF."""

    _attr_unique_id = "ekz_latest_bill_chf"
    _attr_name = "EKZ Latest Bill"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "CHF"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    @property
    def native_value(self):
        bill = self.coordinator.data.latest_bill()
        if bill:
            try:
                return float(bill["amount_chf"])
            except (ValueError, KeyError):
                pass
        return None

    @property
    def extra_state_attributes(self):
        bill = self.coordinator.data.latest_bill()
        if not bill:
            return {}
        return {
            "period_start": bill.get("period_start"),
            "period_end":   bill.get("period_end"),
            "type":         bill.get("type"),
            "address":      bill.get("address"),
            "pdf_file":     bill.get("pdf_file"),
        }


class EkzTotalBilledSensor(CoordinatorEntity["EkzCoordinator"], SensorEntity):
    """Sum of all known EKZ invoice amounts in CHF.

    The 'monthly_costs' attribute provides a chronological list with running
    cumulative totals — ready for use with apexcharts-card data_generator.
    """

    _attr_unique_id = "ekz_total_billed_chf"
    _attr_name = "EKZ Total Billed"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "CHF"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    @property
    def native_value(self):
        return self.coordinator.data.total_billed_chf()

    @property
    def extra_state_attributes(self):
        reader = self.coordinator.data
        return {
            "bill_count":    len(reader.bills),
            "monthly_costs": reader.monthly_costs(),
        }
