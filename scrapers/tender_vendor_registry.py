"""
Tender Board Registered Vendor Registry Scraper
─────────────────────────────────────────────────
Scrapes the public vendor registry at:
  https://etendering.tenderboard.gov.om/product/ReportAction?eventFlag=RegVendorPublic

Strategy (user's design):
  1. Pull all ~20,100 registered vendors (402 pages × 50 rows)
  2. Filter: Company Type = SME (or Local if SME list is small)
  3. Filter: drop vendors whose Reg Expiry Date expired > 1 year ago
  4. Filter: Registered Category matches OIA 150+ protected codes
  5. Cross-match CR number against tender_awards table → award_winner flag
  6. Result: active, OIA-aligned SMEs with proven government contract history

The registry is publicly accessible without login (isUserId=null in page JS).
If blocked, falls back to authenticated Playwright session using
TENDER_BOARD_USERNAME + TENDER_BOARD_PASSWORD env vars.

Also provides import_html() to parse a locally saved HTML file (page-by-page
manual export as a bootstrap before full scrape runs).
"""
import os, re, logging, time, json
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import requests
from .base import BaseScraper

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://etendering.tenderboard.gov.om/product/ReportAction"
LOGIN_URL    = "https://etendering.tenderboard.gov.om/product/loginUserN"

# OIA-aligned categories as they appear in the Tender Board registry
OIA_CATEGORIES = [
    "Construction and Maintenance",
    "Construction of Ports Roads Bridges Railways Dams and Maintenance",
    "Pipeline Network Construction and Drilling Wells",
    "Electromechanical and Telecommunications Contracting and Maintenance",
    "Information Technology Services",
    "Consulting Offices",
    "Supply",
    "Services",
    "Construction Works (Old)",
    "Supply and Services (Old)",
    "Training (Old)",
    "Consultancy  (Old)",
]

# Company types to include (SME first, Local as fallback)
TARGET_COMPANY_TYPES = ["SME", "Local"]

# Drop vendors expired more than this many days ago
MAX_EXPIRY_LAG_DAYS = 365


class TenderVendorRegistryScraper(BaseScraper):
    source_name = "tender_vendor_registry"

    def __init__(self):
        super().__init__()
        self.tb_username = os.getenv("TENDER_BOARD_USERNAME", "")
        self.tb_password = os.getenv("TENDER_BOARD_PASSWORD", "")
        self._session_cookies: dict = {}

    # ── Entry point ────────────────────────────────────────────

    def scrape(self):
        logger.info("TenderVendorRegistry: Starting scrape of ~20,100 vendors")

        # Try public access first (no login)
        test = self._post_page(page_no=1, vendor_type="SME")
        if test and len(self._parse_companies(test)) > 0:
            logger.info("TenderVendorRegistry: Public access working - no login needed")
        elif self.tb_username:
            logger.info("TenderVendorRegistry: Public blocked - trying authenticated login")
            if not self._login():
                logger.error("TenderVendorRegistry: Login failed - aborting")
                return
        else:
            logger.warning(
                "TenderVendorRegistry: Public access blocked and no credentials set. "
                "Set TENDER_BOARD_USERNAME + TENDER_BOARD_PASSWORD to enable."
            )
            return

        total_saved = 0

        # Scrape SMEs across all OIA categories
        for category in OIA_CATEGORIES:
            logger.info(f"TenderVendorRegistry: Category '{category}'")
            count = self._scrape_category(category=category, vendor_type="SME")
            total_saved += count
            self.polite_sleep(2)

        # If SME count is low, also grab Local companies in OIA categories
        if total_saved < 200:
            logger.info("TenderVendorRegistry: SME count low - adding Local companies")
            for category in OIA_CATEGORIES:
                count = self._scrape_category(category=category, vendor_type="Local")
                total_saved += count
                self.polite_sleep(2)

        # Cross-match with tender awards
        self._cross_match_awards()

        logger.info(f"TenderVendorRegistry: Done - {total_saved} companies saved")

    # ── Category scraper with pagination ───────────────────────

    def _scrape_category(self, category: str, vendor_type: str) -> int:
        saved = 0
        for page in range(1, 500):  # max 500 pages per category
            html = self._post_page(page_no=page, vendor_type=vendor_type,
                                   category=category)
            if not html:
                break
            companies = self._parse_companies(html, default_category=category)
            if not companies:
                break
            for c in companies:
                if self._passes_filters(c):
                    if self.save_company(c):
                        saved += 1
            logger.debug(
                f"TenderVendorRegistry [{category}|{vendor_type}] page {page}: "
                f"{len(companies)} companies"
            )
            # Check if last page
            max_page = self._extract_max_page(html)
            if max_page and page >= max_page:
                break
            self.polite_sleep(1.5)
        return saved

    # ── HTTP / form POST ───────────────────────────────────────

    def _post_page(self, page_no: int, vendor_type: str = "SME",
                   category: str = "") -> str | None:
        """POST the vendor registry form for a specific page/filter."""
        payload = {
            "eventFlag":        "SearchVendPublic",
            "pageNo":           str(page_no),
            "vendType":         vendor_type if vendor_type != "All" else "-1",
            "category":         category or "-1",
            "subCategory":      "-1",
            "grade":            "-1",
            "companyName":      "",
            "GTB_CRNo":         "",
            "PublicUrl":        "1",
            "CTRL_STRDIRECTION":"LTR",
            "CTRL_USERID":      "null",
            "CTRL_ROLEID":      "",
            "CTRL_SID":         "null",
        }
        headers = {
            "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
            "Referer":     f"{REGISTRY_URL}?eventFlag=RegVendorPublic",
            "Origin":      "https://etendering.tenderboard.gov.om",
            "Content-Type":"application/x-www-form-urlencoded",
        }
        try:
            resp = self.session.post(
                REGISTRY_URL,
                data=payload,
                headers=headers,
                cookies=self._session_cookies,
                timeout=30,
                allow_redirects=True,
            )
            if resp.ok and len(resp.text) > 1000:
                return resp.text
            logger.debug(f"TenderVendorRegistry POST page {page_no}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"TenderVendorRegistry POST error: {e}")
        return None

    # ── Playwright login (fallback) ────────────────────────────

    def _login(self) -> bool:
        """Authenticate via Playwright and capture session cookies."""
        try:
            self._ensure_browser()
            page = self._context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2000)

            # Fill login form
            for sel in ["#userId", "input[name='userId']", "input[type='text']"]:
                el = page.query_selector(sel)
                if el:
                    el.fill(self.tb_username)
                    break

            for sel in ["#password", "input[name='password']", "input[type='password']"]:
                el = page.query_selector(sel)
                if el:
                    el.fill(self.tb_password)
                    break

            # Submit
            for sel in ["button[type='submit']", "input[type='submit']", "#loginBtn"]:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    break

            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_timeout(2000)

            # Extract cookies
            cookies = self._context.cookies()
            self._session_cookies = {c["name"]: c["value"] for c in cookies}

            # Verify login succeeded
            current_url = page.url
            page.close()

            if "login" not in current_url.lower() and self._session_cookies:
                logger.info("TenderVendorRegistry: Login successful")
                return True

            logger.warning(f"TenderVendorRegistry: Login may have failed - URL: {current_url}")
            return False
        except Exception as e:
            logger.error(f"TenderVendorRegistry: Login error: {e}")
            return False

    # ── HTML parsing ───────────────────────────────────────────

    def _parse_companies(self, html: str,
                          default_category: str = "") -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        companies = []

        for row in soup.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells or not re.match(r'^\d+$', cells[0]):
                continue  # skip non-data rows

            c = self._parse_row(cells, default_category)
            if c:
                companies.append(c)

        return companies

    def _parse_row(self, cells: list[str], default_category: str) -> dict | None:
        if len(cells) < 8:
            return None

        # Columns: S No | Company Name | PTLC/CR | Address | Tel | Fax | Category | Expiry | Type | View
        name     = cells[1].strip()
        ptlc_cr  = cells[2].strip()
        address  = cells[3].strip()
        phone    = cells[4].strip()
        category = cells[6].strip() or default_category
        expiry   = cells[7].strip()
        ctype    = cells[8].strip() if len(cells) > 8 else ""

        if not name or len(name) < 3:
            return None

        # Parse CR number - format is "PTLC-CR" e.g. "00009556-1392582"
        cr_no = None
        if "-" in ptlc_cr:
            parts = ptlc_cr.split("-")
            cr_no = parts[-1].lstrip("0") or parts[-1]  # strip leading zeros
        elif ptlc_cr.isdigit():
            cr_no = ptlc_cr

        # Parse expiry date (DD-MM-YYYY)
        expiry_date = self._parse_date(expiry)
        expiry_str  = expiry_date.strftime("%Y-%m-%d") if expiry_date else None

        # Parse city from Arabic address
        city = self._parse_city(address)

        # Map category to vision2040 sector
        sector = self._map_sector(category)

        return {
            "company_name":          name[:200],
            "commercial_reg_no":     cr_no,
            "contact_phone":         phone[:20] if phone else None,
            "city":                  city,
            "vision2040_sector":     sector,
            "vendor_reg_expiry":     expiry_str,
            "vendor_category":       category[:200],
            "company_type":          ctype,
            "source":                self.source_name,
        }

    def _extract_max_page(self, html: str) -> int | None:
        m = re.search(r'hidMax["\s]+value["\s]+=?\s*["\']?(\d+)', html)
        if m:
            return int(m.group(1))
        m = re.search(r'of\s+(\d+)', html)
        if m:
            return int(m.group(1))
        return None

    # ── Filters ────────────────────────────────────────────────

    def _passes_filters(self, c: dict) -> bool:
        # Company type filter
        ctype = c.get("company_type", "")
        if ctype and ctype not in TARGET_COMPANY_TYPES:
            return False

        # Expiry filter: drop if expired > 1 year ago
        expiry_str = c.get("vendor_reg_expiry")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str)
                cutoff = datetime.now() - timedelta(days=MAX_EXPIRY_LAG_DAYS)
                if expiry < cutoff:
                    return False
            except ValueError:
                pass  # keep if date unparseable

        return True

    # ── Award cross-matching ───────────────────────────────────

    def _cross_match_awards(self):
        """
        For companies in DB sourced from vendor registry, check tender_awards table.
        If CR number or company name matches an award, set award_winner flag
        and update estimated revenue band from award totals.
        """
        try:
            vendors = self.db.query(
                "SELECT id, company_name, commercial_reg_no FROM oman_smes "
                "WHERE source = ? AND commercial_reg_no IS NOT NULL",
                [self.source_name]
            )
            logger.info(f"TenderVendorRegistry: Cross-matching {len(vendors)} vendors against awards")
            matched = 0

            for v in vendors:
                cr   = v.get("commercial_reg_no", "")
                name = v.get("company_name", "")

                # Look up in tender_awards by CR or name similarity
                awards = self.db.query(
                    """SELECT SUM(award_value_omr) as total, COUNT(*) as cnt,
                              MAX(award_date) as latest
                       FROM tender_awards
                       WHERE commercial_reg = ?
                          OR LOWER(company_name) LIKE LOWER(?)""",
                    [cr, f"%{name[:30]}%"]
                )
                if awards and awards[0].get("cnt", 0) > 0:
                    total_omr = awards[0].get("total") or 0
                    cnt       = awards[0].get("cnt", 0)
                    latest    = awards[0].get("latest", "")

                    # Estimate revenue band from cumulative awards
                    band = self._award_to_revenue_band(total_omr)

                    self.db.execute(
                        """UPDATE oman_smes
                           SET tender_award_total_omr  = ?,
                               tender_contract_count   = ?,
                               estimated_revenue_band  = COALESCE(
                                   NULLIF(estimated_revenue_band,'Unknown'), ?),
                               revenue_confidence      = 'tender_proxy'
                           WHERE id = ?""",
                        [total_omr, cnt, band, v["id"]]
                    )
                    matched += 1

            logger.info(f"TenderVendorRegistry: {matched}/{len(vendors)} vendors matched awards")
        except Exception as e:
            logger.error(f"TenderVendorRegistry: Award cross-match error: {e}")

    # ── Helpers ────────────────────────────────────────────────

    def _parse_date(self, s: str) -> datetime | None:
        for fmt in ["%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]:
            try:
                return datetime.strptime(s.strip(), fmt)
            except ValueError:
                continue
        return None

    def _parse_city(self, address: str) -> str:
        city_map = {
            "مسقط": "Muscat", "صحار": "Sohar", "صلالة": "Salalah",
            "نزوى": "Nizwa", "صور": "Sur", "دقم": "Duqm",
            "البريمي": "Buraimi", "إبراء": "Ibra", "عبري": "Ibri",
            "البريمي": "Buraimi", "الرستاق": "Rustaq", "بهلاء": "Bahla",
            "خصب": "Khasab", "مسندم": "Musandam", "ظفار": "Dhofar",
        }
        for arabic, english in city_map.items():
            if arabic in address:
                return english
        # English city names in address
        for city in ["Muscat", "Sohar", "Salalah", "Nizwa", "Sur", "Duqm",
                     "Buraimi", "Ibri", "Rustaq", "Khasab"]:
            if city.lower() in address.lower():
                return city
        return "Muscat"  # default

    def _map_sector(self, category: str) -> str:
        mapping = {
            "Construction":        "Construction",
            "Pipeline":            "Energy",
            "Electromechanical":   "Construction",
            "Telecommunication":   "ICT",
            "Information Technology": "ICT",
            "Consulting":          "Professional Services",
            "Supply":              "Manufacturing",
            "Services":            "Professional Services",
            "Training":            "Professional Services",
        }
        for key, sector in mapping.items():
            if key.lower() in category.lower():
                return sector
        return "Professional Services"

    def _award_to_revenue_band(self, total_omr: float) -> str:
        if total_omr >= 5_000_000:
            return "5M–20M"
        if total_omr >= 1_000_000:
            return "1M–5M"
        if total_omr >= 100_000:
            return "Under 1M"
        return "Unknown"


# ── One-shot HTML importer ─────────────────────────────────────────────────────

def import_html_file(html_path: str, csv_out: str = None):
    """
    Parse a locally saved vendor registry HTML file.
    - If CF_ACCOUNT_ID env var is set: loads directly into D1 database
    - Otherwise: exports to CSV at csv_out path (default: same dir as html_path)

    Usage:
        python -c "from scrapers.tender_vendor_registry import import_html_file; import_html_file('Registered Companies.html')"
    """
    import sys, io, csv, os
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    # Parse HTML (no DB needed)
    scraper_cls = TenderVendorRegistryScraper

    with open(html_path, encoding="utf-8", errors="replace") as f:
        html = f.read()

    # Use a minimal parser instance without DB
    class _Parser:
        _passes_filters = staticmethod(TenderVendorRegistryScraper._passes_filters.__func__
                                       if hasattr(TenderVendorRegistryScraper._passes_filters, '__func__')
                                       else lambda self, c: True)

    # Parse rows directly
    soup = BeautifulSoup(html, "lxml")
    tmp = TenderVendorRegistryScraper.__new__(TenderVendorRegistryScraper)
    tmp.source_name = "tender_vendor_registry"

    companies = []
    for row in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not cells or not re.match(r'^\d+$', cells[0]):
            continue
        c = tmp._parse_row(cells, default_category="")
        if c and tmp._passes_filters(c):
            companies.append(c)

    print(f"Parsed {len(companies)} companies from {html_path}")

    # Try DB load if credentials available
    if os.environ.get("CF_ACCOUNT_ID"):
        from .base import D1Client
        tmp.db    = D1Client()
        tmp.found = 0
        tmp.new   = 0
        from .base import BaseScraper
        # Borrow save_company from BaseScraper
        import json as _json
        from .base import ARRAY_FIELDS
        saved = new = 0
        for c in companies:
            tmp.found += 1
            c.setdefault("source", "tender_vendor_registry")
            # encode
            out = dict(c)
            for field in ARRAY_FIELDS:
                if field in out and isinstance(out[field], list):
                    out[field] = _json.dumps(out[field])
            for k, v in out.items():
                if isinstance(v, bool):
                    out[k] = int(v)
            try:
                cr = out.get("commercial_reg_no")
                if cr:
                    rows = tmp.db.query("SELECT id FROM oman_smes WHERE commercial_reg_no = ?", [cr])
                else:
                    rows = tmp.db.query("SELECT id FROM oman_smes WHERE company_name = ?", [out["company_name"]])
                if rows:
                    update = {k: v for k, v in out.items() if v is not None}
                    sets   = ", ".join(f"{k} = ?" for k in update)
                    vals   = list(update.values()) + [rows[0]["id"]]
                    tmp.db.execute(f"UPDATE oman_smes SET {sets} WHERE id = ?", vals)
                else:
                    cols  = ", ".join(out.keys())
                    ph    = ", ".join("?" * len(out))
                    tmp.db.execute(f"INSERT INTO oman_smes ({cols}) VALUES ({ph})", list(out.values()))
                    new += 1
                saved += 1
            except Exception as e:
                print(f"  Error saving {c.get('company_name')}: {e}")
        print(f"DB load done - saved={saved} new={new}")
    else:
        # Export to CSV
        if not csv_out:
            csv_out = html_path.replace(".html", ".csv")
        with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
            if companies:
                writer = csv.DictWriter(f, fieldnames=list(companies[0].keys()))
                writer.writeheader()
                writer.writerows(companies)
        print(f"No CF credentials - exported {len(companies)} companies to {csv_out}")
        print("To load into DB: set CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN then re-run")
