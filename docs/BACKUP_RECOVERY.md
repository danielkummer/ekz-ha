# Backup and Recovery Guide

This guide covers strategies for backing up and restoring your ekz-ha data and configuration.

## What to Backup

### Critical Files (Must backup)

1. **config.yaml** - Contains credentials and all configuration
   - Location: `./config.yaml`
   - Contains: EKZ credentials, HA token, meter address
   - **⚠️ Keep secure** - contains passwords and API tokens

2. **data/csv/** - Historical consumption data
   - Daily, monthly, yearly CSV files with date prefixes
   - Essential for long-term statistics and trends
   - File retention controlled by `retention.csv_days` in config

3. **data/bills/** - Invoice PDFs and metadata
   - Location: `./data/bills/*.pdf`
   - Contains all historical invoices
   - Never deleted by retention cleanup

4. **data/monthly_snapshots/** - Persistent historical archives
   - Immutable monthly consumption records
   - Used for long-term trend analysis

### Optional Files (Nice to have)

1. **data/screenshots/** - Chart PNG exports
   - Useful for visual history but regenerated on each run
   - File retention: 30 days default

2. **data/status.json** - Last scrape status
   - Regenerated on every run
   - Not critical for recovery

3. **data/ha_push_history.json** - HA push success tracking
   - Regenerated automatically
   - Retains last 100 push attempts

4. **data/debug/** - Debug artifacts (screenshots, HTML dumps)
   - Only created when `log_level: DEBUG` is set
   - Useful for troubleshooting but not critical
   - File retention: 7 days default

### SSH Keys (if using rsync)

- Location: `./ssh_key` and `./ssh_key.pub`
- Required if `rsync_target` is configured
- Generate with: `ssh-keygen -t ed25519 -f ./ssh_key -N ""`

---

## Backup Strategies

### Strategy 1: Simple tar archive

**Create backup:**
```bash
cd /path/to/ekz-ha
tar -czf ekz-ha-backup-$(date +%Y%m%d).tar.gz \
  config.yaml \
  data/csv/ \
  data/bills/ \
  data/monthly_snapshots/ \
  ssh_key ssh_key.pub 2>/dev/null || true
```

**Restore backup:**
```bash
tar -xzf ekz-ha-backup-YYYYMMDD.tar.gz
docker compose up -d
```

**Pros:** Simple, portable, works anywhere  
**Cons:** Manual process, no versioning

---

### Strategy 2: Automated with cron

Add to crontab (edit with `crontab -e`):

```bash
# Backup ekz-ha daily at 3 AM
0 3 * * * /home/youruser/backup-ekz-ha.sh
```

**backup-ekz-ha.sh:**
```bash
#!/bin/bash
set -euo pipefail

SOURCE=/home/youruser/ekz-ha
BACKUP_DIR=/mnt/nas/backups/ekz-ha
DATE=$(date +%Y%m%d)

# Create backup
cd "$SOURCE"
tar -czf "$BACKUP_DIR/ekz-ha-$DATE.tar.gz" \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='data/debug' \
  --exclude='data/screenshots' \
  config.yaml data/csv data/bills data/monthly_snapshots ssh_key* 2>/dev/null || true

# Keep only last 30 days
find "$BACKUP_DIR" -name "ekz-ha-*.tar.gz" -mtime +30 -delete

echo "Backup complete: ekz-ha-$DATE.tar.gz"
```

Make executable: `chmod +x /home/youruser/backup-ekz-ha.sh`

**Pros:** Automated, retention management  
**Cons:** Requires cron access, local storage

---

### Strategy 3: Git-based versioning (config only)

**Initialize git repo:**
```bash
cd /path/to/ekz-ha
git init
echo "data/" >> .gitignore
echo ".venv/" >> .gitignore
echo "ssh_key*" >> .gitignore
git add config.yaml docker-compose.yml
git commit -m "Initial config"
```

**Commit after config changes:**
```bash
git add config.yaml
git commit -m "Update scrape time to 07:00"
git push origin main  # if using remote repo
```

**Restore previous config:**
```bash
git log --oneline  # find commit hash
git checkout <commit-hash> config.yaml
```

**Pros:** Version history, rollback capability  
**Cons:** Config only, doesn't backup data

---

### Strategy 4: Rsync to NAS/remote server

**Automated sync to NAS:**
```bash
#!/bin/bash
# sync-ekz-ha.sh - Run daily via cron
rsync -avz --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='data/debug' \
  --exclude='data/screenshots' \
  /home/youruser/ekz-ha/ \
  user@nas:/volume1/backups/ekz-ha/
```

**Pros:** Incremental, efficient, remote storage  
**Cons:** Requires rsync access, network dependency

---

## Recovery Procedures

### Full Recovery (new Pi / fresh install)

1. **Install Docker and Docker Compose:**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   # Log out and back in
   ```

2. **Clone repository:**
   ```bash
   cd ~
   git clone https://github.com/yourusername/ekz-ha.git
   cd ekz-ha
   ```

3. **Restore backup:**
   ```bash
   tar -xzf /path/to/ekz-ha-backup-YYYYMMDD.tar.gz
   ```

4. **Verify config:**
   ```bash
   docker compose run --rm ekz-scraper python -m scraper.main --check-config
   ```

5. **Start scraper:**
   ```bash
   docker compose up -d
   docker compose logs -f
   ```

6. **Verify data:**
   ```bash
   ls -lh data/csv/
   cat data/status.json
   ```

---

### Partial Recovery (data corruption)

**Restore only CSV data:**
```bash
# Extract just the CSV files from backup
tar -xzf ekz-ha-backup-YYYYMMDD.tar.gz data/csv/
docker compose restart
```

**Restore only bills:**
```bash
tar -xzf ekz-ha-backup-YYYYMMDD.tar.gz data/bills/
```

**Restore config only:**
```bash
tar -xzf ekz-ha-backup-YYYYMMDD.tar.gz config.yaml
docker compose run --rm ekz-scraper python -m scraper.main --check-config
docker compose restart
```

---

### Historical Backfill (rebuild from EKZ portal)

If you lost CSV data but EKZ portal still has historical data, use the backfill script:

```bash
# Backfill all available historical data
docker compose run --rm ekz-scraper \
  python scripts/backfill_historical.py --mode all --auto-confirm

# Backfill specific date range
docker compose run --rm ekz-scraper \
  python scripts/backfill_historical.py \
  --start 2024-01-01 --end 2024-12-31 --auto-confirm
```

**Notes:**
- Backfill uses the same credentials from `config.yaml`
- Scrapes yearly/monthly/daily CSVs from EKZ portal
- Downloads invoice PDFs from billing history
- Uses exponential backoff to avoid rate limiting
- Can take 10-30 minutes for full history
- See `scripts/README.md` for detailed usage

---

## Disaster Recovery Scenarios

### Scenario 1: Pi SD card failure

**Impact:** All data lost  
**Recovery:**
1. Flash new SD card with Raspberry Pi OS
2. Install Docker
3. Clone repo
4. Restore latest backup
5. Start container

**Time:** 30-60 minutes

---

### Scenario 2: Accidental `docker compose down -v`

**Impact:** Container and volumes deleted, but files remain  
**Recovery:**
1. Verify data still exists: `ls data/csv/`
2. Restart: `docker compose up -d`
3. Verify: `docker compose logs -f`

**Time:** 2 minutes

---

### Scenario 3: Corrupted CSV files

**Impact:** Specific date ranges missing or unreadable  
**Recovery:**
1. Remove corrupted files: `rm data/csv/2024-01-15_*.csv`
2. Run backfill for that date range:
   ```bash
   docker compose run --rm ekz-scraper \
     python scripts/backfill_historical.py \
     --start 2024-01-15 --end 2024-01-15 --auto-confirm
   ```

**Time:** 5-10 minutes per date

---

### Scenario 4: Lost EKZ credentials

**Impact:** Cannot scrape new data  
**Recovery:**
1. Reset password at [myekz.ch](https://myekz.ch)
2. Update `config.yaml` with new credentials
3. Verify: `docker compose run --rm ekz-scraper python -m scraper.main --check-config`
4. Restart: `docker compose restart`

**Time:** 10 minutes

---

### Scenario 5: Lost HA token

**Impact:** Cannot push to Home Assistant  
**Recovery:**
1. In HA: Settings → People → Long-lived access tokens
2. Create new token
3. Update `config.yaml`: `ha_token: <new-token>`
4. Restart: `docker compose restart`
5. Verify: Check `data/status.json` for `ha_push_successful: true`

**Time:** 5 minutes

---

## Backup Verification

Always verify your backups work **before** you need them:

```bash
# 1. Extract to temp directory
mkdir /tmp/ekz-ha-test
cd /tmp/ekz-ha-test
tar -xzf /path/to/ekz-ha-backup-YYYYMMDD.tar.gz

# 2. Verify files exist
ls -lh config.yaml data/csv/ data/bills/

# 3. Check config is valid
cd /path/to/ekz-ha
docker compose run --rm ekz-scraper \
  python -c "from scraper.config import load_config; print('Config OK')"

# 4. Clean up
rm -rf /tmp/ekz-ha-test
```

Run quarterly: First week of Jan, Apr, Jul, Oct

---

## Best Practices

1. **Automate backups** - Don't rely on manual processes
2. **Test restores** - Verify backups work before disaster strikes
3. **Store offsite** - Keep at least one backup off-Pi (NAS, cloud, USB drive)
4. **Secure credentials** - Encrypt backups containing `config.yaml`
5. **Document recovery** - Keep printed copy of this guide
6. **Version control config** - Use git for `config.yaml` changes
7. **Retain historical data** - Set `csv_days: 365` or higher in config
8. **Monitor backups** - Set up HA automation to alert on backup failures

---

## Backup Encryption (Optional)

Encrypt backups containing credentials:

**Create encrypted backup:**
```bash
tar -czf - config.yaml data/ ssh_key* | \
  gpg --symmetric --cipher-algo AES256 \
  > ekz-ha-backup-$(date +%Y%m%d).tar.gz.gpg
```

**Restore encrypted backup:**
```bash
gpg --decrypt ekz-ha-backup-YYYYMMDD.tar.gz.gpg | tar -xzf -
```

---

## Recovery Checklist

Print this checklist and keep with your backup media:

- [ ] Install Docker: `curl -fsSL https://get.docker.com | sh`
- [ ] Clone repo: `git clone <repo-url>`
- [ ] Extract backup: `tar -xzf ekz-ha-backup-YYYYMMDD.tar.gz`
- [ ] Verify config: `docker compose run --rm ekz-scraper python -m scraper.main --check-config`
- [ ] Set permissions: `chmod 600 config.yaml ssh_key`
- [ ] Start container: `docker compose up -d`
- [ ] Check logs: `docker compose logs -f --tail=100`
- [ ] Verify data: `cat data/status.json`
- [ ] Test HA push: Check HA entities for new data

---

## Support

For backup/recovery issues:
1. Check `docker compose logs -f` for errors
2. Verify config: `docker compose run --rm ekz-scraper python -m scraper.main --check-config`
3. Run doctor script: `docker compose run --rm ekz-scraper python scripts/doctor.py`
4. Open GitHub issue with logs and config (redact credentials!)
