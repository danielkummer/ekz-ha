"""Push scraped EKZ data to Home Assistant via REST + WebSocket APIs.

No custom component is needed on the HA instance — all data is pushed from
the scraper Pi directly.
"""
import asyncio
import csv
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

_REST_TIMEOUT = 10  # seconds per HTTP call
_STALE_THRESHOLD_HOURS = 48  # mark sensor stale if CSV is older than this
_EST_COST_PER_KWH = 0.25  # rough CHF/kWh used for cost estimates and projections

# Hardcoded month-name maps so Zeitraum parsing is locale-independent.
# The EKZ portal exports English names; German is a safety fallback.
_MONTH_NAMES: dict[str, int] = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    # German
    "januar": 1, "februar": 2, "märz": 3,
    "mai": 5, "juni": 6, "juli": 7,
    "oktober": 10, "dezember": 12,
}


def _parse_zeitraum_ym(zeitraum: str) -> tuple[int, int] | None:
    """Parse an EKZ 'Zeitraum' string into a (year, month) tuple.

    Handles formats like 'May 2026', 'Mai 2026', '2026-05', '05.2026'.
    Returns None if the string cannot be parsed.
    """
    z = zeitraum.strip()
    year_m = re.search(r"\b(\d{4})\b", z)
    if not year_m:
        return None
    year = int(year_m.group(1))

    # Try named month (English or German, case-insensitive)
    for name, month in _MONTH_NAMES.items():
        if name in z.lower():
            return (year, month)

    # Try numeric month: "2026-05" or "05.2026"
    numeric = re.search(r"\b(0?[1-9]|1[0-2])\b", z.replace(year_m.group(1), ""))
    if numeric:
        return (year, int(numeric.group(1)))

    return None




def _latest_csv(csv_dir: Path, label: str) -> Path | None:
    files = sorted(csv_dir.glob(f"*_{label}.csv"), reverse=True)
    if files:
        logger.debug("Latest %s CSV: %s", label, files[0].name)
    else:
        logger.debug("No %s CSV files found in %s", label, csv_dir)
    return files[0] if files else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    logger.debug("Reading CSV: %s", path)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if lines and lines[0].startswith("sep="):
        lines = lines[1:]
    reader = csv.DictReader(lines, delimiter=";")
    # Filter empty rows; handle None values by treating them as empty strings
    rows = [row for row in reader if any((v or "").strip() for v in row.values())]
    logger.debug("  → %d non-empty rows", len(rows))
    return rows


_KWH_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_kwh(raw: str) -> str:
    """Strip annotation suffixes like '(geschätzt)' and Swiss thousands
    separators (1'234.5) before float parsing."""
    cleaned = _KWH_ANNOTATION_RE.sub("", raw).strip()
    return cleaned.replace("'", "").replace("\u2019", "").replace(",", ".")


def _float(s: str) -> float | None:
    try:
        return float(_clean_kwh(s))
    except (ValueError, AttributeError):
        return None


def _kwh_col(row: dict[str, str], *cols: str) -> str:
    """Return cleaned value from the first matching column that has data."""
    for col in cols:
        val = _clean_kwh(row.get(col, ""))
        if val:
            return val
    return ""


def _read_bills(bills_csv: Path) -> list[dict]:
    if not bills_csv.exists():
        return []
    lines = bills_csv.read_text(encoding="utf-8").splitlines()
    return [
        row for row in csv.DictReader(lines, delimiter=";")
        if row.get("amount_chf", "").strip()
    ]


def build_states(csv_dir: Path, bills_csv: Path) -> dict[str, dict]:
    """Return {entity_id: HA state payload} for every EKZ sensor.

    kWh sensors always appear in the result — state is "unavailable" when no
    data is available so HA doesn't keep displaying a stale value indefinitely.
    """
    now = datetime.now(tz=timezone.utc)
    states: dict[str, dict] = {}

    # ---- Daily ----------------------------------------------------------
    daily_path = _latest_csv(csv_dir, "daily")
    daily_age_h: float | None = None
    if daily_path:
        daily_age_h = (now - datetime.fromtimestamp(daily_path.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        # Accept both old format (HT/NT/Gesamt) and new format (Verbrauch [kWh]).
        rows = [r for r in _read_csv(daily_path) if _kwh_col(r, "Gesamt [kWh]", "Verbrauch [kWh]")]
        last = rows[-1] if rows else None
        date_str = last.get("Zeitraum", "") if last else ""
        for entity, cols, fname in [
            ("sensor.ekz_latest_day_kwh",    ("Gesamt [kWh]", "Verbrauch [kWh]"), "EKZ Latest Day"),
            ("sensor.ekz_latest_day_ht_kwh", ("HT [kWh]",),                       "EKZ Latest Day HT"),
            ("sensor.ekz_latest_day_nt_kwh", ("NT [kWh]",),                       "EKZ Latest Day NT"),
        ]:
            raw = _kwh_col(last, *cols) if last else ""
            val = _float(raw) if raw else None
            attrs: dict = {
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "friendly_name": fname,
                "data_date": date_str,
                "source_file": daily_path.name,
                "data_age_hours": round(daily_age_h, 1),
            }
            if daily_age_h is not None and daily_age_h > _STALE_THRESHOLD_HOURS:
                attrs["stale"] = True
            states[entity] = {
                "state": str(round(val, 3)) if val is not None else "unavailable",
                "attributes": attrs,
            }
    else:
        for entity, fname in [
            ("sensor.ekz_latest_day_kwh",    "EKZ Latest Day"),
            ("sensor.ekz_latest_day_ht_kwh", "EKZ Latest Day HT"),
            ("sensor.ekz_latest_day_nt_kwh", "EKZ Latest Day NT"),
        ]:
            states[entity] = {
                "state": "unavailable",
                "attributes": {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "measurement",
                    "friendly_name": fname,
                },
            }

    # ---- Monthly / YTD --------------------------------------------------
    monthly_path = _latest_csv(csv_dir, "monthly")
    monthly_age_h: float | None = None
    if monthly_path:
        monthly_age_h = (now - datetime.fromtimestamp(monthly_path.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        current_ym = (now.year, now.month)
        # Accept both old format (Gesamt [kWh]) and new format (Verbrauch [kWh]).
        rows_all = [r for r in _read_csv(monthly_path) if _kwh_col(r, "Gesamt [kWh]", "Verbrauch [kWh]")]

        # Parse (year, month) for each row — locale-independent.
        rows_parsed: list[tuple[tuple[int, int], dict]] = []
        for r in rows_all:
            ym = _parse_zeitraum_ym(r.get("Zeitraum", ""))
            if ym:
                rows_parsed.append((ym, r))

        # Only rows from the current year for YTD.
        rows_this_year = [(ym, r) for ym, r in rows_parsed if ym[0] == now.year]

        # Current month: exact (year, month) match.
        current_month_row = next(
            (r for ym, r in rows_parsed if ym == current_ym),
            None
        )
        current = _float(_kwh_col(current_month_row, "Gesamt [kWh]", "Verbrauch [kWh]")) if current_month_row else None

        attrs_month: dict = {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "friendly_name": "EKZ Current Month",
            "month": f"{now.year}-{now.month:02d}",
            "source_file": monthly_path.name,
            "data_age_hours": round(monthly_age_h, 1),
        }
        if monthly_age_h > _STALE_THRESHOLD_HOURS:
            attrs_month["stale"] = True
        states["sensor.ekz_current_month_kwh"] = {
            "state": str(round(current, 3)) if current is not None else "unavailable",
            "attributes": attrs_month,
        }

        # YTD: sum only months that belong to the current year.
        ytd = sum(
            v for _ym, r in rows_this_year
            if (v := _float(_kwh_col(r, "Gesamt [kWh]", "Verbrauch [kWh]"))) is not None
        )
        attrs_ytd: dict = {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total",
            "friendly_name": "EKZ Year to Date",
            "year": now.year,
            "source_file": monthly_path.name,
            "data_age_hours": round(monthly_age_h, 1),
        }
        if monthly_age_h > _STALE_THRESHOLD_HOURS:
            attrs_ytd["stale"] = True
        states["sensor.ekz_year_to_date_kwh"] = {
            "state": str(round(ytd, 3)) if rows_this_year else "unavailable",
            "attributes": attrs_ytd,
        }
    else:
        for entity, fname, sc in [
            ("sensor.ekz_current_month_kwh", "EKZ Current Month", "total_increasing"),
            ("sensor.ekz_year_to_date_kwh",  "EKZ Year to Date",  "total"),
        ]:
            states[entity] = {
                "state": "unavailable",
                "attributes": {
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": sc,
                    "friendly_name": fname,
                },
            }

    # ---- Bills ----------------------------------------------------------
    bills = _read_bills(bills_csv)
    # Parse amounts defensively — skip rows with missing or malformed values.
    valid_bills: list[tuple[dict, float]] = []
    for b in sorted(bills, key=lambda b: b.get("period_end", "")):
        try:
            valid_bills.append((b, float(b["amount_chf"])))
        except (ValueError, TypeError, KeyError):
            logger.warning(
                "Skipping bill row with invalid amount_chf: %r", b.get("amount_chf")
            )

    if valid_bills:
        latest_bill, latest_amt = valid_bills[-1]
        total_billed = round(sum(amt for _, amt in valid_bills), 2)

        cumulative = 0.0
        monthly_costs = []
        for b, amt in valid_bills:
            cumulative = round(cumulative + amt, 2)
            monthly_costs.append({
                "period_start":   b.get("period_start"),
                "period_end":     b.get("period_end"),
                "amount_chf":     amt,
                "cumulative_chf": cumulative,
                "type":           b.get("type"),
            })

        states["sensor.ekz_latest_bill_chf"] = {
            "state": str(round(latest_amt, 2)),
            "attributes": {
                "unit_of_measurement": "CHF",
                "device_class": "monetary",
                "state_class": "measurement",
                "friendly_name": "EKZ Latest Bill",
                "period_start": latest_bill.get("period_start"),
                "period_end":   latest_bill.get("period_end"),
                "type":         latest_bill.get("type"),
                "address":      latest_bill.get("address"),
                "pdf_file":     latest_bill.get("pdf_file"),
            },
        }
        states["sensor.ekz_total_billed_chf"] = {
            "state": str(total_billed),
            "attributes": {
                "unit_of_measurement": "CHF",
                "device_class": "monetary",
                "state_class": "total",
                "friendly_name": "EKZ Total Billed",
                "monthly_costs": monthly_costs,
            },
        }
    else:
        for entity, fname, sc in [
            ("sensor.ekz_latest_bill_chf",  "EKZ Latest Bill",   "measurement"),
            ("sensor.ekz_total_billed_chf", "EKZ Total Billed",  "total"),
        ]:
            states[entity] = {
                "state": "unavailable",
                "attributes": {
                    "unit_of_measurement": "CHF",
                    "device_class": "monetary",
                    "state_class": sc,
                    "friendly_name": fname,
                },
            }

    # ---- Last scrape timestamp ------------------------------------------
    states["sensor.ekz_last_scrape"] = {
        "state": now.isoformat(),
        "attributes": {
            "friendly_name": "EKZ Last Scrape",
            "icon": "mdi:clock-check-outline",
            "device_class": "timestamp",
        },
    }

    return states




def push_states(ha_url: str, ha_token: str, states: dict[str, dict]) -> None:
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    base = ha_url.rstrip("/")
    ok = 0
    for entity_id, payload in states.items():
        state_val = payload.get("state", "?")
        try:
            r = requests.post(
                f"{base}/api/states/{entity_id}",
                headers=headers,
                json=payload,
                timeout=_REST_TIMEOUT,
            )
            r.raise_for_status()
            ok += 1
            logger.debug("  ✓ %s = %s", entity_id, state_val)
        except Exception as exc:
            logger.warning("Failed to push %s: %s", entity_id, exc)
    logger.info("HA REST: pushed %d/%d sensors", ok, len(states))




async def _ws_inject_statistics(ha_url: str, ha_token: str, bills: list[dict]) -> None:
    import websockets  # lazy import — only needed here

    ws_url = (
        ha_url.rstrip("/")
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/api/websocket"
    )

    async with websockets.connect(ws_url, open_timeout=10) as ws:
        # Auth handshake
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required from HA WebSocket, got: {msg}")
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            logger.warning("WS auth failed: %s", msg)
            return

        # Build stat rows — one per bill, skipping rows with bad dates or amounts.
        cumulative = 0.0
        stat_rows = []
        for b in sorted(bills, key=lambda b: b.get("period_end", "")):
            try:
                amt = float(b["amount_chf"])
                start_dt = datetime.fromisoformat(b["period_end"]).replace(
                    hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
                )
            except (ValueError, TypeError, KeyError) as e:
                logger.warning(
                    "Skipping bill stat row (period_end=%r, amount_chf=%r): %s",
                    b.get("period_end"), b.get("amount_chf"), e,
                )
                continue
            cumulative = round(cumulative + amt, 2)
            stat_rows.append({
                "start": start_dt.isoformat(),
                "state": amt,
                "sum":   cumulative,
            })

        await ws.send(json.dumps({
            "id": 1,
            "type": "recorder/import_statistics",
            "metadata": {
                "statistic_id":       "ekz_power:monthly_cost_chf",
                "name":               "EKZ Monthly Cost",
                "source":             "ekz_power",
                "unit_of_measurement": "CHF",
                "has_mean": False,
                "has_sum":  True,
            },
            "stats": stat_rows,
        }))
        result = json.loads(await ws.recv())
        if result.get("success"):
            logger.info("WS: injected %d bill stat rows", len(stat_rows))
        else:
            logger.warning("WS statistics injection failed: %s", result)


def inject_bill_statistics(ha_url: str, ha_token: str, bills_csv: Path) -> None:
    bills = _read_bills(bills_csv)
    if not bills:
        return
    try:
        asyncio.run(_ws_inject_statistics(ha_url, ha_token, bills))
    except Exception as exc:
        logger.warning("WS statistics injection error: %s", exc)




def _collect_daily_kwh(csv_dir: Path) -> dict[str, float]:
    """Map each consumption day (YYYY-MM-DD) to its kWh value across all daily CSVs.

    Each daily CSV holds a full month of day-by-day rows, so values are keyed by
    the date parsed from the row's 'Zeitraum' column (DD.MM.YYYY) — not by the
    CSV filename. When several CSVs cover the same day the later (more complete)
    file wins, since glob results are processed in ascending filename order.
    """
    daily_values: dict[str, float] = {}
    for csv_file in sorted(csv_dir.glob("*_daily.csv")):
        for row in _read_csv(csv_file):
            zeitraum = row.get("Zeitraum", "").strip().split()  # strip weekday suffix
            val = _float(_kwh_col(row, "Gesamt [kWh]", "Verbrauch [kWh]"))
            if not zeitraum or val is None:
                continue
            try:
                dt = datetime.strptime(zeitraum[0], "%d.%m.%Y")
            except ValueError:
                continue
            daily_values[dt.strftime("%Y-%m-%d")] = val
    return daily_values


def _build_daily_kwh_stats(csv_dir: Path) -> list[dict]:
    """Return sorted cumulative stat rows for every day found across all daily CSVs."""
    daily_values = _collect_daily_kwh(csv_dir)

    if not daily_values:
        logger.debug("No daily kWh values found across all daily CSVs")
        return []

    logger.debug("Built %d daily kWh data points", len(daily_values))

    cumulative = 0.0
    stat_rows = []
    for date_key in sorted(daily_values):
        kwh = daily_values[date_key]
        cumulative = round(cumulative + kwh, 3)
        start_dt = datetime.strptime(date_key, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=ZoneInfo("Europe/Zurich")
        )
        stat_rows.append({
            "start": start_dt.isoformat(),
            "state": kwh,
            "sum":   cumulative,
        })
    return stat_rows


async def _ws_inject_daily_kwh(ha_url: str, ha_token: str, stat_rows: list[dict]) -> None:
    import websockets

    ws_url = (
        ha_url.rstrip("/")
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/api/websocket"
    )

    async with websockets.connect(ws_url, open_timeout=10) as ws:
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required from HA WebSocket, got: {msg}")
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            logger.warning("WS auth failed for daily kWh inject: %s", msg)
            return

        await ws.send(json.dumps({
            "id": 1,
            "type": "recorder/import_statistics",
            "metadata": {
                "statistic_id":        "ekz_power:daily_kwh",
                "name":                "EKZ Daily kWh",
                "source":              "ekz_power",
                "unit_of_measurement": "kWh",
                "has_mean": False,
                "has_sum":  True,
            },
            "stats": stat_rows,
        }))
        result = json.loads(await ws.recv())
        if result.get("success"):
            logger.info("WS: injected %d daily kWh stat rows", len(stat_rows))
        else:
            logger.warning("WS daily kWh injection failed: %s", result)


def inject_daily_kwh_statistics(ha_url: str, ha_token: str, csv_dir: Path) -> None:
    stat_rows = _build_daily_kwh_stats(csv_dir)
    if not stat_rows:
        logger.debug("No daily kWh data to inject")
        return
    try:
        asyncio.run(_ws_inject_daily_kwh(ha_url, ha_token, stat_rows))
    except Exception as exc:
        logger.warning("WS daily kWh injection error: %s", exc)




def _build_monthly_kwh_stats(csv_dir: Path) -> list[dict]:
    """Return stat rows for each calendar month from all monthly CSVs."""
    monthly_values: dict[tuple[int, int], float] = {}  # (year, month) -> kWh

    for csv_file in sorted(csv_dir.glob("*_monthly.csv")):
        for row in _read_csv(csv_file):
            ym = _parse_zeitraum_ym(row.get("Zeitraum", ""))
            if not ym:
                continue
            val_str = _kwh_col(row, "Gesamt [kWh]", "Verbrauch [kWh]")
            if not val_str:
                continue
            val = _float(val_str)
            if val is not None and val > 0:
                monthly_values[ym] = val

    if not monthly_values:
        logger.debug("No monthly kWh values found across all monthly CSVs")
        return []

    logger.debug("Built %d monthly kWh data points", len(monthly_values))

    cumulative = 0.0
    stat_rows = []
    for (year, month) in sorted(monthly_values):
        kwh = monthly_values[(year, month)]
        cumulative = round(cumulative + kwh, 3)
        start_dt = datetime(year, month, 1, 0, 0, 0, tzinfo=ZoneInfo("Europe/Zurich"))
        stat_rows.append({
            "start": start_dt.isoformat(),
            "state": kwh,
            "sum":   cumulative,
        })
    return stat_rows


async def _ws_inject_monthly_kwh(ha_url: str, ha_token: str, stat_rows: list[dict]) -> None:
    import websockets

    ws_url = (
        ha_url.rstrip("/")
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/api/websocket"
    )

    async with websockets.connect(ws_url, open_timeout=10) as ws:
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required from HA WebSocket, got: {msg}")
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            logger.warning("WS auth failed for monthly kWh inject: %s", msg)
            return

        await ws.send(json.dumps({
            "id": 1,
            "type": "recorder/import_statistics",
            "metadata": {
                "statistic_id":        "ekz_power:monthly_kwh",
                "name":                "EKZ Monthly kWh",
                "source":              "ekz_power",
                "unit_of_measurement": "kWh",
                "has_mean": False,
                "has_sum":  True,
            },
            "stats": stat_rows,
        }))
        result = json.loads(await ws.recv())
        if result.get("success"):
            logger.info("WS: injected %d monthly kWh stat rows", len(stat_rows))
        else:
            logger.warning("WS monthly kWh injection failed: %s", result)


def inject_monthly_kwh_statistics(ha_url: str, ha_token: str, csv_dir: Path) -> None:
    stat_rows = _build_monthly_kwh_stats(csv_dir)
    if not stat_rows:
        logger.debug("No monthly kWh data to inject")
        return
    try:
        asyncio.run(_ws_inject_monthly_kwh(ha_url, ha_token, stat_rows))
    except Exception as exc:
        logger.warning("WS monthly kWh injection error: %s", exc)


def _scale_kwh_stats_to_cost(kwh_rows: list[dict], cost_per_kwh: float) -> list[dict]:
    """Convert cumulative kWh stat rows into cumulative CHF cost rows.

    Uses the configured flat tariff, so cost == kWh × rate per period. The
    cumulative sum is recomputed from the per-period costs to stay internally
    consistent regardless of rounding.
    """
    cumulative = 0.0
    cost_rows: list[dict] = []
    for row in kwh_rows:
        period_cost = round(row["state"] * cost_per_kwh, 3)
        cumulative = round(cumulative + period_cost, 3)
        cost_rows.append({
            "start": row["start"],
            "state": period_cost,
            "sum":   cumulative,
        })
    return cost_rows


async def _ws_inject_cost_stats(
    ha_url: str, ha_token: str, statistic_id: str, name: str, stat_rows: list[dict]
) -> None:
    import websockets

    ws_url = (
        ha_url.rstrip("/")
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/api/websocket"
    )

    async with websockets.connect(ws_url, open_timeout=10) as ws:
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required from HA WebSocket, got: {msg}")
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            logger.warning("WS auth failed for cost inject (%s): %s", statistic_id, msg)
            return

        await ws.send(json.dumps({
            "id": 1,
            "type": "recorder/import_statistics",
            "metadata": {
                "statistic_id":        statistic_id,
                "name":                name,
                "source":              "ekz_power",
                "unit_of_measurement": "CHF",
                "has_mean": False,
                "has_sum":  True,
            },
            "stats": stat_rows,
        }))
        result = json.loads(await ws.recv())
        if result.get("success"):
            logger.info("WS: injected %d cost stat rows (%s)", len(stat_rows), statistic_id)
        else:
            logger.warning("WS cost injection failed (%s): %s", statistic_id, result)


def inject_daily_cost_statistics(
    ha_url: str, ha_token: str, csv_dir: Path, cost_per_kwh: float = _EST_COST_PER_KWH
) -> None:
    """Inject ekz_power:daily_cost_chf so estimated daily cost is chartable over time."""
    cost_rows = _scale_kwh_stats_to_cost(_build_daily_kwh_stats(csv_dir), cost_per_kwh)
    if not cost_rows:
        logger.debug("No daily cost data to inject")
        return
    try:
        asyncio.run(_ws_inject_cost_stats(
            ha_url, ha_token, "ekz_power:daily_cost_chf", "EKZ Daily Cost (estimated)", cost_rows
        ))
    except Exception as exc:
        logger.warning("WS daily cost injection error: %s", exc)


def inject_monthly_cost_statistics(
    ha_url: str, ha_token: str, csv_dir: Path, cost_per_kwh: float = _EST_COST_PER_KWH
) -> None:
    """Inject ekz_power:monthly_cost_kwh_chf (consumption × tariff).

    Companion to the bill-based ekz_power:monthly_cost_chf: this one is derived
    from metered kWh and the configured flat tariff, available even before bills
    arrive.
    """
    cost_rows = _scale_kwh_stats_to_cost(_build_monthly_kwh_stats(csv_dir), cost_per_kwh)
    if not cost_rows:
        logger.debug("No monthly cost data to inject")
        return
    try:
        asyncio.run(_ws_inject_cost_stats(
            ha_url, ha_token, "ekz_power:monthly_cost_kwh_chf",
            "EKZ Monthly Cost (estimated)", cost_rows
        ))
    except Exception as exc:
        logger.warning("WS monthly cost injection error: %s", exc)




def _push_health_sensors(ha_url: str, ha_token: str, data_dir: str) -> None:
    """Push scraper health and system sensors to HA."""
    import shutil
    status_file = Path(data_dir) / "status.json"
    
    # Read status.json
    status = {}
    scrape_age_hours = None
    if status_file.exists():
        try:
            status = json.loads(status_file.read_text())
            finished_at = status.get("finished_at") or status.get("timestamp")
            if finished_at:
                last_dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                scrape_age_hours = (datetime.now(tz=timezone.utc) - last_dt).total_seconds() / 3600
        except Exception as exc:
            logger.debug("Could not parse status.json for health sensors: %s", exc)
    
    # Disk space
    disk_free_mb = None
    disk_free_pct = None
    try:
        usage = shutil.disk_usage(data_dir)
        disk_free_mb = usage.free / (1024 * 1024)
        disk_free_pct = usage.free / usage.total * 100
    except OSError:
        pass
    
    health_sensors = {}
    
    # Scrape status sensor
    scrape_status = "ok" if status.get("success") else "failed" if status else "unknown"
    health_sensors["sensor.ekz_scrape_status"] = {
        "state": scrape_status,
        "attributes": {
            "friendly_name": "EKZ Scrape Status",
            "icon": "mdi:check-circle" if scrape_status == "ok" else "mdi:alert-circle",
            "last_error": status.get("error", ""),
            "attempts": status.get("attempts", 1),
            "phases": status.get("phases", {}),
        }
    }
    
    # Scrape age sensor
    if scrape_age_hours is not None:
        health_sensors["sensor.ekz_scrape_age_hours"] = {
            "state": round(scrape_age_hours, 1),
            "attributes": {
                "friendly_name": "EKZ Scrape Age",
                "unit_of_measurement": "h",
                "icon": "mdi:clock-outline",
                "device_class": "duration",
            }
        }
    
    # Data staleness sensor
    is_stale = scrape_age_hours is not None and scrape_age_hours > 30
    health_sensors["binary_sensor.ekz_data_stale"] = {
        "state": "on" if is_stale else "off",
        "attributes": {
            "friendly_name": "EKZ Data Stale",
            "device_class": "problem",
            "icon": "mdi:database-alert" if is_stale else "mdi:database-check",
        }
    }
    
    # Auth failure alarm
    has_auth_failure = status.get("permanent_failure", False)
    health_sensors["binary_sensor.ekz_auth_required"] = {
        "state": "on" if has_auth_failure else "off",
        "attributes": {
            "friendly_name": "EKZ Auth Required",
            "device_class": "problem",
            "icon": "mdi:account-alert" if has_auth_failure else "mdi:account-check",
            "action": status.get("action_required", ""),
        }
    }
    
    # Disk space sensors
    if disk_free_mb is not None:
        health_sensors["sensor.ekz_disk_free_mb"] = {
            "state": round(disk_free_mb, 0),
            "attributes": {
                "friendly_name": "EKZ Disk Free",
                "unit_of_measurement": "MB",
                "icon": "mdi:harddisk",
            }
        }
    
    if disk_free_pct is not None:
        health_sensors["sensor.ekz_disk_free_percent"] = {
            "state": round(disk_free_pct, 1),
            "attributes": {
                "friendly_name": "EKZ Disk Free %",
                "unit_of_measurement": "%",
                "icon": "mdi:harddisk",
            }
        }
    
    push_states(ha_url, ha_token, health_sensors)
    logger.debug("Pushed %d health sensors", len(health_sensors))


def _calculate_derived_costs(states: dict[str, dict], cost_per_kwh: float = _EST_COST_PER_KWH) -> dict[str, dict]:
    """Calculate and add derived cost sensors from existing data.

    Forward-looking estimates (daily / current-month) use the configured flat
    tariff ``cost_per_kwh``. The actual blended rate from bills is exposed
    separately as ``sensor.ekz_cost_per_kwh`` for comparison.
    """
    # Extract values from existing sensors
    latest_day_kwh = states.get("sensor.ekz_latest_day_kwh", {}).get("state")
    current_month_kwh = states.get("sensor.ekz_current_month_kwh", {}).get("state")
    ytd_kwh = states.get("sensor.ekz_year_to_date_kwh", {}).get("state")
    total_billed_chf = states.get("sensor.ekz_total_billed_chf", {}).get("state")
    
    # Try to parse as numbers (None is allowed, but empty string / "unavailable" is not)
    try:
        latest_day_kwh = float(latest_day_kwh) if latest_day_kwh not in (None, "", "unavailable") else None
    except (ValueError, TypeError):
        latest_day_kwh = None
    
    try:
        current_month_kwh = float(current_month_kwh) if current_month_kwh not in (None, "", "unavailable") else None
    except (ValueError, TypeError):
        current_month_kwh = None
    
    try:
        ytd_kwh = float(ytd_kwh) if ytd_kwh not in (None, "", "unavailable") else None
    except (ValueError, TypeError):
        ytd_kwh = None
    
    try:
        total_billed_chf = float(total_billed_chf) if total_billed_chf not in (None, "", "unavailable") else None
    except (ValueError, TypeError):
        total_billed_chf = None
    
    cost_sensors = {}

    # Actual blended rate from bills (total billed / YTD consumption).
    # Informational only — estimates below use the configured flat tariff.
    effective_rate = None
    if ytd_kwh is not None and ytd_kwh > 0 and total_billed_chf is not None:
        effective_rate = total_billed_chf / ytd_kwh
        cost_sensors["sensor.ekz_cost_per_kwh"] = {
            "state": round(effective_rate, 3),
            "attributes": {
                "friendly_name": "EKZ Cost per kWh",
                "unit_of_measurement": "CHF/kWh",
                "icon": "mdi:cash",
            }
        }

    # Daily cost estimate (configured flat tariff)
    if latest_day_kwh is not None:
        daily_cost = latest_day_kwh * cost_per_kwh
        cost_sensors["sensor.ekz_daily_cost_estimate"] = {
            "state": round(daily_cost, 2),
            "attributes": {
                "friendly_name": "EKZ Daily Cost Estimate",
                "unit_of_measurement": "CHF",
                "icon": "mdi:cash",
                "device_class": "monetary",
            }
        }

    # Current month cost estimate (configured flat tariff)
    if current_month_kwh is not None:
        month_cost = current_month_kwh * cost_per_kwh
        cost_sensors["sensor.ekz_current_month_cost_estimate"] = {
            "state": round(month_cost, 2),
            "attributes": {
                "friendly_name": "EKZ Current Month Cost Estimate",
                "unit_of_measurement": "CHF",
                "icon": "mdi:cash",
                "device_class": "monetary",
            }
        }
    
    # YTD cost (same as total billed)
    if total_billed_chf is not None:
        cost_sensors["sensor.ekz_ytd_cost"] = {
            "state": round(total_billed_chf, 2),
            "attributes": {
                "friendly_name": "EKZ Year-to-Date Cost",
                "unit_of_measurement": "CHF",
                "icon": "mdi:cash-multiple",
                "device_class": "monetary",
            }
        }
    
    return cost_sensors


def _calculate_rolling_averages(data_dir: str, cost_per_kwh: float = _EST_COST_PER_KWH) -> dict[str, float]:
    """Calculate 7d, 30d, 90d rolling average daily kWh and cost.

    Averages are taken over the most recent N *consumption* days (across all
    daily CSVs), not over CSV files — each daily CSV holds a whole month.
    """
    daily_values = _collect_daily_kwh(Path(data_dir) / "csv")
    if not daily_values:
        return {}

    recent_days = sorted(daily_values, reverse=True)  # newest consumption day first

    results: dict[str, float] = {}
    for window_days, suffix in [(7, "7d"), (30, "30d"), (90, "90d")]:
        window = recent_days[:window_days]
        if len(window) < window_days:
            logger.debug("Only %d days available for %s average (need %d)",
                         len(window), suffix, window_days)
        if window:
            avg_kwh = sum(daily_values[d] for d in window) / len(window)
            results[f"avg_{suffix}_kwh"] = round(avg_kwh, 2)
            results[f"avg_{suffix}_cost"] = round(avg_kwh * cost_per_kwh, 2)

    return results


def _calculate_projected_month_end(data_dir: str, cost_per_kwh: float = _EST_COST_PER_KWH) -> dict[str, float]:
    """Project month-end kWh and cost from the current month's daily average."""
    daily_values = _collect_daily_kwh(Path(data_dir) / "csv")
    if not daily_values:
        return {}

    now = datetime.now(tz=ZoneInfo("Europe/Zurich"))

    # Number of days in the current month.
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=ZoneInfo("Europe/Zurich"))
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=ZoneInfo("Europe/Zurich"))
    month_start = datetime(now.year, now.month, 1, tzinfo=ZoneInfo("Europe/Zurich"))
    days_in_month = (next_month - month_start).days

    prefix = f"{now.year:04d}-{now.month:02d}-"
    month_kwh = [v for d, v in daily_values.items() if d.startswith(prefix)]

    # Need at least 2 recorded days to make a reasonable projection.
    if len(month_kwh) < 2:
        return {}

    days_elapsed = len(month_kwh)
    avg_daily_kwh = sum(month_kwh) / days_elapsed
    projected_total_kwh = avg_daily_kwh * days_in_month
    projected_total_cost = projected_total_kwh * cost_per_kwh

    return {
        "projected_month_kwh": round(projected_total_kwh, 2),
        "projected_month_cost": round(projected_total_cost, 2),
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "daily_avg": round(avg_daily_kwh, 2),
    }


def _push_projected_sensors(ha_url: str, ha_token: str, data_dir: str, cost_per_kwh: float = _EST_COST_PER_KWH) -> None:
    """Push projected month-end sensors to Home Assistant."""
    projection = _calculate_projected_month_end(data_dir, cost_per_kwh)
    
    if not projection:
        logger.debug("No projection calculated (insufficient data)")
        return
    
    # Push projected kWh sensor
    kwh_payload = {
        "state": projection["projected_month_kwh"],
        "attributes": {
            "friendly_name": "Projected Month-End kWh",
            "unit_of_measurement": "kWh",
            "state_class": "measurement",
            "device_class": "energy",
            "icon": "mdi:chart-timeline-variant",
            "days_elapsed": projection["days_elapsed"],
            "days_in_month": projection["days_in_month"],
            "daily_average": projection["daily_avg"],
        },
    }
    
    # Push projected cost sensor
    cost_payload = {
        "state": projection["projected_month_cost"],
        "attributes": {
            "friendly_name": "Projected Month-End Cost",
            "unit_of_measurement": "CHF",
            "state_class": "measurement",
            "device_class": "monetary",
            "icon": "mdi:cash-clock",
            "days_elapsed": projection["days_elapsed"],
            "days_in_month": projection["days_in_month"],
            "daily_average_cost": round(projection["daily_avg"] * cost_per_kwh, 2),
        },
    }
    
    try:
        # Push kWh projection
        resp = requests.post(
            f"{ha_url}/api/states/sensor.ekz_projected_month_kwh",
            headers={"Authorization": f"Bearer {ha_token}"},
            json=kwh_payload,
            timeout=_REST_TIMEOUT,
        )
        resp.raise_for_status()
        logger.debug("Pushed sensor.ekz_projected_month_kwh: %.2f kWh", projection["projected_month_kwh"])
        
        # Push cost projection
        resp = requests.post(
            f"{ha_url}/api/states/sensor.ekz_projected_month_cost",
            headers={"Authorization": f"Bearer {ha_token}"},
            json=cost_payload,
            timeout=_REST_TIMEOUT,
        )
        resp.raise_for_status()
        logger.debug("Pushed sensor.ekz_projected_month_cost: %.2f CHF", projection["projected_month_cost"])
    except Exception as e:
        logger.warning("Failed to push projected sensors: %s", e)


def _push_rolling_average_sensors(ha_url: str, ha_token: str, data_dir: str, cost_per_kwh: float = _EST_COST_PER_KWH) -> None:
    """Push rolling average sensors (7d, 30d, 90d) to Home Assistant."""
    averages = _calculate_rolling_averages(data_dir, cost_per_kwh)
    
    if not averages:
        logger.debug("No rolling averages calculated (insufficient data)")
        return
    
    sensor_map = {
        "avg_7d_kwh": ("sensor.ekz_avg_7d_kwh", "7-Day Average kWh", "kWh"),
        "avg_30d_kwh": ("sensor.ekz_avg_30d_kwh", "30-Day Average kWh", "kWh"),
        "avg_90d_kwh": ("sensor.ekz_avg_90d_kwh", "90-Day Average kWh", "kWh"),
        "avg_7d_cost": ("sensor.ekz_avg_7d_cost", "7-Day Average Cost", "CHF"),
        "avg_30d_cost": ("sensor.ekz_avg_30d_cost", "30-Day Average Cost", "CHF"),
        "avg_90d_cost": ("sensor.ekz_avg_90d_cost", "90-Day Average Cost", "CHF"),
    }
    
    for key, (entity_id, friendly_name, unit) in sensor_map.items():
        value = averages.get(key)
        if value is None:
            continue
        
        payload = {
            "state": value,
            "attributes": {
                "friendly_name": friendly_name,
                "unit_of_measurement": unit,
                "state_class": "measurement",
                "device_class": "energy" if "kwh" in key else "monetary",
                "icon": "mdi:chart-line" if "kwh" in key else "mdi:currency-chf",
            },
        }
        
        try:
            resp = requests.post(
                f"{ha_url}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {ha_token}"},
                json=payload,
                timeout=_REST_TIMEOUT,
            )
            resp.raise_for_status()
            logger.debug("Pushed %s: %.2f %s", entity_id, value, unit)
        except Exception as e:
            logger.warning("Failed to push %s: %s", entity_id, e)


def push_to_ha(ha_url: str, ha_token: str, data_dir: str, cost_per_kwh: float = _EST_COST_PER_KWH) -> None:
    """Read all scraped data and push everything to Home Assistant."""
    if not ha_url or not ha_token:
        logger.debug("HA push disabled (ha_url / ha_token not configured)")
        return

    logger.info("Pushing data to Home Assistant (%s)", ha_url)

    csv_dir   = Path(data_dir) / "csv"
    bills_csv = Path(data_dir) / "bills" / "bills.csv"

    try:
        states = build_states(csv_dir, bills_csv)
        if states:
            push_states(ha_url, ha_token, states)
            # Push derived cost sensors
            cost_sensors = _calculate_derived_costs(states, cost_per_kwh)
            if cost_sensors:
                push_states(ha_url, ha_token, cost_sensors)
                logger.debug("Pushed %d derived cost sensors", len(cost_sensors))
        else:
            logger.warning("HA push: no sensor data available yet")
        inject_bill_statistics(ha_url, ha_token, bills_csv)
        inject_daily_kwh_statistics(ha_url, ha_token, csv_dir)
        inject_monthly_kwh_statistics(ha_url, ha_token, csv_dir)
        # Inject cost long-term statistics (chartable cost over time)
        inject_daily_cost_statistics(ha_url, ha_token, csv_dir, cost_per_kwh)
        inject_monthly_cost_statistics(ha_url, ha_token, csv_dir, cost_per_kwh)
        # Push health sensors
        _push_health_sensors(ha_url, ha_token, data_dir)
        # Push rolling average sensors
        _push_rolling_average_sensors(ha_url, ha_token, data_dir, cost_per_kwh)
        # Push projected month-end sensors
        _push_projected_sensors(ha_url, ha_token, data_dir, cost_per_kwh)
        # Track successful push
        _track_ha_push_result(data_dir, success=True, error="")
    except Exception as exc:
        logger.exception("HA push failed")
        _track_ha_push_result(data_dir, success=False, error=str(exc))
    finally:
        # Always push HA connectivity/health sensors
        try:
            _push_ha_connectivity_sensors(ha_url, ha_token, data_dir)
            logger.debug("Pushed HA connectivity sensors")
        except Exception:
            logger.debug("Could not push HA connectivity sensors")


def _track_ha_push_result(data_dir: str, *, success: bool, error: str = "") -> None:
    """Track HA push result for health metrics (success rate, last error)."""
    history_file = Path(data_dir) / "ha_push_history.json"
    
    # Load existing history
    history = []
    if history_file.exists():
        try:
            with history_file.open("r") as f:
                data = json.load(f)
                history = data.get("attempts", [])
        except Exception:
            logger.debug("Could not load HA push history, starting fresh")
    
    # Add new result
    history.append({
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "success": success,
        "error": error if not success else ""
    })
    
    # Keep last 100 attempts (covers ~3 months at daily scrape)
    history = history[-100:]
    
    # Write back
    try:
        tmp = history_file.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump({"attempts": history}, f, indent=2)
        tmp.replace(history_file)
    except Exception:
        logger.debug("Could not write HA push history")


def _push_ha_connectivity_sensors(ha_url: str, ha_token: str, data_dir: str) -> None:
    """Push sensors tracking HA connectivity and push health."""
    history_file = Path(data_dir) / "ha_push_history.json"
    
    # Default values
    push_status = "unknown"
    last_error = ""
    success_rate = 0.0
    connected = False
    
    if not ha_url or not ha_token:
        push_status = "disabled"
        last_error = "HA push not configured"
    else:
        # Test connectivity
        try:
            response = requests.get(
                f"{ha_url}/api/",
                headers={"Authorization": f"Bearer {ha_token}"},
                timeout=5
            )
            connected = response.status_code == 200
        except Exception:
            connected = False
        
        # Load push history
        history = []
        if history_file.exists():
            try:
                with history_file.open("r") as f:
                    data = json.load(f)
                    history = data.get("attempts", [])
            except Exception:
                pass
        
        if history:
            # Calculate success rate over last 7 days
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
            recent = [
                h for h in history
                if datetime.fromisoformat(h["timestamp"]) > cutoff
            ]
            if recent:
                successes = sum(1 for h in recent if h["success"])
                success_rate = (successes / len(recent)) * 100.0
            
            # Get last error and status
            last = history[-1]
            push_status = "ok" if last["success"] else "failed"
            last_error = last.get("error", "") if not last["success"] else ""
        else:
            # No history yet - check if we have connectivity at least
            push_status = "ok" if connected else "unknown"
    
    sensors = {
        "sensor.ekz_ha_push_status": {
            "state": push_status,
            "attributes": {
                "friendly_name": "EKZ HA Push Status",
                "icon": "mdi:cloud-upload" if push_status == "ok" else ("mdi:cloud-off-outline" if push_status == "failed" else "mdi:help-circle"),
            }
        },
        "sensor.ekz_ha_push_last_error": {
            "state": last_error[:255] if last_error else "none",
            "attributes": {
                "friendly_name": "EKZ HA Push Last Error",
                "icon": "mdi:alert-circle-outline",
            }
        },
        "binary_sensor.ekz_ha_connected": {
            "state": "on" if connected else "off",
            "attributes": {
                "friendly_name": "EKZ HA Connected",
                "device_class": "connectivity",
            }
        },
        "sensor.ekz_ha_push_success_rate": {
            "state": round(success_rate, 1),
            "attributes": {
                "friendly_name": "EKZ HA Push Success Rate (7d)",
                "icon": "mdi:percent",
                "unit_of_measurement": "%",
            }
        },
    }
    
    push_states(ha_url, ha_token, sensors)
    logger.debug("Pushed %d HA connectivity sensors", len(sensors))

