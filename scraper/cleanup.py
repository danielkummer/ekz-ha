"""Age-based retention cleanup for scraped data files.

Only files with a YYYY-MM-DD date prefix are considered for deletion.
Bills (data/bills/) are never touched.
Debug artifacts (data/debug/) are cleaned up with a short retention (default 7 days).
"""
import logging
from datetime import date, timedelta
from pathlib import Path

from .config import RetentionConfig

logger = logging.getLogger(__name__)

_MAX_RETENTION_DAYS = 36500  # 100 years — sanity cap for timedelta


def run_cleanup(data_dir: str, retention: RetentionConfig) -> None:
    """Purge files older than their configured retention limit."""
    base = Path(data_dir)
    _purge_dated(base / "csv",         retention.csv_days,         "CSV")
    _purge_dated(base / "screenshots", retention.screenshot_days,  "screenshot")
    _purge_dated(base / "debug",       retention.debug_days,       "debug")


def _purge_dated(directory: Path, max_age_days: int, label: str) -> None:
    """Delete files in *directory* whose YYYY-MM-DD filename prefix is older
    than *max_age_days*. Files without a recognisable date prefix are skipped.
    Symlinked directories are never processed to avoid deleting outside the tree.
    """
    if not directory.exists():
        logger.debug("%s directory does not exist: %s", label, directory)
        return

    if directory.is_symlink():
        logger.warning(
            "Skipping %s cleanup — directory is a symlink and will not be followed: %s",
            label, directory,
        )
        return

    if max_age_days <= 0:
        logger.debug("Retention disabled for %s directory (max_age_days=%d)", label, max_age_days)
        return

    # Calculate directory size before cleanup
    total_size_before = sum(f.stat().st_size for f in directory.rglob('*') if f.is_file() and not f.is_symlink())

    cutoff = date.today() - timedelta(days=min(max_age_days, _MAX_RETENTION_DAYS))
    logger.debug(
        "Checking %s retention in %s (cutoff=%s, max_age=%dd)",
        label, directory, cutoff.isoformat(), max_age_days,
    )

    deleted = 0
    deleted_size = 0
    skipped = 0
    for f in sorted(directory.iterdir()):
        # Never follow symlinks — only delete real files.
        if f.is_symlink() or not f.is_file():
            continue
        file_date = _parse_date_prefix(f.name)
        if file_date is None:
            logger.debug("Skipping %s — no date prefix: %s", label, f.name)
            skipped += 1
            continue
        if file_date < cutoff:
            file_size = f.stat().st_size
            logger.debug(
                "Deleting old %s: %s (file_date=%s, size=%d bytes)", label, f.name, file_date, file_size
            )
            try:
                f.unlink()
                deleted += 1
                deleted_size += file_size
            except OSError as e:
                logger.warning("Could not delete %s: %s", f, e)

    # Calculate directory size after cleanup
    total_size_after = sum(f.stat().st_size for f in directory.rglob('*') if f.is_file() and not f.is_symlink())

    if deleted:
        logger.info(
            "Retention: removed %d old %s file(s) from %s (freed %.2f MB, %d files skipped, dir size: %.2f MB)", 
            deleted, label, directory, deleted_size / (1024 * 1024), skipped, 
            total_size_after / (1024 * 1024)
        )
    else:
        logger.debug(
            "Retention: no %s files to remove in %s (%d files skipped, dir size: %.2f MB)", 
            label, directory, skipped, total_size_after / (1024 * 1024)
        )


def _parse_date_prefix(name: str) -> date | None:
    """Extract the leading YYYY-MM-DD from a filename like '2025-01-15_daily.csv'.
    Returns None if the prefix cannot be parsed as a valid date.
    """
    if len(name) < 10:
        return None
    try:
        return date.fromisoformat(name[:10])
    except ValueError:
        return None
