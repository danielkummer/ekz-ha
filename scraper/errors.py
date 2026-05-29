"""Structured error types for actionable debugging."""
from typing import NamedTuple


class ScraperError(Exception):
    """Base exception for all ekz-ha scraper errors."""
    
    def __init__(self, message: str, error_code: str, hint: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


class LoginError(ScraperError):
    """Authentication failed at EKZ portal."""
    
    def __init__(self, message: str, hint: str = ""):
        if not hint:
            hint = (
                "Check credentials in config.yaml. "
                "Reset password at https://myekz.ch if needed. "
                "Run: docker compose run --rm ekz-scraper python -m scraper.main --check-config"
            )
        super().__init__(message, error_code="LOGIN_FAILED", hint=hint)


class SelectorMismatchError(ScraperError):
    """DOM selector not found (EKZ portal UI changed)."""
    
    def __init__(self, selector: str, page_label: str = ""):
        message = f"Selector not found: {selector}"
        if page_label:
            message += f" (on {page_label})"
        hint = (
            "EKZ portal UI may have changed. "
            "Enable DEBUG logging to save HTML dumps: "
            "config.yaml -> log_level: DEBUG, then inspect data/debug/*.html"
        )
        super().__init__(message, error_code="SELECTOR_MISMATCH", hint=hint)


class MeterNotFoundError(ScraperError):
    """Configured meter address not found in available meters."""
    
    def __init__(self, configured_address: str, available_meters: list[str]):
        message = f"Meter '{configured_address}' not found"
        hint = (
            f"Available meters: {', '.join(available_meters) if available_meters else 'none'}. "
            "Update config.yaml -> ekz.address to match one of the available meters. "
            "Set log_level: DEBUG to see full meter list in logs."
        )
        super().__init__(message, error_code="METER_NOT_FOUND", hint=hint)


class DownloadTimeoutError(ScraperError):
    """CSV/PDF download timed out."""
    
    def __init__(self, file_type: str, timeout_seconds: int):
        message = f"{file_type} download timed out after {timeout_seconds}s"
        hint = (
            "EKZ portal may be slow or unresponsive. "
            "Check network connectivity. Retry will happen automatically. "
            "If persistent, increase timeout in scraper code or check EKZ status."
        )
        super().__init__(message, error_code="DOWNLOAD_TIMEOUT", hint=hint)


class DataValidationError(ScraperError):
    """Downloaded data failed validation (empty, corrupted, wrong format)."""
    
    def __init__(self, file_type: str, reason: str):
        message = f"{file_type} validation failed: {reason}"
        hint = (
            "Downloaded file is invalid or empty. "
            "Enable DEBUG logging and check data/debug/ for snapshots. "
            "May indicate EKZ portal issue or format change."
        )
        super().__init__(message, error_code="DATA_VALIDATION_ERROR", hint=hint)


class NetworkError(ScraperError):
    """Network connectivity issue (DNS, timeout, refused connection)."""
    
    def __init__(self, message: str):
        hint = (
            "Check internet connection. "
            "Verify DNS resolution: ping myekz.ch "
            "Check firewall rules if running in restricted network. "
            "Retry will happen automatically."
        )
        super().__init__(message, error_code="NETWORK_ERROR", hint=hint)


class HAPushError(ScraperError):
    """Home Assistant push failed."""
    
    def __init__(self, message: str, status_code: int | None = None):
        hint_parts = [
            "Check HA is running and accessible from scraper.",
            "Verify ha_url in config.yaml (should be http://ha-host:8123).",
            "Verify ha_token is valid (Settings → People → Long-lived access tokens)."
        ]
        if status_code == 401:
            hint_parts.append("Status 401 = Invalid token.")
        elif status_code == 403:
            hint_parts.append("Status 403 = Token lacks write permission.")
        elif status_code and 500 <= status_code < 600:
            hint_parts.append(f"Status {status_code} = HA server error.")
        
        hint = " ".join(hint_parts)
        super().__init__(message, error_code="HA_PUSH_ERROR", hint=hint)


class RateLimitError(ScraperError):
    """EKZ portal rate limiting detected."""
    
    def __init__(self, retry_after_seconds: int | None = None):
        message = "Rate limit detected"
        if retry_after_seconds:
            message += f" (retry after {retry_after_seconds}s)"
        hint = (
            "EKZ portal is rate limiting requests. "
            "Scraper will automatically back off. "
            "If persistent, reduce scrape frequency in config.yaml."
        )
        super().__init__(message, error_code="RATE_LIMIT", hint=hint)


class ConfigError(ScraperError):
    """Configuration validation failed."""
    
    def __init__(self, field: str, reason: str):
        message = f"Config error in {field}: {reason}"
        hint = (
            "Check config.yaml for typos or missing values. "
            "Run: docker compose run --rm ekz-scraper python -m scraper.main --check-config "
            "Compare with config.yaml.example for valid format."
        )
        super().__init__(message, error_code="CONFIG_ERROR", hint=hint)


class BrowserError(ScraperError):
    """Playwright/browser failure (crash, timeout, can't launch)."""
    
    def __init__(self, message: str):
        hint = (
            "Browser automation failed. "
            "Check Docker logs for Playwright errors. "
            "May need to rebuild container: docker compose build --no-cache "
            "Ensure sufficient memory (1GB+ recommended for Playwright)."
        )
        super().__init__(message, error_code="BROWSER_ERROR", hint=hint)


def classify_exception(exc: Exception) -> ScraperError:
    """Convert generic exception to specific ScraperError with actionable hint."""
    msg = str(exc)
    msg_lower = msg.lower()
    
    # Network errors
    if any(keyword in msg_lower for keyword in ["connection", "dns", "timeout", "unreachable"]):
        return NetworkError(msg)
    
    # Playwright/browser errors
    if any(keyword in msg_lower for keyword in ["browser", "playwright", "chromium"]):
        return BrowserError(msg)
    
    # Selector errors
    if "selector" in msg_lower or "locator" in msg_lower or "not found" in msg_lower:
        return SelectorMismatchError(msg)
    
    # Authentication errors
    if any(keyword in msg_lower for keyword in ["login", "auth", "credential", "password"]):
        return LoginError(msg)
    
    # Default: generic scraper error
    return ScraperError(
        message=msg,
        error_code="UNKNOWN_ERROR",
        hint="Enable DEBUG logging and check data/debug/ for snapshots. Open GitHub issue with logs."
    )
