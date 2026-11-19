"""
Ministry of Commerce, Industry & Investment Promotion (MoCI) Scraper
──────────────────────────────────────────────────────────────────────
Queries the Oman MoCI commercial registry at mocioman.gov.om.

MoCI is the authoritative source for all Oman commercial registrations.
The public-facing CR inquiry system allows searching by:
  - Activity sector / ISIC code (aligns to OIA protected codes)
  - Governorate
  - Company name keyword

Strategy:
  1. Search by OIA-aligned activity codes to find SMEs in protected sectors
  2. Search by sector keywords across all governorates
  3. CR verification for companies already in DB without full details

Extracted fields: company_name, commercial_reg_no, city, activity sector,
                  registration status, contact details where exposed.
"""
import re, logging, time
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

MOCI_BASE = "https://www.mocioman.gov.om"

# URL patterns for MoCI CR search - site has been redesigned multiple times
SEARCH_TEMPLATES = [
    "{base}/en/cr/search?sector={sector}",
    "{base}/en/services/cr-inquiry?activity={sector}",
    "{base}/crSearch?activity={sector}&governorate={gov}",
    "{base}/en/commercial-registry?q={sector}",
    "{base}/services/commercial-registration/search?sector={sector}",
]

# Alternative MoCI portals
ALT_BASES = [
    "https://cr.mocioman.gov.om",
    "https://portal.mocioman.gov.om",
    "https://www.investineoman.com",   # Investment portal, exposes CR data
]

# Activity sectors aligned to OIA protected codes
OIA_SECTORS = [
    "manufacturing",
    "construction",
    "logistics",
    "transport",
    "information technology",
    "telecommunications",
    "healthcare",
    "agriculture",
    "food processing",
    "energy",
    "engineering",
    "chemicals",
    "metal fabrication",
    "electrical",
    "maintenance",
    "environmental services",
    "waste management",
    "industrial services",
]

# Oman governorates for cross-referencing
GOVERNORATES = [
    "muscat", "dhofar", "north batinah", "south batinah",
    "north sharqiyah", "south sharqiyah", "dakhiliyah",
    "dhahirah", "buraimi", "wusta", "musandam",
]

# CR number ranges to probe - Oman CR numbers are 7 digits, ~1M–1.5M active
# Probing specific ranges known to be SME registrations (registered 2010–2024)
CR_PROBE_RANGES = [
    (1000000, 1000200),  # Sample range - expand if verification endpoint works
]


class MoCI_Scraper(BaseScraper):
    source_name = "moci"

    def scrape(self):
        logger.info("MoCI: Starting commercial registry scrape")

        # Pass 1: sector search across all known URL patterns
        working = self._find_working_search(OIA_SECTORS[0])
        if working:
            logger.info(f"MoCI: search URL working: {working}")
            for sector in OIA_SECTORS:
                self._scrape_sector(sector, working)
                self.polite_sleep(3)
        else:
            logger.warning("MoCI: standard search URLs not responding - trying alt portals")
            self._try_alt_portals()

        # Pass 2: enrich existing DB companies with MoCI data
        self._enrich_from_moci()

    # ── Discovery ──────────────────────────────────────────────

    def _find_working_search(self, test_sector: str) -> str | None:
        for tmpl in SEARCH_TEMPLATES:
            for gov in ["muscat", ""]:
                url  = tmpl.format(base=MOCI_BASE, sector=test_sector, gov=gov)
                html = self._fetch(url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "lxml")
                # Check if it looks like a results page
                if soup.select_one(
                    "table tr, .company-card, .cr-result, .result-item, [class*='company']"
                ):
                    return tmpl
        return None

    def _try_alt_portals(self):
        for base in ALT_BASES:
            for sector in OIA_SECTORS[:5]:  # Test first 5 sectors
                for tmpl in [
                    "{base}/search?q={sector}",
                    "{base}/companies?sector={sector}",
                    "{base}/directory?industry={sector}",
                ]:
                    url  = tmpl.format(base=base, sector=sector)
                    html = self._fetch(url)
                    if not html:
                        continue
                    soup      = BeautifulSoup(html, "lxml")
                    companies = self._extract_companies(soup, sector=sector)
                    if companies:
                        logger.info(f"MoCI alt [{base}|{sector}]: {len(companies)} companies")
                        for c in companies:
                            self.save_company(c)
                        self.polite_sleep(3)
                        break

    # ── Sector scraping ────────────────────────────────────────

    def _scrape_sector(self, sector: str, url_template: str):
        for gov in GOVERNORATES:
            url  = url_template.format(base=MOCI_BASE, sector=sector, gov=gov)
            html = self._fetch(url)
            if not html:
                continue
            soup      = BeautifulSoup(html, "lxml")
            companies = self._extract_companies(soup, sector=sector,
                                                city=gov.title().split()[0])
            if companies:
                logger.info(f"MoCI [{sector}|{gov}]: {len(companies)} companies")
                for c in companies:
                    self.save_company(c)
                self._paginate(url, sector=sector, city=gov.title().split()[0])
            self.polite_sleep(2)

    def _paginate(self, base_url: str, sector: str | None, city: str | None,
                  max_pages: int = 50):
        for page in range(2, max_pages + 1):
            sep  = "&" if "?" in base_url else "?"
            url  = f"{base_url}{sep}page={page}"
            html = self._fetch(url)
            if not html:
                break
            soup      = BeautifulSoup(html, "lxml")
            companies = self._extract_companies(soup, sector=sector, city=city)
            if not companies:
                break
            for c in companies:
                self.save_company(c)
            self.polite_sleep(2)

    # ── DB enrichment pass ──────────────────────────────────────

    def _enrich_from_moci(self):
        """
        For companies already in DB without city/sector, search MoCI by name.
        Fills gaps in existing records.
        """
        try:
            smes = self.db.query(
                "SELECT id, company_name, commercial_reg_no FROM oman_smes "
                "WHERE (city IS NULL OR city = '') "
                "AND source != 'moci' LIMIT 200"
            )
            logger.info(f"MoCI enrich: {len(smes)} companies to look up")
            for sme in smes:
                name = sme.get("company_name", "")
                cr   = sme.get("commercial_reg_no", "")
                if not name:
                    continue

                data = self._lookup_company(name=name, cr=cr)
                if data:
                    update_fields = {k: v for k, v in data.items() if v}
                    if update_fields:
                        sets = ", ".join(f"{k} = ?" for k in update_fields)
                        vals = list(update_fields.values()) + [sme["id"]]
                        self.db.execute(
                            f"UPDATE oman_smes SET {sets} WHERE id = ?", vals
                        )
                self.polite_sleep(2)
        except Exception as e:
            logger.error(f"MoCI enrich error: {e}")

    def _lookup_company(self, name: str, cr: str = "") -> dict | None:
        """Search MoCI for a specific company by name or CR."""
        search_targets = []
        if cr:
            search_targets.append(f"{MOCI_BASE}/en/cr/inquiry?cr={cr}")
        # Keyword search
        q = name.split()[0]  # First word of company name
        search_targets += [
            f"{MOCI_BASE}/en/cr/search?q={q}",
            f"{MOCI_BASE}/crSearch?name={q}",
        ]

        for url in search_targets:
            html = self._fetch(url)
            if not html:
                continue
            soup      = BeautifulSoup(html, "lxml")
            companies = self._extract_companies(soup)
            for c in companies:
                if name.lower() in c.get("company_name", "").lower():
                    return c
        return None

    # ── Extraction ─────────────────────────────────────────────

    def _extract_companies(self, soup: BeautifulSoup,
                           sector: str | None = None,
                           city: str | None = None) -> list[dict]:
        companies = []

        # Table rows - MoCI typically returns tabular results
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                # Try first row as header
                rows = table.find_all("tr")
                if rows:
                    headers = [td.get_text(strip=True).lower() for td in rows[0].find_all("td")]
                    rows = rows[1:]
                else:
                    continue
            else:
                rows = table.find_all("tr")[1:]

            if not any(k in str(headers) for k in ["company", "name", "cr", "commercial", "trade"]):
                continue

            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                c = self._parse_row(cells, headers, sector=sector, city=city)
                if c:
                    companies.append(c)

        if companies:
            return companies

        # Card layouts
        for sel in [".cr-result", ".company-card", ".result-card", ".registry-item",
                    "[class*='company']", "[class*='cr-']", "article"]:
            for card in soup.select(sel):
                c = self._parse_card(card, sector=sector, city=city)
                if c:
                    companies.append(c)
            if companies:
                return companies

        return companies

    def _parse_row(self, cells: list, headers: list,
                   sector: str | None, city: str | None) -> dict | None:
        if not cells or len(cells) < 2:
            return None
        record = {"vision2040_sector": sector, "city": city, "source": self.source_name}

        for i, h in enumerate(headers):
            if i >= len(cells):
                break
            val = cells[i].strip()
            if not val:
                continue
            hl = h.lower()
            if any(k in hl for k in ["company", "name", "trade", "establishment", "business"]):
                if val and len(val) >= 3:
                    record["company_name"] = val[:200]
            elif any(k in hl for k in ["cr", "reg", "commercial", "registration number"]):
                record["commercial_reg_no"] = re.sub(r'\D', '', val)[:15]
            elif any(k in hl for k in ["phone", "tel", "mobile"]):
                record["contact_phone"] = val[:20]
            elif "email" in hl:
                record["contact_email"] = val.lower()[:100]
            elif any(k in hl for k in ["city", "governorate", "location", "wilayat"]):
                record["city"] = val[:50]
            elif any(k in hl for k in ["sector", "activity", "industry", "category"]):
                record["vision2040_sector"] = val[:100]
            elif any(k in hl for k in ["status", "state"]):
                if "active" in val.lower():
                    record["jsrs_cert_status"] = "Unknown"  # MoCI status ≠ JSRS status
            elif any(k in hl for k in ["website", "url", "web"]):
                record["website"] = val

        return record if record.get("company_name") else None

    def _parse_card(self, card, sector: str | None, city: str | None) -> dict | None:
        name_el = card.select_one(
            "h2, h3, h4, h5, .name, .company-name, .cr-name, [class*='name']"
        )
        if not name_el:
            return None
        name = name_el.get_text(strip=True)
        if not name or len(name) < 3:
            return None

        text        = card.get_text(separator=" ")
        cr_match    = re.search(r'\b(\d{7,10})\b', text)
        phone_match = re.search(r'(?:\+968|00968)?[\s.-]?[279]\d{7}', text)
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w{2,}', text)

        sector_el = card.select_one(".activity, .sector, .category, [class*='sector']")
        card_sector = sector_el.get_text(strip=True) if sector_el else sector

        city_el = card.select_one(".city, .governorate, .location, [class*='gov']")
        card_city = city_el.get_text(strip=True) if city_el else city

        return {
            "company_name":      name[:200],
            "commercial_reg_no": cr_match.group(1) if cr_match else None,
            "contact_phone":     phone_match.group(0).strip() if phone_match else None,
            "contact_email":     email_match.group(0).lower() if email_match else None,
            "city":              card_city or "Muscat",
            "vision2040_sector": card_sector,
            "source":            self.source_name,
        }

    # ── Helpers ────────────────────────────────────────────────

    def _fetch(self, url: str, retries: int = 2) -> str | None:
        for attempt in range(retries + 1):
            html = self.cf_browser_get(url, timeout=45_000)
            if html and len(html) > 500:
                return html
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
        return None
