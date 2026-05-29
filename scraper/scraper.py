"""Playwright scraper for logging into myEKZ and downloading consumption data.

Selectors are centralised in the SELECTORS dict so they can be updated if
the portal changes its markup without touching the automation logic.
"""
import asyncio
import csv
import logging
import random
import re
import shutil
from datetime import date
from pathlib import Path

from playwright.async_api import Download, Page, async_playwright

from .config import Config
from .storage import already_exists, csv_path, screenshot_path, monthly_snapshot_path
from .bills import scrape_bills

logger = logging.getLogger(__name__)


class PermanentScrapeError(Exception):
    """Non-retryable scrape failure (bad credentials, account locked, CAPTCHA)."""

LOGIN_URL = (
    "https://login.ekz.ch/auth/realms/myEKZ/protocol/openid-connect/auth"
    "?response_type=code"
    "&client_id=cos-myekz-webapp"
    "&scope=openid%20email%20profile%20roles"
    "&redirect_uri=https://my.ekz.ch/login/oauth2/code/cos-portal-web-app-client"
)
CONSUMPTION_URL = "https://my.ekz.ch/verbrauch/"

# CSS selectors — update if portal changes its markup
# Each key maps to a list tried in order; first visible one wins
SELECTORS: dict[str, list[str]] = {
    "username_field": [
        "#username",
        "input[name='username']",
        "input[type='email']",
    ],
    "password_field": [
        "#password",
        "input[name='password']",
        "input[type='password']",
    ],
    "login_button": [
        "#kc-login",
        "input[type='submit']",
        "button[type='submit']",
    ],
    # SPA nav link to the consumption section (used to trigger Vue Router navigation
    # instead of a cold page.goto() which can leave the Vue app shell empty).
    "nav_verbrauch": [
        "a[href='/verbrauch']",
        "a[href='/verbrauch/']",
        "a[data-track-me='true'][href*='verbrauch']",
        "a:has-text('Verbrauch')",
    ],
    # Meter/address row in the consumption-point selection table.
    # tr:has-text() preferred because click handler is on the <tr>
    # Avoid bare "table tbody tr" which risks clicking header rows
    "meter_item": [
        ".consumption-point",
        ".meter-item",
        ".address-item",
        "[data-testid='meter-item']",
        ".verbrauchsstelle",
        "li.meter",
    ],
    # Tabs are <a> links (not buttons)
    "tab_daily": [
        "a:has-text('Tage')",
        "button:has-text('Tage')",
        "[data-period='day']",
        "[data-granularity='day']",
    ],
    "tab_monthly": [
        "a:has-text('Monate')",
        "button:has-text('Monate')",
        "[data-period='month']",
        "[data-granularity='month']",
    ],
    "tab_yearly": [
        "a:has-text('Jahre')",
        "button:has-text('Jahre')",
        "[data-period='year']",
        "[data-granularity='year']",
    ],
    # "Ende" jumps to the most recent available period
    "period_last": [
        "button:has-text('Ende')",
        "button[aria-label*='last']",
        "button[aria-label*='Ende']",
    ],
    # Navigate backward/forward through periods (for backfill)
    "period_prev": [
        "button:has-text('Zurück')",
        "button[aria-label*='previous']",
        "button[aria-label*='vorige']",
        "button[aria-label*='zurück']",
        ".period-nav__prev",
        "[data-testid='period-prev']",
    ],
    # Download is a <button class="btn icon-pdf"> with text including "(.csv)"
    "download_btn": [
        "button:has-text('Tabellendaten herunterladen')",
        "button:has-text('CSV')",
        "button:has-text('Export')",
        "button:has-text('Exportieren')",
        "button:has-text('Herunterladen')",
        "button[aria-label*='Download']",
        "button[aria-label*='herunterladen']",
        "[data-testid='download-btn']",
        "a[href$='.csv']",
    ],
    # Stable content element that confirms the consumption detail has rendered.
    # Used to decide whether we're already on the chart page (skip meter selection).
    "consumption_ready": [
        # Primary: tab navigation links that appear when chart view is active
        "a:has-text('Tage')",
        "a:has-text('Monate')",
        "a:has-text('Jahre')",
        "button:has-text('Tage')",
        "button:has-text('Monate')",
        "button:has-text('Jahre')",
        # Data attributes some SPAs use for period selectors
        "[data-period]",
        "[data-granularity]",
        # Highcharts renders a container div when it initialises
        ".highcharts-container",
        ".highcharts-root",
        # Common tab/nav component class names the portal might use
        ".cos-tab",
        ".cos-tab-nav",
        ".cos-period-tabs",
        ".cos-chart",
        ".cos-consumption-chart",
        # Download button appears only when chart data is loaded
        "button:has-text('Tabellendaten herunterladen')",
        "button:has-text('CSV')",
    ],
    # Error/empty states that indicate data is unavailable for this meter
    "consumption_error": [
        "*:has-text('Keine Daten')",
        "*:has-text('keine Daten')",
        "*:has-text('nicht verfügbar')",
        "*:has-text('Keine Verbrauchsdaten')",
        "*:has-text('Derzeit keine Daten')",
    ],
}




async def _polite_pause(min_s: float = 1.5, max_s: float = 4.0) -> None:
    """Sleep for a random interval between browser actions."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def run_scrape(config: Config) -> None:
    """Launch browser, login, scrape all granularities, close browser.

    Raises PermanentScrapeError for non-retryable failures (bad credentials).
    Raises RuntimeError for transient failures (network timeout, portal error).
    """
    run_date = date.today()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=config.headless,
            args=[
                "--disable-dev-shm-usage",  # use /tmp instead of /dev/shm (Pi stability)
                "--disable-blink-features=AutomationControlled",  # reduce detection
                "--no-sandbox",  # required in Docker
            ]
        )
        try:
            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()

            def _on_console(msg: object) -> None:
                if getattr(msg, "type", "") in ("error", "warning"):
                    # Cap message length to avoid logging sensitive portal data
                    text = str(getattr(msg, "text", ""))[:200]
                    logger.debug("Browser [%s]: %s", msg.type, text)  # type: ignore[attr-defined]

            page.on("console", _on_console)

            try:
                logger.info("→ Login phase starting")
                await _login(page, config)
                logger.info("✓ Login successful")
                
                await _polite_pause(2.0, 5.0)
                
                logger.info("→ Consumption data scraping starting")
                await _scrape_consumption(page, config, run_date)
                logger.info("✓ Consumption data scraped")
                
                await _polite_pause(1.5, 3.5)
                
                logger.info("→ Bills scraping starting")
                await scrape_bills(page, config, run_date)
                logger.info("✓ Bills scraped")
                
                logger.info("→ Saving monthly snapshot")
                _save_monthly_snapshot(config.data_dir, run_date)
                logger.info("✓ Monthly snapshot saved")
            except PermanentScrapeError:
                await _save_debug_screenshot(page, config.data_dir, run_date, "error")
                raise
            except Exception as exc:
                await _save_debug_screenshot(page, config.data_dir, run_date, "error")
                raise RuntimeError(f"Scrape failed: {exc}") from exc
        finally:
            await browser.close()




async def _login(page: Page, config: Config) -> None:
    logger.info("Navigating to login page")
    await page.goto(LOGIN_URL)
    await page.wait_for_load_state("domcontentloaded")
    await _polite_pause(0.5, 1.5)

    await _fill_field(page, SELECTORS["username_field"], config.username, "username")
    await _polite_pause(0.3, 0.8)
    await _fill_field(page, SELECTORS["password_field"], config.password, "password")
    await _polite_pause(0.5, 1.2)
    await _click_required(page, SELECTORS["login_button"], "login button")

    logger.info("Waiting for redirect to my.ekz.ch")
    try:
        await page.wait_for_url("https://my.ekz.ch/**", timeout=30_000)
    except Exception:
        page_text = (await page.content()).lower()
        permanent_phrases = (
            # Wrong credentials
            "invalid username", "invalid password", "incorrect credentials",
            "ungültig", "passwort falsch", "benutzername falsch",
            # Account locked / blocked
            "account locked", "account disabled", "account suspended",
            "konto gesperrt", "konto deaktiviert", "zu viele versuche",
            "too many", "temporarily locked",
            # CAPTCHA / bot detection
            "captcha", "recaptcha", "robot", "automated access",
            "access denied", "403 forbidden",
        )
        if any(phrase in page_text for phrase in permanent_phrases):
            raise PermanentScrapeError(
                "Login rejected — check ekz.username and ekz.password in config.yaml, "
                "or account may be locked/blocked"
            )
        raise RuntimeError("Login redirect to my.ekz.ch timed out — portal may be unavailable")
    logger.info("Logged in — now at: %s", page.url)




async def _scrape_consumption(page: Page, config: Config, run_date: date) -> None:
    logger.info("Loading consumption page")

    already_ready = await _navigate_to_consumption(page, config)

    await _debug_snapshot(page, config.data_dir, "02_after_navigation")

    if not already_ready:
        # Log available meters before selection to help users configure ekz.address
        await _log_available_meters(page)
        await _select_meter(page, config)

    await _debug_snapshot(page, config.data_dir, "03_chart_ready")

    # Download CSV for each time granularity + chart screenshot
    for label, tab_key in [
        ("daily", "tab_daily"),
        ("monthly", "tab_monthly"),
        ("yearly", "tab_yearly"),
    ]:
        await _download_granularity(page, config, run_date, label, tab_key)


async def _navigate_to_consumption(page: Page, config: Config) -> bool:
    """Navigate to consumption page and select meter.
    
    Returns True if chart tabs are ready, False if tabs couldn't be confirmed.
    Attempts immediate meter click to catch brief auto-selection window.
    """
    already_on_page = "verbrauch" in page.url.lower()

    if already_on_page:
        # Quick check: if the page is blank, force fresh navigation.
        content_visible = False
        for sel in [
            *SELECTORS["consumption_ready"],
            "table.cos-table",
            "table tbody tr",
        ]:
            try:
                if await page.locator(sel).first.is_visible(timeout=1_500):
                    content_visible = True
                    break
            except Exception:
                pass
        if not content_visible:
            logger.info("Consumption page appears blank — forcing fresh SPA navigation")
            already_on_page = False

    if not already_on_page:
        nav_clicked = await _click(page, SELECTORS["nav_verbrauch"], "Verbrauch nav link", timeout=5_000)
        if nav_clicked:
            try:
                await page.wait_for_url("**/verbrauch**", timeout=10_000)
                logger.info("Navigated via SPA nav link to consumption page")
            except Exception:
                logger.warning("wait_for_url timed out after nav click — proceeding anyway")
        else:
            logger.info("Nav link not found — falling back to page.goto(%s)", CONSUMPTION_URL)
            await page.goto(CONSUMPTION_URL)
    else:
        logger.info("Already on consumption page (%s)", page.url)

    # Wait for content (meter table or chart tabs)
    combined = ", ".join([
        *SELECTORS["consumption_ready"],
        f"tr:has-text('{config.address}')" if config.address else "",
        "table.cos-table tbody tr",
    ])
    combined = ", ".join(s for s in combined.split(", ") if s)
    try:
        await page.wait_for_selector(combined, timeout=45_000)
        logger.info("Consumption page content detected (URL: %s)", page.url)
    except Exception:
        logger.warning(
            "Consumption page content not detected after 45 s — will attempt anyway "
            "(URL: %s)", page.url
        )
        await _debug_snapshot(page, config.data_dir, "01_consumption_timeout", force=True)
        await _dump_html(page, config.data_dir, "01_consumption_timeout", force=True)

    # Attempt immediate click on meter row
    await _try_js_click_meter(page, config)

    # Snapshot page state after click
    await _debug_snapshot(page, config.data_dir, "01_consumption_loaded")
    await _dump_html(page, config.data_dir, "01_consumption_loaded")

    # Wait for chart tabs to become present in DOM
    # Chart renders within ~1-2s once locale is correct
    # Poll using count() to tolerate zero-height CSS containers
    ready_selectors = ", ".join(SELECTORS["consumption_ready"])
    poll_interval_ms = 3_000
    max_polls = 40  # 40 × 3 s = 120 s max total

    for poll in range(1, max_polls + 1):
        await page.wait_for_timeout(poll_interval_ms)
        elapsed_s = poll * (poll_interval_ms // 1000)

        for sel in SELECTORS["consumption_ready"]:
            try:
                if await page.locator(sel).count() > 0:
                    logger.info(
                        "Chart element in DOM at t+%ds via '%s' (URL: %s)",
                        elapsed_s, sel, page.url,
                    )
                    return True
            except Exception:
                pass

        # Intermediate screenshots every 30 s for diagnostics
        if elapsed_s % 30 == 0:
            await _debug_snapshot(page, config.data_dir, f"poll_{elapsed_s:03d}s")

    logger.warning(
        "Chart UI not detected after %d s (URL: %s)",
        max_polls * (poll_interval_ms // 1000), page.url,
    )
    await _debug_snapshot(page, config.data_dir, "poll_final", force=True)
    await _dump_html(page, config.data_dir, "poll_final", force=True)
    return False


async def _try_js_click_meter(page: Page, config: Config) -> bool:
    """Click the meter row using a <td>-to-parent-<tr> strategy.

    The <tr> in the EKZ table has zero reported height (CSS peculiarity), so
    Playwright's visibility checks fail and JS getBoundingClientRect() skips it.
    We locate the <td> cell containing the address text (which does have height),
    navigate to its parent <tr>, and click with force=True to bypass the height=0
    visibility check while still targeting the element with the Vue click handler.

    Returns True if a matching row was found and clicked.
    """
    target = config.address
    if not target:
        return False
    try:
        # Find the <td> containing the address text, go up to parent <tr>, click.
        td_loc = page.locator(f"td:has-text('{target}')").first
        tr_loc = td_loc.locator("..")  # XPath parent = <tr>
        await tr_loc.click(force=True, timeout=5_000)
        logger.info("Clicked meter <tr> via td-parent strategy for '%s'", target)
        return True
    except Exception as e:
        logger.info("td-parent click failed for '%s': %s", target, e)
        # Fallback: try <td> direct click (Vue router still navigates even if
        # chart component has a known rendering issue via this path).
        try:
            td_loc2 = page.locator(f"td:has-text('{target}')").first
            await td_loc2.click(timeout=5_000)
            logger.info("Clicked meter <td> directly for '%s'", target)
            return True
        except Exception as e2:
            logger.info("td direct click also failed for '%s': %s", target, e2)
            return False


async def _log_available_meters(page: Page) -> None:
    """Log all detected meter labels to help users configure ekz.address."""
    try:
        # Try to find meter rows in the table
        rows = await page.locator("table tbody tr").all()
        if rows:
            logger.info("Detected %d meter(s) in your account:", len(rows))
            for i, row in enumerate(rows, 1):
                text = await row.text_content()
                if text:
                    # Clean up whitespace and truncate to reasonable length
                    clean_text = " ".join(text.split())[:150]
                    logger.info("  %d. %s", i, clean_text)
        else:
            logger.debug("No meter rows found in table")
    except Exception as exc:
        logger.debug("Could not enumerate meter labels: %s", exc)


async def _select_meter(page: Page, config: Config) -> None:
    """Secondary meter selection — called only if chart tabs weren't detected after navigation.

    Tries multiple strategies, then waits up to 60 s for the chart to appear.
    """
    # If chart tabs are already visible, nothing to do.
    for sel in SELECTORS["consumption_ready"]:
        try:
            if await page.locator(sel).first.is_visible(timeout=1_000):
                logger.info("Chart tabs visible at _select_meter entry — skipping")
                return
        except Exception:
            pass

    logger.info("Attempting secondary meter selection...")

    # Try real mouse click again (page may have re-rendered the meter table).
    clicked = await _try_js_click_meter(page, config)

    if not clicked:
        # No meter row found — as a last resort, try Playwright's locator click
        # with force=True to bypass any CSS-based actionability blocks.
        target = config.address
        if target:
            try:
                await page.locator(f"tr:has-text('{target}')").first.click(
                    force=True, timeout=10_000
                )
                logger.info("Meter row clicked via Playwright force-click")
                clicked = True
            except Exception as e:
                logger.warning("Force-click failed: %s", e)

        if not clicked:
            await _click(page, SELECTORS["meter_item"], "meter entry (last resort)", timeout=5_000)

    # Wait for chart to load after click.
    try:
        await page.wait_for_selector(
            ", ".join(SELECTORS["consumption_ready"]),
            timeout=90_000,
        )
        logger.info("Chart loaded after secondary meter selection (URL: %s)", page.url)
    except Exception:
        logger.warning(
            "Chart still not visible after secondary selection — proceeding anyway (URL: %s)",
            page.url,
        )
        await page.wait_for_timeout(5_000)



async def _download_granularity(
    page: Page,
    config: Config,
    run_date: date,
    label: str,
    tab_key: str,
    _attempt: int = 1,
) -> None:
    _MAX_ATTEMPTS = 3
    out_path = csv_path(config.data_dir, label, run_date)
    csv_done = already_exists(out_path)
    chart_done = already_exists(screenshot_path(config.data_dir, f"chart_{label}", run_date))
    if csv_done and chart_done:
        return

    logger.info("Switching to %s view (attempt %d/%d)", label, _attempt, _MAX_ATTEMPTS)
    clicked = await _click(page, SELECTORS[tab_key], f"{label} tab", timeout=5_000)
    if clicked:
        await page.wait_for_load_state("networkidle", timeout=20_000)

    await _debug_snapshot(page, config.data_dir, f"03_{label}_tab")

    # Navigate to the most recent available period ("Ende" = last)
    end_clicked = await _click(page, SELECTORS["period_last"], "Ende (last period)", timeout=5_000)
    if end_clicked:
        await page.wait_for_load_state("networkidle", timeout=20_000)
        logger.info("Navigated to last period for %s", label)

    download = await _trigger_download(page, SELECTORS["download_btn"], f"{label} download")
    if download is None:
        await _debug_snapshot(page, config.data_dir, f"04_{label}_no_download", force=True)
        await _dump_html(page, config.data_dir, f"04_{label}_no_download", force=True)
        logger.warning("No download button found for %s (attempt %d)", label, _attempt)
        if _attempt < _MAX_ATTEMPTS:
            logger.info("Retrying %s — re-navigating to consumption page in 5 s ...", label)
            await page.wait_for_timeout(5_000)
            # Full re-navigation to recover from any bad SPA state, then recurse.
            already_ready = await _navigate_to_consumption(page, config)
            if not already_ready:
                await _select_meter(page, config)
            return await _download_granularity(page, config, run_date, label, tab_key, _attempt + 1)
        logger.error("Giving up on %s download after %d attempts", label, _MAX_ATTEMPTS)
        await _save_chart_screenshot(page, config.data_dir, label, run_date)
        return

    if not csv_done:
        await download.save_as(str(out_path))
        logger.info("Saved %s CSV: %s", label, out_path)
        if not _validate_csv(out_path):
            logger.warning("Downloaded %s CSV contains no numeric data — deleting", label)
            out_path.unlink(missing_ok=True)
            if _attempt < _MAX_ATTEMPTS:
                logger.info("Retrying %s (empty CSV) — re-navigating in 5 s ...", label)
                await page.wait_for_timeout(5_000)
                already_ready = await _navigate_to_consumption(page, config)
                if not already_ready:
                    await _select_meter(page, config)
                return await _download_granularity(page, config, run_date, label, tab_key, _attempt + 1)
            logger.error("All %d %s CSV downloads were empty — giving up", _MAX_ATTEMPTS, label)

    await _save_chart_screenshot(page, config.data_dir, label, run_date)


async def _save_chart_screenshot(
    page: Page, data_dir: str, label: str, run_date: date
) -> None:
    """Screenshot just the Highcharts bar chart element."""
    ss_path = screenshot_path(data_dir, f"chart_{label}", run_date)
    if already_exists(ss_path):
        return
    try:
        chart = page.locator(".highcharts-container").first
        await chart.wait_for(state="visible", timeout=10_000)
        await chart.screenshot(path=str(ss_path))
        logger.info("Chart screenshot saved: %s", ss_path)
    except Exception as e:
        logger.warning("Could not capture chart for %s: %s", label, e)




_KWH_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")

def _clean_kwh_value(raw: str) -> str:
    """Strip annotation suffixes like '(geschätzt)' before float parsing."""
    return _KWH_ANNOTATION_RE.sub("", raw).replace(",", ".").strip()


def _validate_csv(path: Path) -> bool:
    """Return True if CSV contains at least one numeric kWh value."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines and lines[0].startswith("sep="):
            lines = lines[1:]
        reader = csv.DictReader(lines, delimiter=";")
        for row in reader:
            for col in ("Verbrauch [kWh]", "Gesamt [kWh]", "ET [kWh]", "HT [kWh]", "NT [kWh]"):
                val = _clean_kwh_value(row.get(col, ""))
                if val:
                    try:
                        float(val)
                        return True
                    except ValueError:
                        pass
    except Exception as e:
        logger.warning("CSV validation error for %s: %s", path, e)
    return False




async def _fill_field(
    page: Page, selectors: list[str], value: str, description: str
) -> None:
    for sel in selectors:
        try:
            locator = page.locator(sel).first
            await locator.wait_for(state="visible", timeout=5_000)
            await locator.fill(value)
            logger.debug("Filled %s via: %s", description, sel)
            return
        except Exception:
            continue
    raise RuntimeError(
        f"Could not locate field '{description}' (tried {len(selectors)} selectors)"
    )


async def _click(
    page: Page, selectors: list[str], description: str, timeout: int = 5_000
) -> bool:
    """Try each selector; click the first visible one. Returns True if clicked."""
    for sel in selectors:
        try:
            locator = page.locator(sel).first
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            logger.debug("Clicked '%s' via: %s", description, sel)
            return True
        except Exception:
            continue
    return False


async def _click_required(
    page: Page, selectors: list[str], description: str, timeout: int = 5_000
) -> None:
    """Like _click but raises if nothing was found."""
    if not await _click(page, selectors, description, timeout):
        raise RuntimeError(
            f"Could not click required element '{description}' "
            f"(tried {len(selectors)} selectors)"
        )


async def _trigger_download(
    page: Page, selectors: list[str], description: str
) -> Download | None:
    """Click the first visible download button; return the resulting Download."""
    for sel in selectors:
        try:
            locator = page.locator(sel).first
            if not await locator.is_visible(timeout=3_000):
                continue
            async with page.expect_download(timeout=60_000) as dl_info:
                await locator.click()
            download = await dl_info.value
            logger.debug("Download triggered via: %s", sel)
            return download
        except Exception:
            continue
    logger.warning("Could not trigger download for '%s'", description)
    return None


async def _save_debug_screenshot(
    page: Page, data_dir: str, run_date: date, label: str
) -> None:
    path = Path(data_dir) / "screenshots" / f"{run_date.isoformat()}_{label}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Debug screenshot: %s", path)
    except Exception as e:
        logger.warning("Could not save debug screenshot: %s", e)


async def _debug_snapshot(page: Page, data_dir: str, label: str, *, force: bool = False) -> None:
    """Save a screenshot to data/debug/ for selector investigation.

    In normal INFO mode this is a no-op to avoid disk accumulation.
    Pass ``force=True`` on error/failure paths so diagnostics are always
    captured regardless of log level.
    """
    if not force and not logger.isEnabledFor(logging.DEBUG):
        return
    path = Path(data_dir) / "debug" / f"{label}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Debug snapshot: %s", path)
    except Exception as e:
        logger.warning("Could not save debug snapshot %s: %s", label, e)


async def _dump_html(page: Page, data_dir: str, label: str, *, force: bool = False) -> None:
    """Save page HTML for selector debugging. Only runs at DEBUG level unless force=True."""
    if not force and not logger.isEnabledFor(logging.DEBUG):
        return
    path = Path(data_dir) / "debug" / f"{label}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        html = await page.content()
        path.write_text(html, encoding="utf-8")
        logger.info("HTML dump: %s (%d bytes)", path, len(html))
    except Exception as e:
        logger.warning("Could not save HTML dump %s: %s", label, e)


def _save_monthly_snapshot(data_dir: str, run_date: date) -> None:
    """Copy monthly CSV to persistent snapshot directory (never deleted by cleanup).
    
    Creates data/monthly_snapshots/monthly_YYYY-MM.csv for each month.
    This provides immutable historical records for long-term trend analysis.
    """
    # Find the monthly CSV from today's run
    monthly_csv = csv_path(data_dir, "monthly", run_date)
    if not monthly_csv.exists():
        logger.debug("Monthly CSV not found for snapshot: %s", monthly_csv)
        return
    
    # Create snapshot for the current month
    snapshot_path = monthly_snapshot_path(data_dir, run_date.year, run_date.month)
    
    if snapshot_path.exists():
        logger.debug("Monthly snapshot already exists: %s", snapshot_path)
        return
    
    try:
        shutil.copy2(monthly_csv, snapshot_path)
        logger.info("Created monthly snapshot: %s", snapshot_path.name)
    except Exception as e:
        logger.warning("Could not create monthly snapshot: %s", e)
