"""
JSRS Supplier Scraper
─────────────────────
Two modes controlled by env var JSRS_BUYER_MODE:

  PUBLIC MODE  (default, no login):
    - Scrapes businessgateways.com public supplier search
    - Extracts company name, city, category from partial results
    - Sends standard browser headers and falls back to browser rendering for JS pages
    - Supplemented by JSRS verification page (checks single company status)

  BUYER MODE   (JSRS_BUYER_MODE=true + JSRS_USERNAME + JSRS_PASSWORD):
    - Full authenticated session via JSRS portal
    - Access to complete certified supplier directory (thousands of entries)
    - Returns: CR no, cert number, expiry, all registered categories
    - This is the gold standard - push for buyer account access

Set JSRS_BUYER_MODE=true once you have credentials. The system will
automatically switch without any code changes.
"""
import os, re, logging, time
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

JSRS_BASE = "https://jsrs.businessgateways.com"
JSRS_PUBLIC_BASE = "https://businessgateways.com"

# JSRS category codes aligned to OIA protected list sectors
TARGET_JSRS_CATEGORIES = [
    "Mechanical Works", "Electrical Works", "Civil Works",
    "IT Services", "Telecommunications", "Logistics & Transport",
    "Catering & Cleaning", "Healthcare", "Industrial Services",
    "Manufacturing", "Oil Field Services", "Chemical Supplies",
    "Safety Equipment", "Instrumentation", "Welding & Fabrication",
    "HVAC", "Plumbing", "Environmental Services", "Laboratory Services",
    "Training & Consultancy", "Waste Management", "Food Supplies",
]


class JSRSScraper(BaseScraper):
    source_name = "jsrs"

    def __init__(self):
        super().__init__()
        self.buyer_mode = os.getenv("JSRS_BUYER_MODE", "false").lower() == "true"
        self.jsrs_username = os.getenv("JSRS_USERNAME", "")
        self.jsrs_password = os.getenv("JSRS_PASSWORD", "")

    def scrape(self):
        if self.buyer_mode and self.jsrs_username:
            logger.info("JSRS: Running in BUYER MODE (authenticated)")
            self._buyer_mode_scrape()
        else:
            logger.info("JSRS: Running in PUBLIC MODE")
            self._public_mode_scrape()

    # ── PUBLIC MODE ────────────────────────────────────────────
    # NOTE: JSRS supplier directory requires buyer authentication.
    # Public URLs return marketing pages, not supplier listings.
    # Set JSRS_BUYER_MODE=true + credentials to unlock full directory.

    def _public_mode_scrape(self):
        logger.warning(
            "JSRS public mode: supplier directory requires buyer login. "
            "Add JSRS_BUYER_MODE=true + credentials to unlock. Skipping category scrape."
        )
        self._verify_known_companies()
        self._scrape_jsrs_news()

    def _parse_supplier_listings(self, soup: BeautifulSoup, category: str) -> list[dict]:
        companies = []
        selectors = [
            ".supplier-card", ".company-listing", ".supplier-item",
            ".vendor-card", "tr.supplier-row", ".result-card",
            "[data-company]", ".jsrs-supplier",
        ]
        for sel in selectors:
            cards = soup.select(sel)
            if cards:
                for card in cards:
                    c = self._parse_supplier_card(card, category)
                    if c:
                        companies.append(c)
                break

        # Table fallback
        if not companies:
            for table in soup.find_all("table"):
                headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
                if any(kw in str(headers) for kw in ["company","supplier","name","cert"]):
                    for row in table.find_all("tr")[1:]:
                        cells = [td.get_text(strip=True) for td in row.find_all("td")]
                        c = self._parse_table_row(cells, headers, category)
                        if c:
                            companies.append(c)

        return companies

    def _parse_supplier_card(self, card, category: str) -> dict | None:
        text = card.get_text(separator="\n", strip=True)
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            return None

        name = lines[0] if lines else None
        if not name or len(name) < 4:
            return None

        # Check if cert status visible
        cert_status = "Unknown"
        if re.search(r'\bactive\b', text, re.I):
            cert_status = "Active"
        elif re.search(r'\bexpired\b', text, re.I):
            cert_status = "Expired"

        cr_match = re.search(r'\b(\d{7,10})\b', text)
        cert_match = re.search(r'JSRS[-/]?(\w{6,})', text, re.I)
        phone_match = re.search(r'\+968\s*[\d\s-]{8,}', text)
        email_match = re.search(r'[\w.+-]+@[\w-]+\.\w+', text)

        # Extract city
        city = "Muscat"
        for gov in ["Muscat","Sohar","Salalah","Nizwa","Sur","Duqm","Buraimi","Ibri","Adam"]:
            if gov.lower() in text.lower():
                city = gov
                break

        return {
            "company_name":      name[:200],
            "commercial_reg_no": cr_match.group(1) if cr_match else None,
            "jsrs_certified":    cert_status == "Active",
            "jsrs_cert_status":  cert_status,
            "jsrs_cert_number":  cert_match.group(0) if cert_match else None,
            "jsrs_categories":   [category],
            "contact_phone":     phone_match.group(0) if phone_match else None,
            "contact_email":     email_match.group(0) if email_match else None,
            "city":              city,
            "source":            self.source_name,
        }

    def _parse_table_row(self, cells: list, headers: list, category: str) -> dict | None:
        if len(cells) < 2:
            return None
        record = {
            "jsrs_categories": [category],
            "source": self.source_name,
        }
        for i, h in enumerate(headers):
            if i >= len(cells): break
            val = cells[i].strip()
            if any(k in h for k in ["company","name","supplier"]):
                record["company_name"] = val[:200]
            elif any(k in h for k in ["cert","status"]):
                record["jsrs_cert_status"] = "Active" if "active" in val.lower() else "Expired"
                record["jsrs_certified"]   = "active" in val.lower()
                record["jsrs_cert_number"] = val if re.match(r'JSRS', val, re.I) else None
            elif any(k in h for k in ["expiry","valid","date"]):
                record["jsrs_cert_expiry"] = val[:20]
            elif any(k in h for k in ["city","location","gov"]):
                record["city"] = val
            elif any(k in h for k in ["cr","reg","commercial"]):
                record["commercial_reg_no"] = val
            elif any(k in h for k in ["phone","tel","mobile"]):
                record["contact_phone"] = val
            elif "email" in h:
                record["contact_email"] = val
        return record if record.get("company_name") else None

    def _verify_known_companies(self):
        """
        Bulk-verify JSRS status for all DB companies that have a CR number
        but no confirmed JSRS status. Uses the public /verify endpoint.
        Falls back to browser rendering if plain HTTP is blocked.
        Processes in batches of 200 to avoid long runs.
        """
        try:
            smes = self.db.query(
                "SELECT id, company_name, commercial_reg_no FROM oman_smes "
                "WHERE commercial_reg_no IS NOT NULL "
                "AND (jsrs_cert_status IS NULL OR jsrs_cert_status = 'Unknown') "
                "ORDER BY id"
                # No LIMIT - process all unverified companies
            )
            logger.info(f"JSRS verify: {len(smes)} companies with CR to check")
            resolved = 0

            for sme in smes:
                cr = sme.get("commercial_reg_no", "").strip()
                if not cr:
                    continue

                result = self._verify_single_cr(cr)
                if result["status"] != "Unknown":
                    self.db.execute(
                        """UPDATE oman_smes
                           SET jsrs_certified = ?, jsrs_cert_status = ?,
                               jsrs_cert_number = COALESCE(jsrs_cert_number, ?),
                               jsrs_cert_expiry = COALESCE(jsrs_cert_expiry, ?)
                           WHERE id = ?""",
                        [
                            1 if result["status"] == "Active" else 0,
                            result["status"],
                            result.get("cert_number"),
                            result.get("cert_expiry"),
                            sme["id"],
                        ],
                    )
                    resolved += 1
                    logger.debug(
                        f"JSRS verify: {sme['company_name']} CR={cr} → {result['status']}"
                    )
                self.polite_sleep(1.5)

            logger.info(f"JSRS verify: resolved {resolved}/{len(smes)} companies")
        except Exception as e:
            logger.error(f"JSRS verify error: {e}")

    def _verify_single_cr(self, cr: str) -> dict:
        """
        Hit the JSRS public verification endpoint for one CR number.
        Returns dict with 'status', optionally 'cert_number', 'cert_expiry'.
        Tries plain HTTP first; falls back to browser if blocked.
        """
        result = {"status": "Unknown"}

        # Attempt 1: plain HTTP (fast)
        verify_url = f"{JSRS_BASE}/verify?cr={cr}"
        resp = self.get(verify_url)
        if resp:
            parsed = self._parse_verify_response(resp.text)
            if parsed["status"] != "Unknown":
                return parsed

        # Attempt 2: alternative verify URL patterns
        for url in [
            f"{JSRS_BASE}/supplier/verify?cr={cr}",
            f"{JSRS_PUBLIC_BASE}/verify-supplier?cr={cr}",
            f"{JSRS_PUBLIC_BASE}/supplier-verification?commercial_reg={cr}",
        ]:
            resp = self.get(url)
            if resp:
                parsed = self._parse_verify_response(resp.text)
                if parsed["status"] != "Unknown":
                    return parsed

        # Attempt 3: browser rendering (JS-rendered response)
        html = self.cf_browser_get(verify_url, timeout=20_000)
        if html:
            return self._parse_verify_response(html)

        return result

    def _parse_verify_response(self, html: str) -> dict:
        """Extract JSRS status, cert number, and expiry from verification page HTML."""
        soup   = BeautifulSoup(html, "lxml")
        text   = soup.get_text(separator=" ")
        result = {"status": "Unknown"}

        if re.search(r'\bactive\b', text, re.I):
            result["status"] = "Active"
        elif re.search(r'\bexpired\b', text, re.I):
            result["status"] = "Expired"
        elif re.search(r'\bsuspended\b', text, re.I):
            result["status"] = "Suspended"
        elif re.search(r'\bnot\s+found\b|\bno\s+record\b|\bnot\s+registered\b', text, re.I):
            result["status"] = "Not Registered"
            return result

        cert_match = re.search(r'JSRS[-/]?\s*([A-Z0-9]{4,12})', text, re.I)
        if cert_match:
            result["cert_number"] = cert_match.group(0).strip()

        expiry_match = re.search(
            r'(?:expir(?:y|es?|ed?)|valid\s+(?:until|through)|renewal)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})',
            text, re.I
        )
        if expiry_match:
            result["cert_expiry"] = expiry_match.group(1)

        return result

    def _scrape_jsrs_news(self):
        """Parse JSRS press releases/news that name specific certified companies."""
        news_urls = [
            f"{JSRS_PUBLIC_BASE}/news",
            f"{JSRS_PUBLIC_BASE}/blog",
            f"{JSRS_PUBLIC_BASE}/press-releases",
        ]
        for url in news_urls:
            html = self.cf_browser_get(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            # Extract company names mentioned alongside "JSRS certified" language
            text = soup.get_text()
            for match in re.finditer(
                r'([A-Z][a-zA-Z\s&]{5,50}(?:LLC|SAOC|SAOG|Co\.|Ltd|Est\.))\s+(?:received|awarded|certified|holds)\s+(?:JSRS|ICV)',
                text
            ):
                self.save_company({
                    "company_name":    match.group(1).strip(),
                    "jsrs_certified":  True,
                    "jsrs_cert_status":"Active",
                    "source":          self.source_name,
                })
            self.polite_sleep(3)

    # ── BUYER MODE ─────────────────────────────────────────────

    def _buyer_mode_scrape(self):
        """
        Full authenticated JSRS directory scrape via Playwright.
        Requires: JSRS_USERNAME, JSRS_PASSWORD env vars.
        Activates when JSRS_BUYER_MODE=true.
        """
        if not self._authenticate():
            logger.error("JSRS buyer login failed - check credentials")
            return

        logger.info("JSRS authenticated - scraping full supplier directory")

        for category in TARGET_JSRS_CATEGORIES:
            url = (
                f"{JSRS_BASE}/buyers/supplier-search"
                f"?country=OM&status=Active&category={category.replace(' ', '+')}"
            )
            # browser_get() reuses the authenticated Playwright context
            html = self.cf_browser_get(url)
            if not html:
                continue
            soup      = BeautifulSoup(html, "lxml")
            companies = self._parse_authenticated_directory(soup, category)
            logger.info(f"JSRS buyer [{category}]: {len(companies)} suppliers")
            for c in companies:
                self.save_company(c)
            self.polite_sleep(2)

            for page in range(2, 200):
                paged_html = self.cf_browser_get(f"{url}&page={page}")
                if not paged_html:
                    break
                soup2 = BeautifulSoup(paged_html, "lxml")
                more  = self._parse_authenticated_directory(soup2, category)
                if not more:
                    break
                for c in more:
                    self.save_company(c)
                self.polite_sleep(1.5)

    def _authenticate(self) -> bool:
        """Log in via Playwright - stores session cookies in shared browser context."""
        self._ensure_browser()
        page = None
        try:
            page = self._context.new_page()
            page.goto(f"{JSRS_BASE}/login", wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1_500)

            # Fill credentials - try common selector patterns
            page.fill(
                "input[name='username'], input[name='email'], input[type='email']",
                self.jsrs_username,
            )
            page.fill("input[name='password'], input[type='password']", self.jsrs_password)
            page.click("button[type='submit'], input[type='submit'], .login-btn, .btn-login")
            page.wait_for_load_state("networkidle", timeout=15_000)

            success = (
                "dashboard" in page.url.lower()
                or "supplier" in page.url.lower()
                or page.is_visible(".dashboard, .supplier-list, .buyer-dashboard")
            )
            return success
        except Exception as e:
            logger.error(f"JSRS login error: {e}")
            return False
        finally:
            if page:
                page.close()

    def _parse_authenticated_directory(self, soup: BeautifulSoup, category: str) -> list[dict]:
        """Parse the richer authenticated supplier directory view."""
        companies = []
        for row in soup.select("tr.supplier-row, .supplier-entry, .vendor-row, tr[data-id]"):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 4:
                continue

            c = {
                "company_name":      cells[0][:200] if cells else None,
                "commercial_reg_no": cells[1] if len(cells) > 1 else None,
                "jsrs_cert_number":  cells[2] if len(cells) > 2 else None,
                "jsrs_cert_status":  "Active" if "active" in str(cells).lower() else "Expired",
                "jsrs_certified":    "active" in str(cells).lower(),
                "jsrs_categories":   [category],
                "city":              cells[4] if len(cells) > 4 else "Muscat",
                "source":            self.source_name,
            }
            if c["company_name"]:
                companies.append(c)
        return companies
