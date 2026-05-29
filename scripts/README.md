# EKZ Scraper Scripts

This directory contains helper scripts for the ekz-ha project.

---

## doctor.py - Setup Doctor & Bootstrap Helper

Interactive tool that checks prerequisites, validates config, and helps with first-time setup.

**Usage:**
```bash
python3 scripts/doctor.py
```

**What it checks:**
- ✓ Docker and Docker Compose installation
- ✓ Docker daemon running
- ✓ config.yaml exists (offers to create from example)
- ✓ Configuration validity (uses `--check-config`)
- ✓ Home Assistant connectivity (if configured)
- ✓ data/ directory exists and is writable
- ✓ SSH key for rsync (optional, offers to generate)

**Interactive features:**
- Offers to create config.yaml from example if missing
- Offers to create data/ directory if missing
- Offers to generate SSH key for rsync if desired
- Tests Home Assistant connectivity with actual API call

---

## Historical Backfill (`backfill_historical.py`)

Automatically downloads all available historical consumption data from the EKZ portal.

### Purpose

Run this **once** after initial setup to populate your Home Assistant with historical data. The main scraper will then handle daily updates going forward.

### Features

- ✅ **Fully automated** - navigates through all previous periods
- ✅ **Idempotent** - safe to re-run (skips existing files by timestamp)
- ✅ **All period types** - daily, monthly, and yearly data
- ✅ **Polite** - respects rate limits with delays
- ✅ **Progress tracking** - shows what's being downloaded
- ✅ **Resume-friendly** - can be interrupted and re-run

### Usage

```bash
# Backfill all available history (default)
python scripts/backfill_historical.py

# Limit to last 12 months
python scripts/backfill_historical.py --max-months 12

# Skip daily data (faster, less storage)
python scripts/backfill_historical.py --skip-daily

# Verbose logging
python scripts/backfill_historical.py --verbose
```

### Docker Usage

```bash
# Run inside the container
docker compose exec ekz-scraper python scripts/backfill_historical.py

# Or as a one-off command
docker compose run --rm ekz-scraper python scripts/backfill_historical.py --max-months 12
```

### What It Does

1. **Logs into EKZ portal** using credentials from `config.yaml`
2. **Navigates to consumption page** and selects your meter
3. **For each period type** (daily → monthly → yearly):
   - Downloads current period CSV + screenshot
   - Clicks "previous period" button
   - Repeats until no more history available
4. **Saves all files** with timestamped names to `data/csv/` and `data/screenshots/`

### Output

Files are saved as:
```
data/
  csv/
    2026-05-27_183045_daily_backfill_000.csv
    2026-05-27_183102_daily_backfill_001.csv
    ...
    2026-05-27_184522_monthly_backfill_000.csv
    ...
  screenshots/
    2026-05-27_183045_chart_daily_backfill_000.png
    ...
```

### Performance

- **Daily data**: ~1-2 seconds per period (365 days = ~10 minutes)
- **Monthly data**: ~1-2 seconds per period (12 months = ~30 seconds)
- **Yearly data**: ~1-2 seconds per period (5 years = ~15 seconds)

**Typical backfill time**: 10-15 minutes for 1 year of data

### After Backfill

Once backfill completes:

1. **Run the main scraper** to push all data to Home Assistant:
   ```bash
   docker compose restart
   ```

2. **Verify in Home Assistant** that historical statistics appear in the Energy dashboard

3. **Normal operation** - main scraper handles daily updates

### Troubleshooting

**"Failed to download CSV"**
- Portal may have changed selectors - check logs
- Network timeout - increase `timeout` in script
- Auth expired - check credentials in `config.yaml`

**"No more previous periods available"**
- This is normal - reached end of history
- Portal may only keep 12-24 months of data

**"Chart failed to load"**
- Network latency - script will retry
- If persistent, increase `_wait_for_chart_stable` timeout

### Safety

- **No data loss**: Existing files are never overwritten
- **Rate limited**: 1.5-2s delay between requests
- **Interruptible**: Ctrl+C safely stops (resume by re-running)
- **No side effects**: Only reads data, never modifies portal

### Limitations

- **Portal history limit**: EKZ may only provide 12-24 months of history
- **Single address**: Backfills only the meter specified in `config.yaml`
- **No deduplication**: If you run multiple times, you'll get duplicate files (timestamps differ)
