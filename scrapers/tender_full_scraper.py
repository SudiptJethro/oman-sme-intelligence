"""
Tender Board - Full Paginated Scraper
──────────────────────────────────────
Scrapes ALL pages of:
  1. Registered Vendors  (402 pages, ~20,100 companies)
  2. Awarded Tenders     (567 pages, ~28,350 tenders)

Run standalone:
    python -m scrapers.tender_full_scraper
    python -m scrapers.tender_full_scraper --source vendors
    python -m scrapers.tender_full_scraper --source tenders

Output: data/vendors_full.jsonl, data/tenders_full.jsonl
Checkpoint: data/.vendors_checkpoint, data/.tenders_checkpoint  (last page saved)
"""

import re
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(open(sys.stdout.fileno(), mode="w",
                                         encoding="utf-8", closefd=False))],
)
logger = logging.getLogger(__name__)

BASE_URL     = "https://etendering.tenderboard.gov.om"
REPORT_URL   = f"{BASE_URL}/product/ReportAction"
DATA_DIR     = Path(__file__).parent.parent / "data"

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Origin":       BASE_URL,
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept":       "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Vendor registry ────────────────────────────────────────────────────────────

VENDOR_EVENT_FLAGS = [
    "RegVendorPublic",   # matches the page URL
    "SearchVendPublic",  # used by existing scraper POST
]

VENDOR_PAYLOAD_BASE = {
    "vendType":          "-1",   # all types (SME + Local + etc.)
    "category":          "-1",
    "subCategory":       "-1",
    "grade":             "-1",
    "companyName":       "",
    "GTB_CRNo":          "",
    "PublicUrl":         "1",
    "CTRL_STRDIRECTION": "LTR",
    "CTRL_USERID":       "null",
    "CTRL_ROLEID":       "",
    "CTRL_SID":          "null",
}


def scrape_vendors(session: requests.Session, resume: bool = True) -> int:
    DATA_DIR.mkdir(exist_ok=True)
    out_path  = DATA_DIR / "vendors_full.jsonl"
    ckpt_path = DATA_DIR / ".vendors_checkpoint"

    start_page = 1
    if resume and ckpt_path.exists():
        try:
            start_page = int(ckpt_path.read_text().strip()) + 1
            logger.info(f"Vendors: resuming from page {start_page}")
        except ValueError:
            pass

    # Detect working eventFlag
    working_flag = _detect_vendor_flag(session)
    if not working_flag:
        logger.error("Vendors: no eventFlag returned data - site may be under maintenance")
        return 0

    logger.info(f"Vendors: using eventFlag={working_flag}")

    saved = 0
    mode  = "a" if resume and start_page > 1 else "w"

    with open(out_path, mode, encoding="utf-8") as fout:
        for page in range(start_page, 10_000):
            html = _post_vendor_page(session, working_flag, page)
            if not html:
                logger.warning(f"Vendors: empty response at page {page} - stopping")
                break

            companies = _parse_vendor_table(html)
            if not companies:
                logger.info(f"Vendors: no rows at page {page} - done")
                break

            for c in companies:
                fout.write(json.dumps(c, ensure_ascii=False) + "\n")
                saved += 1
            fout.flush()

            max_page = _extract_max_page(html)
            logger.info(f"Vendors: page {page}/{max_page or '?'} - {len(companies)} rows")

            ckpt_path.write_text(str(page))

            if max_page and page >= max_page:
                logger.info(f"Vendors: reached last page ({max_page})")
                break

            time.sleep(1.2)

    logger.info(f"Vendors: total saved = {saved} → {out_path}")
    return saved


def _detect_vendor_flag(session: requests.Session) -> str | None:
    for flag in VENDOR_EVENT_FLAGS:
        html = _post_vendor_page(session, flag, page=1)
        if html and _has_table_rows(html):
            return flag
        time.sleep(1)
    return None


def _post_vendor_page(session: requests.Session, event_flag: str, page: int) -> str | None:
    payload = {
        **VENDOR_PAYLOAD_BASE,
        "eventFlag": event_flag,
        "pageNo":    str(page),
        "prevPageNo": str(page - 1),
    }
    headers = {**HEADERS, "Referer": f"{REPORT_URL}?eventFlag=RegVendorPublic"}
    try:
        resp = session.post(REPORT_URL, data=payload, headers=headers, timeout=30)
        if resp.ok and len(resp.text) > 500 and "maintenance" not in resp.text.lower():
            return resp.text
        if "maintenance" in resp.text.lower():
            logger.warning(f"Vendors page {page}: site under maintenance")
    except Exception as e:
        logger.warning(f"Vendors POST page {page}: {e}")
    return None


def _parse_vendor_table(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    # Find the data table (has S No. / Company Name headers)
    for table in tables:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if any("Company" in h or "S No" in h for h in headers):
            rows = []
            for tr in table.find_all("tr")[1:]:
                cols = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cols) >= 4 and cols[0].isdigit():
                    rows.append({
                        "sno":          cols[0],
                        "company_name": cols[1],
                        "cr_number":    cols[2],
                        "address":      cols[3],
                        "tel":          cols[4] if len(cols) > 4 else "",
                        "fax":          cols[5] if len(cols) > 5 else "",
                        "category":     cols[6] if len(cols) > 6 else "",
                        "reg_expiry":   cols[7] if len(cols) > 7 else "",
                        "source":       "tender_vendor_registry",
                        "scraped_at":   datetime.utcnow().date().isoformat(),
                    })
            return rows
    return []


# ── Awarded tenders ────────────────────────────────────────────────────────────

TENDER_EVENT_FLAGS = [
    "CompletedTendersForPublic",
    "SearchAwardPublic",
    "AwardedTendersPublic",
]

TENDER_PAYLOAD_BASE = {
    "division":          "-1",
    "tenderNumber":      "",
    "submissionFromDate": "",
    "submissionToDate":  "",
    "PublicUrl":         "1",
    "CTRL_STRDIRECTION": "LTR",
    "CTRL_USERID":       "null",
    "CTRL_ROLEID":       "",
    "CTRL_SID":          "null",
}


def scrape_tenders(session: requests.Session, resume: bool = True) -> int:
    DATA_DIR.mkdir(exist_ok=True)
    out_path  = DATA_DIR / "tenders_full.jsonl"
    ckpt_path = DATA_DIR / ".tenders_checkpoint"

    start_page = 1
    if resume and ckpt_path.exists():
        try:
            start_page = int(ckpt_path.read_text().strip()) + 1
            logger.info(f"Tenders: resuming from page {start_page}")
        except ValueError:
            pass

    working_flag = _detect_tender_flag(session)
    if not working_flag:
        logger.error("Tenders: no eventFlag returned data - site may be under maintenance")
        return 0

    logger.info(f"Tenders: using eventFlag={working_flag}")

    saved = 0
    mode  = "a" if resume and start_page > 1 else "w"

    with open(out_path, mode, encoding="utf-8") as fout:
        for page in range(start_page, 10_000):
            html = _post_tender_page(session, working_flag, page)
            if not html:
                logger.warning(f"Tenders: empty response at page {page} - stopping")
                break

            tenders = _parse_tender_table(html)
            if not tenders:
                logger.info(f"Tenders: no rows at page {page} - done")
                break

            for t in tenders:
                fout.write(json.dumps(t, ensure_ascii=False) + "\n")
                saved += 1
            fout.flush()

            max_page = _extract_max_page(html)
            logger.info(f"Tenders: page {page}/{max_page or '?'} - {len(tenders)} rows")

            ckpt_path.write_text(str(page))

            if max_page and page >= max_page:
                logger.info(f"Tenders: reached last page ({max_page})")
                break

            time.sleep(1.2)

    logger.info(f"Tenders: total saved = {saved} → {out_path}")
    return saved


def _detect_tender_flag(session: requests.Session) -> str | None:
    for flag in TENDER_EVENT_FLAGS:
        html = _post_tender_page(session, flag, page=1)
        if html and _has_table_rows(html):
            return flag
        time.sleep(1)
    return None


def _post_tender_page(session: requests.Session, event_flag: str, page: int) -> str | None:
    payload = {
        **TENDER_PAYLOAD_BASE,
        "eventFlag": event_flag,
        "pageNo":    str(page),
        "prevPageNo": str(page - 1),
    }
    headers = {**HEADERS, "Referer": f"{REPORT_URL}?eventFlag={event_flag}"}
    try:
        resp = session.post(REPORT_URL, data=payload, headers=headers, timeout=30)
        if resp.ok and len(resp.text) > 500 and "maintenance" not in resp.text.lower():
            return resp.text
        if "maintenance" in resp.text.lower():
            logger.warning(f"Tenders page {page}: site under maintenance")
    except Exception as e:
        logger.warning(f"Tenders POST page {page}: {e}")
    return None


def _parse_tender_table(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if any("Tender" in h or "Entity" in h or "S.No" in h for h in headers):
            rows = []
            for tr in table.find_all("tr")[1:]:
                cols = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cols) >= 6 and cols[0].isdigit():
                    rows.append({
                        "sno":            cols[0],
                        "tender_no":      cols[1],
                        "tender_title":   cols[2],
                        "entity_name":    cols[3],
                        "category_grade": cols[4],
                        "tender_type":    re.sub(r"\s+", " ", cols[5]).strip(),
                        "awarded_date":   (cols[6] if len(cols) > 6 else "").replace(" ", ", ", 1),
                        "source":         "tender_awards",
                        "scraped_at":     datetime.utcnow().date().isoformat(),
                    })
            return rows
    return []


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _has_table_rows(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) >= 4 and cells[0].isdigit():
            return True
    return False


def _extract_max_page(html: str) -> int | None:
    m = re.search(r'hidMax["\s]+value["\s]*=\s*["\']?(\d+)', html, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r'name=["\']hidMax["\'][^>]*value=["\'](\d+)["\']', html, re.I)
    if m:
        return int(m.group(1))
    return None


def load_local_html(vendors_html: str | None = None, tenders_html: str | None = None):
    """Parse already-saved HTML files and write to JSONL (no network needed)."""
    DATA_DIR.mkdir(exist_ok=True)

    if vendors_html:
        with open(vendors_html, encoding="utf-8") as f:
            html = f.read()
        rows = _parse_vendor_table(html)
        out  = DATA_DIR / "vendors_full.jsonl"
        with open(out, "w", encoding="utf-8") as fout:
            for r in rows:
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")
        logger.info(f"Local vendors: {len(rows)} rows → {out}")

    if tenders_html:
        with open(tenders_html, encoding="utf-8") as f:
            html = f.read()
        rows = _parse_tender_table(html)
        out  = DATA_DIR / "tenders_full.jsonl"
        with open(out, "w", encoding="utf-8") as fout:
            for r in rows:
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")
        logger.info(f"Local tenders: {len(rows)} rows → {out}")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tender Board full scraper")
    parser.add_argument("--source", choices=["vendors", "tenders", "both"], default="both")
    parser.add_argument("--no-resume", action="store_true", help="Start from page 1")
    parser.add_argument("--local", action="store_true",
                        help="Parse local HTML files instead of hitting the network")
    args = parser.parse_args()

    if args.local:
        load_local_html(
            vendors_html="Registered Companies.html",
            tenders_html="Report Of Awarded Tenders.html",
        )
        return

    session = requests.Session()
    session.headers.update({"User-Agent": HEADERS["User-Agent"]})

    resume = not args.no_resume

    if args.source in ("vendors", "both"):
        scrape_vendors(session, resume=resume)

    if args.source in ("tenders", "both"):
        scrape_tenders(session, resume=resume)


if __name__ == "__main__":
    main()
