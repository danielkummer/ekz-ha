"""File-system helpers: dated paths, deduplication, directory creation."""
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


def _dated(data_dir: str, subfolder: str, label: str, ext: str, run_date: date) -> Path:
    p = Path(data_dir) / subfolder / f"{run_date.isoformat()}_{label}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def csv_path(data_dir: str, label: str, run_date: date | None = None) -> Path:
    return _dated(data_dir, "csv", label, "csv", run_date or date.today())


def screenshot_path(data_dir: str, label: str, run_date: date | None = None) -> Path:
    return _dated(data_dir, "screenshots", label, "png", run_date or date.today())


def bills_dir(data_dir: str) -> Path:
    """Return (and create) the bills subdirectory."""
    p = Path(data_dir) / "bills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def bills_csv_path(data_dir: str) -> Path:
    """Aggregate bills CSV path — not dated, rebuilt on every run."""
    return bills_dir(data_dir) / "bills.csv"


def monthly_snapshot_path(data_dir: str, year: int, month: int) -> Path:
    """Return path for persistent monthly snapshot: data/monthly_snapshots/monthly_YYYY-MM.csv
    
    These files are never deleted by cleanup and provide immutable historical records.
    """
    p = Path(data_dir) / "monthly_snapshots" / f"monthly_{year:04d}-{month:02d}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def already_exists(path: Path) -> bool:
    if path.exists():
        logger.info("Skipping %s - already exists", path)
        return True
    return False
