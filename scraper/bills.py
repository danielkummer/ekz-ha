"""Bills section: download PDF invoices from myEKZ and extract amounts.

Portal page  : https://my.ekz.ch/rechnung
Output PDFs  : data/bills/<period_start>_to_<period_end>.pdf
Aggregate CSV: data/bills/bills.csv

Portal DOM structure:
  .cos-invoice-card
    .cos-invoice-card__value-description       e.g. "Schlussabrechnung"
    .cos-invoice-card__value-accountingPeriod  e.g. "16.09.2025 - 30.09.2025"
    .cos-invoice-card__value-amount            e.g. "CHF 32.85"
    a.icon-download[href*='/api/invoice']      PDF download link

Amount is read directly from HTML — pdfplumber is fallback only.

CSV format: period_start;period_end;amount_chf;type;address;pdf_file;invoice_number;due_date
"""
import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import Page

from .config import Config
from .storage import bills_dir, bills_csv_path

logger = logging.getLogger(__name__)

BILLS_URL = "https://my.ekz.ch/rechnung"

BILLS_CSV_FIELDS = [
    "period_start", 
    "period_end", 
    "amount_chf", 
    "type", 
    "address", 
    "pdf_file",
    "invoice_number",  # Added: unique identifier from portal
    "due_date",        # Added: payment due date if available
]



_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_AMOUNT_RE = re.compile(r"CHF\s*([\d'.,]+)", re.IGNORECASE)


def _parse_period(text: str) -> tuple[str | None, str | None]:
    """Return (ISO start, ISO end) from text like '16.09.2025 - 30.09.2025'."""
    dates = []
    for m in _DATE_RE.finditer(text):
        try:
            dates.append(datetime.strptime(m.group(0), "%d.%m.%Y").date().isoformat())
        except ValueError:
            pass
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        return dates[0], None
    return None, None


def _parse_single_date(text: str) -> str | None:
    """Extract single date from text like 'Fällig: 15.10.2025'."""
    m = _DATE_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(0), "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass
    return None


def _parse_chf_amount(text: str) -> float | None:
    """Extract CHF amount from text like 'CHF\xa032.85'."""
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    return _normalise_amount(m.group(1))


def _normalise_amount(raw: str) -> float | None:
    """Convert Swiss-formatted number to float (1'234.50 -> 1234.5)."""
    s = raw.strip().replace("'", "").replace("\u2019", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _period_from_filename(name: str) -> tuple[str | None, str | None]:
    m = re.match(r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})", name)
    return (m.group(1), m.group(2)) if m else (None, None)




def parse_bill_amount(pdf_path: Path) -> float | None:
    """Extract invoice total from PDF text using pdfplumber (fallback)."""
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed — cannot parse PDF amounts")
        return None
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        logger.warning("Could not read PDF %s: %s", pdf_path.name, e)
        return None

    patterns = [
        r"(?:Rechnungsbetrag|Gesamtbetrag|Zu bezahlen|Zahlbetrag)"
        r"[\s:]*CHF[\s]*([\d'.,]+)",
        r"(?:Rechnungsbetrag|Gesamtbetrag|Zu bezahlen|Zahlbetrag)"
        r"[\s:]*([\d'.,]+)[\s]*CHF",
        r"Total[\s:]*CHF[\s]*([\d'.,]+)",
        r"Total[\s:]*([\d'.,]+)[\s]*CHF",
        r"CHF[\s]*([\d'.,]+)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            amount = _normalise_amount(matches[-1])
            if amount is not None:
                logger.info("PDF fallback: extracted %.2f CHF from %s", amount, pdf_path.name)
                return amount
    logger.warning("Could not extract CHF amount from PDF %s", pdf_path.name)
    return None




def rebuild_bills_csv(bdir: Path, address: str) -> None:
    """Scan all PDFs in bdir and rebuild bills.csv. Reuses existing amounts when PDFs haven't changed."""
    # Load any already-known amounts from the existing CSV
    existing: dict[str, dict] = {}
    csv_path = bdir / "bills.csv"
    csv_mtime = 0
    
    if csv_path.exists():
        try:
            csv_mtime = csv_path.stat().st_mtime
            for row in csv.DictReader(
                csv_path.read_text(encoding="utf-8").splitlines(), delimiter=";"
            ):
                if row.get("pdf_file"):
                    existing[row["pdf_file"]] = row
        except Exception:
            pass

    rows: list[dict] = []
    parsed_count = 0
    reused_count = 0
    
    for pdf_path in sorted(bdir.glob("*.pdf")):
        name = pdf_path.name
        
        # If we have existing data for this PDF
        if name in existing:
            # Check if PDF is newer than the CSV (indicates it was re-downloaded or modified)
            pdf_mtime = pdf_path.stat().st_mtime
            if csv_mtime > 0 and pdf_mtime < csv_mtime:
                # PDF is older than CSV, reuse existing data
                rows.append(existing[name])
                reused_count += 1
                continue
        
        # PDF is new or modified, parse it
        if name in existing:
            # Modified PDF - use existing metadata but could verify amount
            rows.append(existing[name])
            logger.debug("PDF modified, keeping existing data: %s", name)
            reused_count += 1
        else:
            # New PDF - parse it
            start, end = _period_from_filename(name)
            amount = parse_bill_amount(pdf_path)
            rows.append({
                "period_start": start or "",
                "period_end":   end or "",
                "amount_chf":   f"{amount:.2f}" if amount is not None else "",
                "type":         "",
                "address":      address,
                "pdf_file":     name,
                "invoice_number": "",
                "due_date":     "",
            })
            parsed_count += 1
            logger.debug("Parsed new PDF: %s", name)

    if not rows:
        logger.info("No bill PDFs in %s — bills.csv not written", bdir)
        return
    
    if parsed_count > 0:
        logger.info("Bills CSV: %d parsed, %d reused from existing CSV", parsed_count, reused_count)
    else:
        logger.debug("Bills CSV: reused all %d existing entries (no new PDFs)", reused_count)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BILLS_CSV_FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Bills CSV written: %s (%d invoice(s))", csv_path, len(rows))




async def scrape_bills(page: Page, config: Config, run_date: date) -> None:
    """Navigate to /rechnung, select the apartment, download new bill PDFs."""
    bdir = bills_dir(config.data_dir)

    logger.info("Loading bills page: %s", BILLS_URL)
    await page.goto(BILLS_URL)
    await page.wait_for_load_state("networkidle", timeout=30_000)
    await _debug_snapshot(page, config.data_dir, "10_bills_overview")

    # Select the target apartment by address text
    target = config.address
    if target:
        row_sel = f"tr:has-text('{target}')"
        try:
            row = page.locator(row_sel).first
            await row.wait_for(state="visible", timeout=10_000)
            await row.click()
            logger.info("Clicked apartment row for '%s'", target)
        except Exception as e:
            logger.warning("Could not click apartment row for '%s': %s — trying first row", target, e)
            try:
                await page.locator("table.cos-table tbody tr").first.click()
            except Exception as e2:
                logger.error("Could not select any apartment row: %s", e2)
                await _dump_html(page, config.data_dir, "10_bills_no_apartment", force=True)
                return
    else:
        try:
            await page.locator("table.cos-table tbody tr").first.click()
        except Exception as e:
            logger.error("No address configured and no table row found: %s", e)
            return

    # Wait for the invoice list to appear
    try:
        await page.wait_for_selector(".cos-invoice-list", timeout=15_000)
        logger.info("Invoice list loaded")
    except Exception:
        await _debug_snapshot(page, config.data_dir, "10_bills_no_list", force=True)
        await _dump_html(page, config.data_dir, "10_bills_no_list", force=True)
        logger.warning("Invoice list did not appear")
        return

    await _debug_snapshot(page, config.data_dir, "11_bills_list")

    # Find all invoice cards
    cards = await page.locator(".cos-invoice-card").all()
    logger.info("Found %d invoice card(s)", len(cards))

    downloaded_any = False
    for idx, card in enumerate(cards):
        try:
            period_text = await card.locator(
                ".cos-invoice-card__value-accountingPeriod"
            ).inner_text()
            amount_text = await card.locator(
                ".cos-invoice-card__value-amount"
            ).inner_text()
            bill_type = ""
            try:
                bill_type = (await card.locator(
                    ".cos-invoice-card__value-description"
                ).inner_text()).strip()
            except Exception:
                pass

            period_start, period_end = _parse_period(period_text)
            amount_chf = _parse_chf_amount(amount_text)
            
            # Extract due date if available (usually shows "Fällig: DD.MM.YYYY")
            due_date = None
            try:
                due_text = await card.locator(
                    ".cos-invoice-card__value-dueDate, "
                    "[class*='due'], "
                    "[class*='fällig']"
                ).inner_text()
                due_date = _parse_single_date(due_text)
            except Exception:
                pass
            
            # Extract invoice number if available
            invoice_number = None
            try:
                inv_text = await card.locator(
                    ".cos-invoice-card__value-invoiceNumber, "
                    "[class*='invoice'], "
                    "[class*='rechnung']"
                ).inner_text()
                # Extract any number-like pattern
                inv_match = re.search(r"[A-Z0-9-]{6,}", inv_text)
                if inv_match:
                    invoice_number = inv_match.group(0)
            except Exception:
                pass

            logger.info(
                "Card %d: %s | %s | %s | CHF %.2f | Invoice: %s | Due: %s",
                idx + 1, bill_type, period_text.strip(),
                period_start, amount_chf or 0, invoice_number or "N/A", due_date or "N/A",
            )
            logger.debug(
                "  Raw period text: %r  raw amount text: %r",
                period_text.strip(), amount_text.strip(),
            )

            # Derive filename from billing period
            if period_start and period_end:
                pdf_name = f"{period_start}_to_{period_end}.pdf"
            else:
                pdf_name = f"{run_date.isoformat()}_bill_{idx + 1:02d}.pdf"

            pdf_path = bdir / pdf_name
            if pdf_path.exists():
                logger.info("Already downloaded: %s — skipping", pdf_name)
                continue

            # Find and click the download link inside this card — try progressively
            # broader selectors because different invoice types may use different markup
            dl_link = card.locator(
                "a[href*='/api/invoice'], "
                "a[href*='invoice'], "
                "a[href*='/download'], "
                "a.icon-download, "
                "a[download], "
                "button[aria-label*='herunterladen'], "
                "button[aria-label*='download'], "
                "a:has-text('PDF'), "
                "a:has-text('Herunterladen')"
            )
            try:
                await dl_link.first.wait_for(state="visible", timeout=5_000)
            except Exception:
                logger.warning("No download link in card %d — skipping", idx + 1)
                continue

            async with page.expect_download(timeout=60_000) as dl_info:
                await dl_link.first.click()
            dl = await dl_info.value
            
            # Try to extract invoice number from download URL if not found in card
            if not invoice_number:
                try:
                    dl_url = await dl_link.first.get_attribute("href")
                    if dl_url:
                        # Look for invoice ID in URL like /api/invoice/123456/download
                        url_match = re.search(r'/invoice/([A-Z0-9-]+)', dl_url)
                        if url_match:
                            invoice_number = url_match.group(1)
                except Exception:
                    pass
            
            await dl.save_as(str(pdf_path))
            logger.info("Downloaded: %s", pdf_name)

            # Write a row directly into bills.csv using HTML-sourced data
            _append_or_update_bill(
                bdir, pdf_name, period_start, period_end,
                amount_chf, bill_type, config.address,
                invoice_number, due_date,
            )
            downloaded_any = True

        except Exception as e:
            logger.warning("Failed to process invoice card %d: %s", idx + 1, e)

    if not downloaded_any:
        logger.info("No new bills to download")

    # Final rebuild to make sure bills.csv is consistent with all PDFs on disk
    rebuild_bills_csv(bdir, config.address)


def _append_or_update_bill(
    bdir: Path,
    pdf_name: str,
    period_start: str | None,
    period_end: str | None,
    amount_chf: float | None,
    bill_type: str,
    address: str,
    invoice_number: str | None = None,
    due_date: str | None = None,
) -> None:
    """Add or update a single bill row in bills.csv immediately after download."""
    csv_path = bdir / "bills.csv"
    rows: list[dict] = []
    if csv_path.exists():
        try:
            rows = list(csv.DictReader(
                csv_path.read_text(encoding="utf-8").splitlines(), delimiter=";"
            ))
        except Exception:
            rows = []

    new_row = {
        "period_start": period_start or "",
        "period_end":   period_end or "",
        "amount_chf":   f"{amount_chf:.2f}" if amount_chf is not None else "",
        "type":         bill_type or "",
        "address":      address or "",
        "pdf_file":     pdf_name,
        "invoice_number": invoice_number or "",
        "due_date":     due_date or "",
    }

    # Replace existing row for same pdf_file, or append
    updated = False
    for i, row in enumerate(rows):
        if row.get("pdf_file") == pdf_name:
            rows[i] = new_row
            updated = True
            break
    if not updated:
        rows.append(new_row)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BILLS_CSV_FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)




async def _debug_snapshot(page: Page, data_dir: str, label: str, *, force: bool = False) -> None:
    """Only runs at DEBUG log level (or when force=True for failure paths)."""
    if not force and not logger.isEnabledFor(logging.DEBUG):
        return
    path = Path(data_dir) / "debug" / f"{label}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True)
        logger.debug("Debug snapshot: %s", path)
    except Exception as e:
        logger.warning("Could not save debug snapshot %s: %s", label, e)


async def _dump_html(page: Page, data_dir: str, label: str, *, force: bool = False) -> None:
    """Only runs at DEBUG log level (or when force=True for failure paths)."""
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

