"""Configuration loaded exclusively from config.yaml.

The config file path defaults to ./config.yaml and can be overridden by the
EKZ_CONFIG_FILE environment variable (the only env var still honoured).

All other settings live in config.yaml — see config.yaml.example.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


@dataclass
class RetentionConfig:
    """File retention policy. Set a value to 0 to disable cleanup for that category."""
    csv_days: int = 90         # daily / monthly / yearly CSV files
    screenshot_days: int = 30  # chart screenshots
    debug_days: int = 7        # debug artifacts (screenshots, HTML dumps)


@dataclass
class Config:
    username: str
    password: str
    scrape_time: str
    headless: bool
    data_dir: str
    address: str        # substring of the meter label shown in the portal
    rsync_target: str   # user@host:/path — empty means disabled
    ha_url: str         # empty means HA push is disabled
    ha_token: str       # HA long-lived access token
    log_level: str = "INFO"
    max_retries: int = 3
    retry_backoff_base_minutes: int = 15
    ha_republish_minutes: int = 15  # re-push ephemeral HA states this often (0 = disabled)
    cost_per_kwh: float = 0.25  # flat tariff (CHF/kWh) for cost estimates & statistics
    retention: RetentionConfig = field(default_factory=RetentionConfig)


def _load_yaml(path: str) -> dict:
    """Load config.yaml. Raises RuntimeError if file is found but unreadable."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(
            f"Config file not found: {path!r}\n"
            "Copy config.yaml.example to config.yaml and fill in your credentials."
        )
    try:
        import yaml
    except ImportError:
        raise RuntimeError(
            f"Config file {path!r} found but PyYAML is not installed. "
            "Run: pip install PyYAML"
        )
    try:
        with p.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        raise RuntimeError(f"Could not parse config file {path!r}: {e}") from e


def load_config() -> Config:
    cfg_path = os.environ.get("EKZ_CONFIG_FILE", "config.yaml")
    file_cfg = _load_yaml(cfg_path)

    ekz       = file_cfg.get("ekz",            {}) or {}
    ha        = file_cfg.get("home_assistant", {}) or {}
    rsync     = file_cfg.get("rsync",          {}) or {}
    retention = file_cfg.get("retention",      {}) or {}
    tariff    = file_cfg.get("tariff",         {}) or {}

    errors = []
    
    username = str(ekz.get("username", "")).strip()
    password = str(ekz.get("password", "")).strip()

    if not username:
        errors.append("ekz.username is required — your myEKZ login email")
    if not password:
        errors.append("ekz.password is required — your myEKZ password")

    scrape_time = str(ekz.get("scrape_time", "06:00"))
    parts = scrape_time.split(":")
    if (
        len(parts) != 2
        or not parts[0].isdigit()
        or not parts[1].isdigit()
        or not (0 <= int(parts[0]) <= 23)
        or not (0 <= int(parts[1]) <= 59)
    ):
        errors.append(f"ekz.scrape_time must be HH:MM (e.g. 06:00), got: {scrape_time!r}")

    _raw_headless = ekz.get("headless", True)
    headless = True
    if isinstance(_raw_headless, bool):
        headless = _raw_headless
    elif str(_raw_headless).lower() in ("false", "no", "0"):
        headless = False
    elif str(_raw_headless).lower() in ("true", "yes", "1"):
        headless = True
    else:
        errors.append(f"ekz.headless must be true or false, got: {_raw_headless!r}")

    data_dir = (
        str(ekz.get("data_dir", "")).strip()
        or os.path.join(os.getcwd(), "data")
    )

    address = str(ekz.get("address", "")).strip()
    if not address:
        errors.append("ekz.address is required — any unique substring of your meter label (e.g. 'Main Street 42')")
    rsync_target = str(rsync.get("target",  "")).strip()
    ha_url       = str(ha.get("url",        "")).strip()
    ha_token     = str(ha.get("token",      "")).strip()

    raw_log_level = str(file_cfg.get("log_level", "INFO")).upper().strip()
    log_level = "INFO"
    if raw_log_level not in _VALID_LOG_LEVELS:
        errors.append(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got: {raw_log_level!r}")
    else:
        log_level = raw_log_level

    def _int(section: dict, key: str, default: int, min_val: int, max_val: int, label: str) -> int:
        try:
            val = int(section.get(key, default))
        except (TypeError, ValueError):
            errors.append(f"{label} must be an integer, got: {section.get(key)!r}")
            return default
        if val < min_val or val > max_val:
            errors.append(f"{label} must be between {min_val} and {max_val}, got: {val}")
            return default
        return val

    max_retries = _int(ekz, "max_retries", 3, 1, 10, "ekz.max_retries")
    retry_backoff_base_minutes = _int(
        ekz, "retry_backoff_base_minutes", 15, 1, 240, "ekz.retry_backoff_base_minutes"
    )
    # How often to re-push ephemeral HA states so entities survive HA restarts.
    # 0 disables periodic re-pushing (states then persist only until next scrape).
    ha_republish_minutes = _int(
        ha, "republish_minutes", 15, 0, 1440, "home_assistant.republish_minutes"
    )

    def _days(key: str, default: int) -> int:
        raw = retention.get(key, default)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            errors.append(f"retention.{key} must be an integer, got: {raw!r}")
            return default
        if val < 0:
            errors.append(f"retention.{key} must be >= 0 (use 0 to disable), got: {val}")
            return default
        if val > 36500:
            errors.append(f"retention.{key} exceeds 100 years ({val}) — check your config")
            return default
        return val

    retention_cfg = RetentionConfig(
        csv_days=_days("csv_days", 90),
        screenshot_days=_days("screenshot_days", 30),
        debug_days=_days("debug_days", 7),
    )

    cost_per_kwh = 0.25
    raw_rate = tariff.get("cost_per_kwh", 0.25)
    try:
        cost_per_kwh = float(raw_rate)
    except (TypeError, ValueError):
        errors.append(f"tariff.cost_per_kwh must be a number, got: {raw_rate!r}")
    else:
        if cost_per_kwh < 0:
            errors.append(f"tariff.cost_per_kwh must be >= 0, got: {cost_per_kwh}")
            cost_per_kwh = 0.25

    if errors:
        error_msg = "Configuration errors:\n" + "\n".join(f"  • {e}" for e in errors)
        raise RuntimeError(error_msg)

    return Config(
        username=username,
        password=password,
        scrape_time=scrape_time,
        headless=headless,
        data_dir=data_dir,
        address=address,
        rsync_target=rsync_target,
        ha_url=ha_url,
        ha_token=ha_token,
        log_level=log_level,
        max_retries=max_retries,
        retry_backoff_base_minutes=retry_backoff_base_minutes,
        ha_republish_minutes=ha_republish_minutes,
        cost_per_kwh=cost_per_kwh,
        retention=retention_cfg,
    )
