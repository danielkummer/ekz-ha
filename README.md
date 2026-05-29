# EKZ for Home Assistant

Automated daily scraper for the [myEKZ customer portal](https://my.ekz.ch/verbrauch/) - the self-service portal for [EKZ](https://www.ekz.ch/) electricity customers in Zurich. Downloads power consumption CSVs and invoice PDFs, then pushes the data as sensors into Home Assistant.

> **Why this exists:** EKZ does not offer a public API. The portal is a Vue SPA behind Keycloak OIDC login, so this tool uses Playwright (headless Chromium) to automate the download.

Disclaimer: unofficial, not affiliated with EKZ.

---

## 🚀 5-Minute Quickstart

**Get your EKZ data into Home Assistant in 5 minutes:**

```bash
# 1. Clone and configure (30 seconds)
git clone https://github.com/yourusername/ekz-ha.git && cd ekz-ha
cp config.yaml.example config.yaml
# Edit config.yaml: set username, password, address, ha_url, ha_token

# 2. Start the scraper (30 seconds)
docker compose up -d --build

# 3. Check logs and status (30 seconds)
docker compose logs -f
cat data/status.json

# 4. Verify in Home Assistant (3 minutes)
# Go to Settings → Devices & Services → Entities
# Filter for "ekz" - you should see ~30 sensors
```

**That's it!** The scraper runs daily at 06:00 and automatically pushes data to Home Assistant.

**Troubleshooting?** See the [3-Step Troubleshooting](#troubleshooting) section below.

---

## ☕ Support

If this saves you time or you just enjoy having your electricity data in Home Assistant, a small tip is always appreciated!

[![PayPal](https://img.shields.io/badge/PayPal-tip_the_author-00457C?logo=paypal&logoColor=white)](https://paypal.me/danikummer)

---

## Architecture

There are two ways data reaches Home Assistant - use one or both:

```
┌─────────────────────────┐
│  myEKZ portal           │  https://my.ekz.ch
│  (Keycloak OIDC login)  │
└────────────┬────────────┘
             │  Playwright/Chromium (daily)
             ▼
┌─────────────────────────┐
│  ekz-scraper container  │  Docker on any Linux host
│  (this repo)            │
└──────┬──────────────────┘
       │ writes CSV + PDF
       ▼
┌─────────────────────────┐     ┌──────────────────────────────┐
│  ./data/                │     │  Home Assistant              │
│   csv/*.csv             │ ──► │  ekz_power custom component  │  pull-based
│   bills/*.pdf           │     │  (reads CSV files directly)  │
│   screenshots/*.png     │     └──────────────────────────────┘
└─────────────────────────┘
       │ REST + WebSocket
       ▼
┌──────────────────────────────┐
│  Home Assistant              │
│  sensor.ekz_* entities       │  push-based (recommended)
└──────────────────────────────┘
```

**Path A - Direct push (recommended):** After each scrape, the container pushes all sensor states and long-term statistics directly to HA via its REST and WebSocket APIs. No shared filesystem needed - just set `home_assistant.url` and `home_assistant.token` in `config.yaml`.

**Path B - Custom component:** The included `homeassistant/custom_components/ekz_power` component reads the dated CSV files from a directory accessible to HA (local path, NFS mount, or rsync target) and exposes the same sensors. Use this if you prefer a pull-based setup or can't use the push API.

---

## Prerequisites

- **Docker + Docker Compose** installed on the host that will run the scraper
- A **myEKZ account** (https://my.ekz.ch - requires an EKZ electricity contract)
- A **Home Assistant instance** (for Path A or B above) - optional but recommended
- Any Linux host for the scraper: Raspberry Pi, NAS, VPS, or your desktop machine

---

## Quick start

### 1. Configure

Copy the example and fill in your credentials:

```bash
cp config.yaml.example config.yaml
# edit config.yaml - at minimum: ekz.username, ekz.password, ekz.address
```

The minimum required keys are `ekz.username`, `ekz.password`, and `ekz.address`. Everything else has a sensible default. See [Configuration](#configuration) for all options.

### 2. Build and start

```bash
docker compose up -d --build
```

View live logs:
```bash
docker compose logs -f
```

The scraper runs once immediately on startup, then daily at the configured `scrape_time`.

### 3. Data location

Output lands in `./data/` (edit the `volumes:` entry in `docker-compose.yml` to change this):

```
data/
├── csv/
│   ├── YYYY-MM-DD_daily.csv      # current month, day-by-day
│   ├── YYYY-MM-DD_monthly.csv    # current year, month-by-month
│   └── YYYY-MM-DD_yearly.csv     # multi-year overview
├── bills/
│   ├── YYYY-MM-DD_to_YYYY-MM-DD.pdf  # invoice PDFs (kept forever - never auto-deleted)
│   └── bills.csv                     # aggregated invoice data
├── screenshots/
│   └── YYYY-MM-DD_chart_*.png    # chart screenshots (auto-deleted after 30 days by default)
├── status.json                   # last run health status (timestamp, success, error)
└── debug/                        # browser snapshots (only written at DEBUG log level)
```

Files are prefixed with the scrape date. Re-running on the same day is safe - existing files are skipped. Old CSV and screenshot files are removed automatically (see `retention` in [Configuration](#configuration)).

**`status.json`** is written after every run (success or failure) and contains:
```json
{
  "timestamp": "2026-05-27T17:55:34.943272+00:00",
  "success": true
}
```
On failure it includes `"error": "..."` with details. For permanent failures (wrong credentials, account locked) it also sets `"permanent_failure": true`. Monitor this file externally or expose it via a Home Assistant file sensor for alerts.

---

## Configuration

All settings live in `config.yaml`. The only environment variable honoured is `EKZ_CONFIG_FILE`, which overrides the path to `config.yaml` (default: `./config.yaml`).

| YAML key | Default | Description |
|---|---|---|
| `ekz.username` | *(required)* | myEKZ login email |
| `ekz.password` | *(required)* | myEKZ password |
| `ekz.address` | *(required)* | Substring of your meter label as shown in the portal. Any unique fragment works - e.g. `"Main Street"` matches `"Main Street 42, 1st floor"`. |
| `ekz.scrape_time` | `06:00` | Daily run time, HH:MM in Europe/Zurich timezone |
| `ekz.headless` | `true` | Set `false` to watch the browser (desktop only; useful for debugging) |
| `ekz.max_retries` | `3` | Retry attempts on transient failures (network errors, portal timeouts) before giving up |
| `ekz.retry_backoff_base_minutes` | `15` | Base delay for exponential backoff. Actual delay is doubled each retry with random jitter added (15–30 min first retry, 30–60 min second, etc.) |
| `ekz.data_dir` | `/app/data` | Data directory inside the container (rarely needs changing) |
| `home_assistant.url` | *(empty)* | Home Assistant base URL - enables sensor push when set |
| `home_assistant.token` | *(empty)* | HA Long-Lived Access Token |
| `rsync.target` | *(empty)* | `user@host:/path` - push `data/` after each scrape; empty disables |
| `log_level` | `INFO` | Log verbosity: `INFO` for normal operation, `DEBUG` to also write browser screenshots and HTML dumps to `data/debug/` |
| `retention.csv_days` | `90` | Days to keep dated CSV files (0 = keep forever) |
| `retention.screenshot_days` | `30` | Days to keep chart screenshots (0 = keep forever) |

---

## Home Assistant integration

### Path A - Direct push (recommended)

Set `home_assistant.url` and `home_assistant.token` in your config. After each scrape the container pushes all sensor states via the HA REST API and injects long-term statistics via WebSocket. No NFS mount or custom component needed.

Create a Long-Lived Access Token in HA at **Profile → Security → Long-Lived Access Tokens**.

**Sensors created:**

| Entity | Description |
|---|---|
| `sensor.ekz_latest_day_kwh` | Most recent day total (kWh) |
| `sensor.ekz_latest_day_ht_kwh` | Same day - HT / peak (kWh) - `unavailable` with new portal format |
| `sensor.ekz_latest_day_nt_kwh` | Same day - NT / off-peak (kWh) - `unavailable` with new portal format |
| `sensor.ekz_current_month_kwh` | Current calendar month accumulated (kWh) |
| `sensor.ekz_year_to_date_kwh` | Year-to-date sum from monthly CSV (kWh) |
| `sensor.ekz_latest_bill_chf` | Most recent invoice amount (CHF) |
| `sensor.ekz_total_billed_chf` | Sum of all known invoices (CHF) |
| `sensor.ekz_last_scrape` | Timestamp of last successful push |

**Long-term statistics injected** (power/cost `statistics-graph` chart cards):

| Statistic ID | Description |
|---|---|
| `ekz_power:daily_kwh` | Daily kWh consumption (one row per calendar day) |
| `ekz_power:monthly_kwh` | Monthly kWh consumption (one row per month) |
| `ekz_power:monthly_cost_chf` | Per-bill CHF amount + running cumulative sum |
| `ekz_power:daily_cost_chf` | Estimated daily cost (daily kWh × your tariff) |
| `ekz_power:monthly_cost_kwh_chf` | Estimated monthly cost (monthly kWh × your tariff) |

The two estimated-cost statistics use the flat `tariff.cost_per_kwh` from your
config (default `0.25` CHF/kWh) so cost is chartable over time even before any
bills arrive. See [Visualizing in Home Assistant](#visualizing-in-home-assistant).

---

### Path B - Custom component (pull-based)

A custom HA component (`homeassistant/custom_components/ekz_power`) reads the dated CSV files from a directory accessible to HA and exposes the same seven sensors.

Use this if you prefer HA to control the refresh cycle, or if direct API access isn't available.

See [`homeassistant/README.md`](homeassistant/README.md) for installation instructions, volume-mount options, and ready-to-paste Lovelace card examples.

---

## Visualizing in Home Assistant

The repo ships ready-to-use dashboard templates — all genericized, with no
personal entities:

- **[`homeassistant/dashboards/ekz-energy.yaml`](homeassistant/dashboards/ekz-energy.yaml)** —
  a complete importable *sections* view: at-a-glance tiles, a consumption gauge,
  monthly/daily kWh charts, a **cost-over-time** chart, and a data-freshness /
  scraper-health row. Uses **core cards only** (no HACS required).
- **[`homeassistant/lovelace_cards.yaml`](homeassistant/lovelace_cards.yaml)** —
  individual copy-paste card snippets, plus a clearly separated **Optional: HACS**
  section (apexcharts dual-axis kWh + CHF, mushroom summary) and an example for
  combining the EKZ total with your own smart-plug sensors.

**Import the dashboard view:**

1. In HA, open the dashboard you want to add it to → top-right **⋮ → Edit
   Dashboard → ⋮ → Raw configuration editor**.
2. Paste the contents of `ekz-energy.yaml` as a new entry under `views:`.
3. Save. (Entity IDs assume the recommended **direct push** setup; rename to the
   `sensor.ekz_*` IDs you actually have if you only run the custom component.)

**Set your tariff** so the cost charts are accurate: edit `tariff.cost_per_kwh`
in `config.yaml` (find your rate on a recent invoice or the
[EKZ tariff table](https://www.ekz.ch/de/angebote/strom/tarife/stromtarife-privatkunden.html),
"Total" row in Rp./kWh ÷ 100).

**Energy Dashboard:** EKZ sensors aren't real-time pulses, so they don't plug
directly into HA's Energy Dashboard grid. The `homeassistant/README.md` shows a
Utility Meter helper recipe if you want them there anyway; otherwise the shipped
`statistics-graph` cards cover day/month/cost trends natively.

---

## Remote Home Assistant (scraper and HA on separate machines)

If the scraper runs on one host and Home Assistant on another, the direct push (Path A) works over the network with no extra setup. For the custom component (Path B), you need to share the `data/` directory:

### rsync push (built-in)

The scraper can automatically rsync `data/` to a remote host after each run.

**1. Generate a dedicated SSH key on the scraper host:**
```bash
mkdir -p ssh
ssh-keygen -t ed25519 -f ssh/id_ed25519 -N ""
```

**2. Authorise the key on the HA host:**
```bash
ssh-copy-id -i ssh/id_ed25519.pub user@ha-host
```

**3. Set the rsync target in your config:**
```yaml
# config.yaml
rsync:
  target: "user@ha-host:/media/ekz-ha"
```

The SSH key volume in `docker-compose.yml` is pre-commented - just uncomment it:
```yaml
volumes:
  - ./config.yaml:/app/config.yaml:ro
  - ./data:/app/data
  - ./ssh/id_ed25519:/root/.ssh/id_ed25519:ro  # ← uncomment this line
```

> `ssh/` is gitignored - never commit private keys.

### NFS mount

Export `data/` from the scraper host via NFS and mount it on the HA host as `/media/ekz-ha`. No code changes required.

### Syncthing

Add a Syncthing sidecar to `docker-compose.yml` and pair it with the [Syncthing HA add-on](https://github.com/Poeschl/Hassio-Addons). Useful for setups across different networks.

---

## Historical data backfill

On first install, you probably want all available historical data, not just today's consumption. The `scripts/backfill_historical.py` tool automates this:

```bash
# Backfill all available history (typically 12-24 months)
docker compose run --rm ekz-scraper python scripts/backfill_historical.py

# Or limit to last 6 months
docker compose run --rm ekz-scraper python scripts/backfill_historical.py --max-months 6

# Skip daily data (faster, less storage)
docker compose run --rm ekz-scraper python scripts/backfill_historical.py --skip-daily
```

**What it does:**
- Logs into the EKZ portal
- Downloads current period's CSV
- Clicks "previous period" button
- Repeats until no more history available
- Saves all files to `data/csv/` and `data/screenshots/`

**Performance:** ~10-15 minutes for 1 year of data (daily + monthly + yearly).

**After backfill:** Restart the main scraper to push all historical data to Home Assistant:
```bash
docker compose restart
```

See `scripts/README.md` for full documentation.

---

## Running locally (without Docker)

Useful for development or testing selector changes on a desktop machine:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp config.yaml.example config.yaml   # fill in credentials
python -m scraper.main
```

Set `headless: false` in `config.yaml` to watch the browser.

---

## Updating selectors after a portal change

If EKZ redesigns their portal, the scraper may fail to find buttons or fields. All CSS selectors are in the `SELECTORS` dict at the top of `scraper/scraper.py`. Update only that dict - no other changes are needed.

Debug screenshots are saved to `data/debug/` at every major step, making it straightforward to diagnose exactly where the automation broke without running in headed mode.

Set `headless: false` on a desktop machine (not a headless Pi) to watch the browser in real time and identify the correct selectors.

---

## Troubleshooting

### 🔧 3-Step Troubleshooting Checklist

**90% of issues can be solved with these 3 checks:**

1. **Check the logs**
   ```bash
   docker compose logs -f --tail=100
   ```
   Look for errors mentioning "LOGIN", "SELECTOR", "TIMEOUT", or "CONFIG"

2. **Verify your config**
   ```bash
   docker compose run --rm ekz-scraper python -m scraper.main --check-config
   ```
   This validates all settings and shows what's configured

3. **Check the status file**
   ```bash
   cat data/status.json
   ```
   Shows last scrape result, duration, and which phases succeeded/failed

### Common Issues

**No data after first run / empty charts in HA**
- Check logs: `docker compose logs -f`
- Set `log_level: "DEBUG"` in `config.yaml` and restart - this writes browser screenshots and HTML to `data/debug/` at every step so you can see exactly where automation stopped
- Confirm `ekz.address` matches your meter label (substring match - try a shorter fragment)

**Scraper retries but never succeeds**
- Check `data/status.json` for the last error
- If `"permanent_failure": true`, the issue is with credentials or account status - verify `ekz.username` and `ekz.password`
- Transient errors (network, portal timeout) are automatically retried with exponential backoff up to `ekz.max_retries` times

**Config file not found on startup**
- The container expects `config.yaml` mounted at `/app/config.yaml` - check the `volumes:` section in `docker-compose.yml` and that `./config.yaml` exists on the host

**HT/NT sensors show `unavailable`**
- Expected. The current EKZ portal CSV format exports a single `Verbrauch [kWh]` column with no peak/off-peak split. HT/NT are only available in older historical data on the yearly tab

**Scraper fails with locale error inside the container**
- The Dockerfile sets `LANG=en_US.UTF-8` explicitly. If you're building a custom image, ensure this is set. The Pi host's `LANG=en_US@posix` causes `RangeError: Invalid language tag` inside the portal's Vue app

**HA is unreachable when scrape completes**
- The scraper logs a warning and continues. Statistics from this run won't be pushed, but they will be included the next time `push_to_ha` runs successfully (it re-reads all CSV files each time)

**Scraper loops but never downloads / chart never loads**
- The portal's `<tr>` rows have zero CSS height, which can confuse some CSS inspector tools. The code uses a `<td>`-to-parent-`<tr>` click strategy with `force=True` to bypass this. Check `data/debug/` screenshots (requires `log_level: "DEBUG"`) for the current state

**"Login failed" or "Authentication error"**
- Verify credentials in `config.yaml`
- Test login manually at https://my.ekz.ch
- If you changed your password, update `config.yaml` and restart: `docker compose restart`

**"No sensors appearing in Home Assistant"**
- Check `ha_url` and `ha_token` are set in `config.yaml`
- Verify token is valid: go to HA → Settings → People → Long-lived access tokens
- Test connectivity: `curl -H "Authorization: Bearer YOUR_TOKEN" http://HA_IP:8123/api/`
- Check `data/status.json` for `ha_push_successful: true`

---

## Limitations

- Relies on web scraping: if EKZ changes the portal layout, selectors may need updating (see [Updating selectors](#updating-selectors-after-a-portal-change))
- Data freshness is limited to once per day (the portal itself does not offer real-time data)
- HT/NT (peak/off-peak) data is no longer exported by the current portal format for daily/monthly views
- Only a single meter/apartment per container instance is supported

---

## License

MIT - see [LICENSE](LICENSE).
