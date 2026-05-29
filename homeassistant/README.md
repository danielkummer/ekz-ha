# Home Assistant Integration

There are two ways EKZ data reaches Home Assistant (see the top-level README for
the full comparison):

- **Direct push (recommended):** the scraper pushes everything to HA via the
  REST + WebSocket APIs. No custom component needed. This exposes the **full**
  `sensor.ekz_*` set (energy, bills, cost estimates, rolling averages,
  projections, and scraper-health sensors) plus five long-term statistics.
- **Custom component (`ekz_power`, pull-based):** HA reads the CSV files itself
  and exposes the **seven core sensors** below plus the
  `ekz_power:monthly_cost_chf` statistic.

This document covers the custom-component install and the Lovelace cards that
work with **either** path.

## Sensors (custom component — seven core sensors)

| Entity | Description | Unit | state_class |
|---|---|---|---|
| `sensor.ekz_latest_day_kwh` | Most recent day with data (total) | kWh | measurement |
| `sensor.ekz_latest_day_ht_kwh` | Same day - HT (peak) - see note below | kWh | measurement |
| `sensor.ekz_latest_day_nt_kwh` | Same day - NT (off-peak) - see note below | kWh | measurement |
| `sensor.ekz_current_month_kwh` | Current calendar month accumulated | kWh | total_increasing |
| `sensor.ekz_year_to_date_kwh` | Year-to-date (sum of all months in CSV) | kWh | total |
| `sensor.ekz_latest_bill_chf` | Most recent invoice total | CHF | measurement |
| `sensor.ekz_total_billed_chf` | Sum of all known invoices | CHF | total |

> The direct-push path adds many more entities (cost estimates, 7/30/90-day
> rolling averages, month-end projection, and scraper-health sensors such as
> `sensor.ekz_scrape_status`, `sensor.ekz_scrape_age_hours`,
> `binary_sensor.ekz_data_stale`, `sensor.ekz_ha_push_success_rate`). They are
> listed in the top-level README.

> **HT/NT sensors:** The new EKZ portal CSV format exports a single `Verbrauch [kWh]`
> column with no HT/NT (peak/off-peak) split. `ekz_latest_day_ht_kwh` and
> `ekz_latest_day_nt_kwh` show `unavailable` when the daily CSV uses this format.
> The yearly CSV still contains HT/NT breakdown for historical reference.

All energy sensors carry `data_date` and `source_file` attributes.
Bill sensors carry `period_start`, `period_end`, `type`, `address`, and `pdf_file` attributes.
`sensor.ekz_total_billed_chf` also exposes `monthly_costs` — a list of
`{period_start, period_end, amount_chf, cumulative_chf, type}` dicts, ordered
chronologically with running totals pre-computed.

### Long-term statistics (for chart cards)

The custom component injects one external statistic into the HA recorder:

| Statistic ID | `state` | `sum` |
|---|---|---|
| `ekz_power:monthly_cost_chf` | Per-bill CHF amount | Running cumulative CHF |

The **direct-push** path injects five statistics, so cost and consumption are
both chartable over time:

| Statistic ID | `state` | Notes |
|---|---|---|
| `ekz_power:daily_kwh` | Daily kWh | one row per calendar day |
| `ekz_power:monthly_kwh` | Monthly kWh | one row per month |
| `ekz_power:monthly_cost_chf` | Per-bill CHF | bill-based, cumulative sum |
| `ekz_power:daily_cost_chf` | Daily kWh × tariff | estimated daily cost |
| `ekz_power:monthly_cost_kwh_chf` | Monthly kWh × tariff | estimated monthly cost |

The two estimated-cost statistics use the flat `tariff.cost_per_kwh` from
`config.yaml` (default `0.25` CHF/kWh).

These are upserted on every poll cycle — new bills added remotely are picked up
automatically within one `scan_interval` (default 1 hour), no HA restart needed.

## Data update flow

All sensors share a single `DataUpdateCoordinator`. On each poll cycle it:
1. Reads all CSV files **once** (in a thread — safe for NFS / slow storage)
2. Injects/updates long-term statistics in the HA recorder
3. Notifies all sensors simultaneously

This means: if the scraper Pi rsyncs new files to the HA Pi, HA will pick up
the new data and refresh all charts within one `scan_interval`.

## Installation

### 1. Copy the custom component

```bash
cp -r custom_components/ekz_power  <your-ha-config>/custom_components/
```

### 2. Share the CSV volume with Home Assistant

The scraper writes CSVs to `./data/csv/`. HA needs to read the same path.

**Option A - HA running in Docker on the same Pi:**

Add a volume to your HA Docker run command or `docker-compose.yml`:
```yaml
# in your HA docker-compose.yml
volumes:
  - /path/to/ekz-ha/data/csv:/media/ekz-ha/csv:ro
```

**Option B - Home Assistant OS (HAOS):**

1. Install the **Samba** or **SSH** add-on
2. Copy the CSV files into `/media/ekz-ha/csv/` on the HA instance
   (or mount the share from the Pi that runs the scraper)
3. If the Pi and HAOS device are different machines, use a cron job or rsync
   to push new CSVs to the HAOS media folder after each scrape:
   ```bash
   rsync -a /path/to/ekz-ha/data/csv/ ha-host:/media/ekz-ha/csv/
   ```

### 3. Add to configuration.yaml

```yaml
sensor:
  - platform: ekz_power
    data_dir: /media/ekz-ha/csv
    bills_dir: /media/ekz-ha/bills   # optional; defaults to ../bills relative to data_dir
    scan_interval: 3600
```

### 4. Restart Home Assistant

After restarting, the sensors appear under **Settings > Devices & Services >
Entities** filtered by "EKZ".

## Dashboard Cards

See [`lovelace_cards.yaml`](lovelace_cards.yaml) for copy-paste card configs:

| Card | What it shows |
|---|---|
| 1. Entities card | Daily / monthly / yearly kWh + bills at a glance |
| 2. Statistics-graph bar | **Monthly bill amounts (CHF)** — one bar per invoice |
| 3. Statistics-graph line | **Cumulative YTD cost (CHF)** — running total line |
| 4. Statistics-graph bar | Monthly kWh usage |
| 5. Gauge | Current month vs. reference |
| 6. Bill summary | Latest bill + total |
| 7. Markdown | Data freshness |
| 8. apexcharts-card | Combined bar + line on one chart (requires HACS) |

Cards 2 and 3 use the `ekz_power:monthly_cost_chf` statistic injected by the
component — no extra configuration needed beyond installing the component.

Card 8 uses the `monthly_costs` attribute on `sensor.ekz_total_billed_chf` and
requires the [apexcharts-card](https://github.com/RomRider/apexcharts-card) from
HACS. It overlays per-bill bars and a cumulative line on a single chart.

## Notes

- `ekz_current_month_kwh` uses `state_class: total_increasing` (resets each month).
  `ekz_year_to_date_kwh` and `ekz_total_billed_chf` use `state_class: total` (can
  decrease if estimated values are revised to actuals).
- These sensors are **not** real-time counters, so they won't integrate natively
  into HA's Energy Dashboard grid view (which expects live kWh pulses). They work
  well with tile cards, statistics-graph cards, and apexcharts-card.
- Data is as fresh as the last scraper run (default: daily at 06:00).
- If the scraper is configured with `home_assistant.url` / `home_assistant.token`
  in `config.yaml`, it pushes the sensors directly via REST after each run. The
  custom component and the direct push both inject statistics with `hour=0 UTC`
  timestamps so HA upserts rather than creating duplicates.

## Energy Dashboard Integration

While EKZ sensors can't be added directly to the Energy Dashboard grid (they're not real-time pulses), you can use **Utility Meter** helpers to create daily/monthly energy counters that work with the dashboard.

### Method 1: Using Utility Meter (Recommended)

Create a utility meter helper that converts the EKZ data into HA Energy Dashboard compatible format:

```yaml
# configuration.yaml
utility_meter:
  ekz_daily_energy:
    source: sensor.ekz_current_month_kwh
    cycle: daily
    name: EKZ Daily Energy
  
  ekz_monthly_energy:
    source: sensor.ekz_year_to_date_kwh
    cycle: monthly
    name: EKZ Monthly Energy
```

After restarting HA, go to:
1. **Settings → Dashboards → Energy**
2. Click **Add Consumption**
3. Select `sensor.ekz_daily_energy` or `sensor.ekz_monthly_energy`
4. Set cost to `0.25` CHF/kWh (or your actual rate)

### Method 2: Using Template Sensors

Create template sensors with `state_class: total_increasing`:

```yaml
# configuration.yaml
template:
  - sensor:
      - name: "EKZ Energy Consumption"
        unique_id: ekz_energy_consumption
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
        state: "{{ states('sensor.ekz_current_month_kwh') | float(0) }}"
```

Then add to Energy Dashboard:
1. **Settings → Dashboards → Energy**
2. **Add Consumption** → Select `sensor.ekz_energy_consumption`
3. Set your electricity cost

### Method 3: Direct Statistics Import (Advanced)

The scraper pushes long-term statistics via WebSocket. Check if statistics are visible:

```yaml
# In Developer Tools → Statistics
# Search for: ekz_power:daily_kwh
```

You can reference these statistic IDs directly in `statistics-graph` cards:

```yaml
type: statistics-graph
title: Daily consumption
entities:
  - ekz_power:daily_kwh
chart_type: bar
period: day
stat_types:
  - state
```

### Limitations

- EKZ data is scraped **once daily** (not real-time)
- Historical backfill will create statistics, but Energy Dashboard may not show them retroactively
- For real-time monitoring, consider adding a Shelly EM or similar smart meter that measures at your circuit breaker

## Combined kWh/CHF Visualization

Dual-axis chart showing both energy consumption (kWh) and cost (CHF) on one graph:

```yaml
type: custom:apexcharts-card
title: Energy & Cost
graph_span: 6mo
header:
  show: true
series:
  - entity: sensor.ekz_current_month_kwh
    name: Consumption (kWh)
    type: column
    yaxis_id: kwh
    color: '#1f77b4'
    statistics:
      type: sum
      period: month
  - entity: sensor.ekz_current_month_cost_estimate
    name: Cost (CHF)
    type: line
    yaxis_id: cost
    color: '#ff7f0e'
    stroke_width: 3
    statistics:
      type: sum
      period: month
yaxis:
  - id: kwh
    apex_config:
      title:
        text: kWh
  - id: cost
    opposite: true
    apex_config:
      title:
        text: CHF
```

**Requirements:** Install [apexcharts-card](https://github.com/RomRider/apexcharts-card) via HACS.

**Alternative (no custom card needed):**
```yaml
type: history-graph
title: Energy & Cost
entities:
  - entity: sensor.ekz_current_month_kwh
  - entity: sensor.ekz_current_month_cost_estimate
hours_to_show: 720  # 30 days
```

## Additional Dashboard Examples

### Health Monitoring Card

Monitor scraper and HA push health:

```yaml
type: entities
title: EKZ Scraper Health
entities:
  - entity: sensor.ekz_scrape_status
  - entity: sensor.ekz_scrape_age_hours
  - entity: binary_sensor.ekz_data_stale
  - entity: binary_sensor.ekz_auth_required
  - entity: binary_sensor.ekz_ha_connected
  - entity: sensor.ekz_ha_push_status
  - entity: sensor.ekz_ha_push_success_rate
```

> These scraper-health sensors are only available with the **direct-push** path.

### Cost Overview Card

```yaml
type: entities
title: Cost Overview
entities:
  - entity: sensor.ekz_cost_per_kwh
  - entity: sensor.ekz_daily_cost_estimate
  - entity: sensor.ekz_current_month_cost_estimate
  - entity: sensor.ekz_ytd_cost
  - entity: sensor.ekz_latest_bill_chf
  - entity: sensor.ekz_total_billed_chf
```

### 7-Day Trend Card

```yaml
type: history-graph
title: Last 7 Days
entities:
  - entity: sensor.ekz_latest_day_kwh
hours_to_show: 168
refresh_interval: 3600
```

### Actionable Automations

**Cost Spike Alert:**
```yaml
alias: EKZ Cost Spike Alert
trigger:
  - platform: numeric_state
    entity_id: sensor.ekz_daily_cost_estimate
    above: 10  # CHF threshold
action:
  - service: notify.mobile_app
    data:
      title: "High Energy Cost Today"
      message: "Daily cost is {{ states('sensor.ekz_daily_cost_estimate') }} CHF"
```

**Stale Data Warning:**
```yaml
alias: EKZ Stale Data Warning
trigger:
  - platform: state
    entity_id: binary_sensor.ekz_data_stale
    to: "on"
action:
  - service: notify.mobile_app
    data:
      title: "EKZ Scraper Issue"
      message: "Data stale. Last scrape: {{ states('sensor.ekz_scrape_age_hours') }}h ago"
```

