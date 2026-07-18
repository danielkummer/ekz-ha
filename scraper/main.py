"""Entrypoint: runs a scrape once at startup, then daily on schedule."""
import asyncio
import json
import logging
import math
import os
import random
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule

from .config import Config, load_config
from .scraper import PermanentScrapeError, run_scrape
from .sync import rsync_data
from .ha_push import push_to_ha, republish_to_ha
from .cleanup import run_cleanup

logger = logging.getLogger("scraper.main")


def _backoff_seconds(attempt: int, base_minutes: int, cap_minutes: int = 240) -> float:
    """Equal-jitter exponential backoff in seconds, capped at cap_minutes."""
    ceiling = min(base_minutes * math.pow(2, attempt), cap_minutes)
    half = ceiling / 2.0
    return (half + random.uniform(0.0, half)) * 60.0


def _check_disk_space(data_dir: str) -> None:
    try:
        usage = shutil.disk_usage(data_dir)
        free_pct = usage.free / usage.total * 100
        free_mb = usage.free / (1024 * 1024)
        if free_pct < 5.0:
            logger.error(
                "Critically low disk space: %.0f MB (%.1f%%) free on %s volume",
                free_mb, free_pct, data_dir,
            )
        elif free_pct < 15.0:
            logger.warning(
                "Low disk space: %.0f MB (%.1f%%) free on %s volume",
                free_mb, free_pct, data_dir,
            )
        else:
            logger.debug(
                "Disk space OK: %.0f MB (%.1f%%) free on %s volume",
                free_mb, free_pct, data_dir,
            )
    except OSError as exc:
        logger.debug("Could not check disk space for %s: %s", data_dir, exc)


def _write_status(
    data_dir: str, *, success: bool, error: str = "", permanent: bool = False,
    started_at: str = "", finished_at: str = "", duration_s: float = 0,
    attempt: int = 1, phase_status: dict = None
) -> None:
    payload: dict = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "success": success,
    }
    if started_at:
        payload["started_at"] = started_at
    if finished_at:
        payload["finished_at"] = finished_at
    if duration_s > 0:
        payload["duration_s"] = round(duration_s, 2)
    if attempt > 1:
        payload["attempts"] = attempt
    if phase_status:
        payload["phases"] = phase_status
    if error:
        payload["error"] = error
    if permanent:
        payload["permanent_failure"] = True
        payload["action_required"] = "Check ekz.username / ekz.password in config.yaml"

    dest = Path(data_dir) / "status.json"
    tmp = dest.with_suffix(".json.tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, dest)
        logger.debug("Status written: %s", dest)
    except OSError as exc:
        logger.warning("Could not write status file %s: %s", dest, exc)
        tmp.unlink(missing_ok=True)


def _run_phase(name: str, fn, *args) -> str:
    """Run a post-scrape step; returns empty string on success, error message on failure.

    Failures are isolated so rsync, HA push, and cleanup run independently.
    """
    try:
        logger.info("→ Running %s phase", name)
        fn(*args)
        logger.info("✓ %s phase completed", name)
        return ""
    except Exception:
        logger.exception("✗ %s phase failed — continuing with remaining phases", name)
        return f"{name} failed"


def _should_skip_scrape(data_dir: str, min_hours: float = 6.0) -> bool:
    """Check if we should skip scraping based on last successful run time."""
    status_file = Path(data_dir) / "status.json"
    if not status_file.exists():
        return False
    
    try:
        status = json.loads(status_file.read_text())
        if not status.get("success"):
            return False  # last run failed, don't skip
        
        last_run = status.get("finished_at") or status.get("timestamp")
        if not last_run:
            return False
        
        last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        age_hours = (datetime.now(tz=timezone.utc) - last_dt).total_seconds() / 3600
        
        return age_hours < min_hours
    except Exception as exc:
        logger.debug("Could not parse status.json for skip check: %s", exc)
        return False


def _scrape_job(config: Config) -> None:
    started_at = datetime.now(tz=timezone.utc).isoformat()
    logger.info("═══════════════════════════════════════════════════════════")
    logger.info("Scrape job started at %s", started_at)
    logger.info("═══════════════════════════════════════════════════════════")
    _check_disk_space(config.data_dir)

    succeeded = False
    last_error = ""
    final_attempt = 1

    for attempt in range(1, config.max_retries + 1):
        if attempt > 1:
            delay_s = _backoff_seconds(attempt - 1, config.retry_backoff_base_minutes)
            logger.info(
                "Retry %d/%d — waiting %.0f min before next attempt",
                attempt, config.max_retries, delay_s / 60.0,
            )
            time.sleep(delay_s)

        try:
            logger.info("→ Starting scrape attempt %d/%d", attempt, config.max_retries)
            asyncio.run(run_scrape(config))
            logger.info("✓ Scrape completed successfully (attempt %d/%d)", attempt, config.max_retries)
            succeeded = True
            last_error = ""
            final_attempt = attempt
            break
        except PermanentScrapeError as exc:
            logger.error("Permanent scrape failure (will not retry): %s", exc)
            finished_at = datetime.now(tz=timezone.utc).isoformat()
            duration_s = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
            _write_status(
                config.data_dir, success=False, error=str(exc), permanent=True,
                started_at=started_at, finished_at=finished_at, duration_s=duration_s, attempt=attempt
            )
            return
        except Exception as exc:
            last_error = str(exc)
            final_attempt = attempt
            if attempt < config.max_retries:
                logger.warning(
                    "Scrape attempt %d/%d failed: %s", attempt, config.max_retries, exc
                )
            else:
                logger.error(
                    "All %d scrape attempts exhausted. Last error: %s",
                    config.max_retries, exc,
                )

    finished_at = datetime.now(tz=timezone.utc).isoformat()
    duration_s = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()

    if not succeeded:
        _write_status(
            config.data_dir, success=False, error=last_error,
            started_at=started_at, finished_at=finished_at, duration_s=duration_s, attempt=final_attempt
        )
        logger.error("═══════════════════════════════════════════════════════════")
        logger.error("Scrape job FAILED after %d attempts", final_attempt)
        logger.error("═══════════════════════════════════════════════════════════")
        return

    logger.info("───────────────────────────────────────────────────────────")
    logger.info("Post-scrape phases starting")
    logger.info("───────────────────────────────────────────────────────────")
    
    phase_status = {}
    phase_errors = []
    
    rsync_err = _run_phase("rsync",   rsync_data,  config.data_dir, config.rsync_target)
    phase_status["rsync"] = "ok" if not rsync_err else "failed"
    if rsync_err:
        phase_errors.append(rsync_err)
    
    ha_err = _run_phase("HA push", push_to_ha,  config.ha_url, config.ha_token, config.data_dir, config.cost_per_kwh)
    phase_status["ha_push"] = "ok" if not ha_err else "failed"
    if ha_err:
        phase_errors.append(ha_err)
    
    cleanup_err = _run_phase("cleanup", run_cleanup, config.data_dir, config.retention)
    phase_status["cleanup"] = "ok" if not cleanup_err else "failed"

    if phase_errors:
        _write_status(
            config.data_dir, success=False, error="; ".join(phase_errors),
            started_at=started_at, finished_at=finished_at, duration_s=duration_s,
            attempt=final_attempt, phase_status=phase_status
        )
        logger.warning("═══════════════════════════════════════════════════════════")
        logger.warning("Run completed with errors: %s", "; ".join(phase_errors))
        logger.warning("═══════════════════════════════════════════════════════════")
    else:
        _write_status(
            config.data_dir, success=True,
            started_at=started_at, finished_at=finished_at, duration_s=duration_s,
            attempt=final_attempt, phase_status=phase_status
        )
        logger.info("═══════════════════════════════════════════════════════════")
        logger.info("✓ Run completed successfully (duration: %.1fs)", duration_s)
        logger.info("═══════════════════════════════════════════════════════════")


def _republish_job(config: Config) -> None:
    """Re-push ephemeral HA states so entities survive Home Assistant restarts.

    Reads the already-scraped CSVs (no browser/scrape) and re-sends sensor
    states. Cheap and isolated — never raises into the scheduler loop.
    """
    if not config.ha_url:
        return
    try:
        republish_to_ha(config.ha_url, config.ha_token, config.data_dir, config.cost_per_kwh)
        logger.debug("HA state re-push completed")
    except Exception:
        logger.exception("HA state re-push failed — will retry next interval")


def check_config() -> None:
    """Validate config and exit with status code."""
    try:
        config = load_config()
        print("✓ Config valid")
        print(f"  Username    : {config.username}")
        print(f"  Address     : {config.address}")
        print(f"  Data dir    : {config.data_dir}")
        print(f"  HA push     : {'enabled (' + config.ha_url + ')' if config.ha_url else 'disabled (no URL configured)'}")
        if config.ha_url:
            print(f"  HA re-push  : {'every ' + str(config.ha_republish_minutes) + ' min' if config.ha_republish_minutes else 'disabled'}")
        print(f"  Rsync       : {'enabled (' + config.rsync_target + ')' if config.rsync_target else 'disabled'}")
        print(f"  Retention   : CSV {config.retention.csv_days}d, screenshots {config.retention.screenshot_days}d")
        print(f"  Log level   : {config.log_level}")
        print(f"  Scrape time : {config.scrape_time} (Europe/Zurich)")
        sys.exit(0)
    except Exception as exc:
        print(f"✗ Config error: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    # Handle --check-config flag
    if len(sys.argv) > 1 and sys.argv[1] == "--check-config":
        check_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        config = load_config()
    except Exception as exc:
        logging.error("Configuration error: %s", exc)
        sys.exit(1)

    # Scope configured level to this application only; keep third-party libs quiet.
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("scraper").setLevel(config.log_level)

    logger.info("EKZ for Home Assistant — scraper starting up")
    logger.info("  Log level   : %s", config.log_level)
    logger.info("  Scrape time : %s (Europe/Zurich)", config.scrape_time)
    logger.info("  Data dir    : %s", config.data_dir)
    logger.info("  Headless    : %s", config.headless)
    logger.info(
        "  Retries     : up to %d, base backoff %d min (exponential + jitter)",
        config.max_retries, config.retry_backoff_base_minutes,
    )
    
    # Explicit HA push status with URL
    if config.ha_url:
        logger.info("  HA push     : enabled → %s", config.ha_url)
        if config.ha_republish_minutes > 0:
            logger.info("  HA re-push  : every %d min (survives HA restarts)", config.ha_republish_minutes)
        else:
            logger.info("  HA re-push  : disabled")
    else:
        logger.info("  HA push     : disabled (no URL configured)")
    
    # Explicit rsync status with target
    if config.rsync_target:
        logger.info("  Rsync       : enabled → %s", config.rsync_target)
    else:
        logger.info("  Rsync       : disabled")
    
    logger.info(
        "  Retention   : %s",
        f"CSV {config.retention.csv_days}d, screenshots {config.retention.screenshot_days}d" if config.retention.csv_days or config.retention.screenshot_days else "disabled"
    )

    # Skip startup scrape if last successful run was recent
    if _should_skip_scrape(config.data_dir, min_hours=6.0):
        logger.info("Skipping startup scrape — last successful run was < 6 hours ago")
        # Still (re)publish states now so entities exist immediately after a
        # container/HA restart, even when the scrape itself is skipped.
        _republish_job(config)
    else:
        _scrape_job(config)

    schedule.every().day.at(config.scrape_time).do(_scrape_job, config)
    logger.info("Scheduler active — next scrape daily at %s", config.scrape_time)

    # Periodically re-push ephemeral HA states so the dashboard survives HA
    # restarts between daily scrapes (root cause of "entity not found").
    if config.ha_url and config.ha_republish_minutes > 0:
        schedule.every(config.ha_republish_minutes).minutes.do(_republish_job, config)
        logger.info(
            "HA state re-push active — every %d min (keeps entities alive across HA restarts)",
            config.ha_republish_minutes,
        )
    elif config.ha_url:
        logger.info("HA state re-push disabled (home_assistant.republish_minutes = 0)")

    while True:
        try:
            # Calculate seconds until next scheduled run
            next_run = schedule.next_run()
            if next_run is None:
                logger.warning("No scheduled jobs found — sleeping 60s")
                time.sleep(60)
                continue
            
            now = datetime.now()
            sleep_seconds = (next_run - now).total_seconds()
            
            # Sleep until 30 seconds before next run to avoid missing it
            if sleep_seconds > 30:
                sleep_seconds -= 30
            else:
                sleep_seconds = max(1, sleep_seconds)
            
            logger.debug("Next run in %.1f minutes, sleeping for %.1f minutes", 
                        sleep_seconds / 60, sleep_seconds / 60)
            time.sleep(sleep_seconds)
            
            # Run pending jobs
            schedule.run_pending()
            
        except Exception:
            logger.exception("Unexpected error in scheduler loop — continuing")
            time.sleep(60)  # Back off on error


if __name__ == "__main__":
    main()
