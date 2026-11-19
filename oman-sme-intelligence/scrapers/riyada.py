"""
Riyada SME Registry Scraper
─────────────────────────────
Scrapes the Authority for SME Development (Riyada) at riyada.om.

Riyada is Oman's official SME development body and maintains a public
registry of registered Omani SMEs with sector, governorate, and contact
data. This is the most targeted source - every result is already an SME.

Strategy:
  1. SME directory / member search by sector + governorate
  2. Success stories pages (name certified companies explicitly)
  3. Programme participant lists (Tanmia, Wathbah, etc.)
"""
import re, logging, time
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

RIYADA_BASE = "https://riyada.om"

# Riyada sector slugs / categories
TARGET_SECTORS = [
    "manufacturing",
    "construction",
    "logistics",
    "technology",
    "healthcare",
    "agriculture",
    "food-beverages",
    "energy",
    "services",
    "trading",
    "education",
    "tourism",
    "fisheries",
    "media",
]

GOVERNORATES = [
    "muscat", "dhofar", "sohar", "batinah", "sharqiyah",
    "dakhiliyah", "dhahirah", "buraimi", "wusta", "musandam",
]

# URL patterns to probe - Riyada has redesigned several times
DIRECTORY_TEMPLATES = [
    "{base}/sme-directory",
    "{base}/en/sme-directory",
    "{base}/directory",
    "{base}/our-smes",
    "{base}/registered-smes",
    "{base}/smes",
    "{base}/en/register/sme-directory",
    "{base}/beneficiaries",
    "{base}/success-stories",
    "{base}/en/success-stories",
]

SECTOR_TEMPLATES = [
    "{base}/sme-directory?sector={sector}",
    "{base}/directory?category={sector}",
    "{base}/our-smes?sector={sector}",
    "{base}/smes?industry={sector}",
]

GOV_TEMPLATES = [
    "{base}/sme-directory?governorate={gov}",
    "{base}/directory?governorate={gov}",
    "{base}/our-smes?region={gov}",
]


class RiyadaScraper(BaseScraper):
    source_name = "riyada"

    def scrape(self):
        logger.info("Riyada: Starting SME directory scrape")

        # Pass 1: probe known directory URL patterns
        working_url = self._find_directory_url()

        if working_url:
            logger.info(f"Riyada: directory found at {working_url}")
            self._scrape_directory(working_url)
        else:
            logger.warning("Riyada: no main directory URL worked - trying sector/governorate paths")

        # Pass 2: sector-filtered pages
        for sector in TARGET_SECTORS:
            for tmpl in SECTOR_TEMPLATES:
                url  = tmpl.format(base=RIYADA_BASE, sector=sector)
                html = self._fetch(url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "lxml")
                companies = self._extract_companies(soup, sector=sector)
                if companies:
                    logger.info(f"Riyada [{sector}]: {len(companies)} SMEs")
                    for c in companies:
                        self.save_company(c)
                    self._paginate(url, sector=sector)
                    break
            self.polite_sleep(2)

        # Pass 3: governorate pages
        for gov in GOVERNORATES:
            for tmpl in GOV_TEMPLATES:
                url  = tmpl.format(base=RIYADA_BASE, gov=gov)
                html = self._fetch(url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "lxml")
                companies = self._extract_companies(soup, city=gov.capitalize())
                if companies:
                    logger.info(f"Riyada [{gov}]: {len(companies)} SMEs")
                    for c in companies:
                        self.save_company(c)
                    break
            self.polite_sleep(2)

    # ── Discovery ──────────────────────────────────────────────

    def _find_directory_url(self) -> str | None:
        for tmpl in DIRECTORY_TEMPLATES:
            url  = tmpl.format(base=RIYADA_BASE)
            html = self._fetch(url)
            if not html:
                continue
            soup      = BeautifulSoup(html, "lxml")
            companies = self._extract_companies(soup)
            if companies:
                for c in companies:
                    self.save_company(c)
                return url
            # Page loaded but no companies - still a valid URL for pagination
            # if it has listing structure signals
            if soup.select_one(".sme-card, .company-card, .member-card, .sme-item, article"):
                return url
        return None

    def _scrape_directory(self, base_url: str):
        self._paginate(base_url, sector=None)

    # ── Pagination ─────────────────────────────────────────────

    def _paginate(self, base_url: str, sector: str | None, max_pages: int = 100):
        for page in range(2, max_pages + 1):
            sep  = "&" if "?" in base_url else "?"
            url  = f"{base_url}{sep}page={page}"
            html = self._fetch(url)
            if not html:
                break
            soup      = BeautifulSoup(html, "lxml")
            companies = self._extract_companies(soup, sector=sector)
            if not companies:
                break
            for c in companies:
                self.save_company(c)
            logger.debug(f"Riyada page {page}: {len(companies)} companies")
            self.polite_sleep(2)

    # ── Extraction ─────────────────────────────────────────────

    def _extract_companies(self, soup: BeautifulSoup,
                           sector: str | None = None,
                           city: str | None = None) -> list[dict]:
        companies = []

        # Card-based layouts
        card_selectors = [
            ".sme-card", ".company-card", ".member-card", ".sme-item",
            ".business-card", ".directory-item", ".profile-card",
            "article.sme", "article.company", ".result-item",
            "[class*='sme-']", "[class*='company-']",
        ]
        for sel in card_selectors:
            for card in soup.select(sel):
                c = self._parse_card(card, sector=sector, city=city)
                if c:
                    companies.append(c)
            if companies:
                return companies

        # Table layouts
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(k in str(headers) for k in ["company", "name", "sme", "business"]):
                continue
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                c = self._parse_row(cells, headers, sector=sector, city=city)
                if c:
                    companies.append(c)
            if companies:
                return companies

        # List fallback - success stories, programme listings
        for li in soup.select("ul.sme-list li, ul.companies li, .directory-list li"):
            text = li.get_text(strip=True)
            if self._looks_like_company(text):
                link  = li.find("a")
                href  = link["href"] if link and link.get("href") else None
                companies.append(self._minimal_record(text, href, sector=sector, city=city))

        # Success stories - names in headings
        if not companies:
            for heading in soup.select("h2, h3, h4"):
                text = heading.get_text(strip=True)
                if self._looks_like_company(text):
                    link  = heading.find("a") or heading.find_parent("a")
                    href  = link["href"] if link and link.get("href") else None
                    companies.append(self._minimal_record(text, href, sector=sector, city=city))

        return companies

    def _parse_card(self, card, sector: str | None, city: str | None) -> dict | None:
        name_el = card.select_one(
            "h2, h3, h4, h5, .sme-name, .company-name, .name, "
            "[class*='name'], [class*='title']"
        )
        if not name_el:
            return None
        name = name_el.get_text(strip=True)
        if not self._looks_like_company(name):
            return None

        text        = card.get_text(separator=" ")
        cr_match    = re.search(r'\b(\d{7,10})\b', text)
        phone_match = re.search(r'(?:\+968|00968)?[\s.-]?[279]\d{7}', text)
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w{2,}', text)
        link        = card.find("a", href=True)

        # Sector from card if not passed
        sector_el = card.select_one(".sector, .category, .industry, [class*='sector']")
        card_sector = sector_el.get_text(strip=True) if sector_el else sector

        # City from card
        city_el = card.select_one(".city, .location, .governorate, [class*='city'], [class*='gov']")
        card_city = city_el.get_text(strip=True) if city_el else city
        if not card_city:
            for gov in ["Muscat","Sohar","Salalah","Nizwa","Sur","Duqm","Buraimi","Ibri"]:
                if gov.lower() in text.lower():
                    card_city = gov
                    break

        website = None
        if link:
            href = link["href"]
            if href and href.startswith("http") and "riyada.om" not in href:
                website = href

        return {
            "company_name":      name[:200],
            "commercial_reg_no": cr_match.group(1) if cr_match else None,
            "contact_phone":     self._clean_phone(phone_match.group(0)) if phone_match else None,
            "contact_email":     email_match.group(0).lower() if email_match else None,
            "website":           website,
            "city":              card_city or "Muscat",
            "vision2040_sector": card_sector,
            "source":            self.source_name,
        }

    def _parse_row(self, cells: list, headers: list,
                   sector: str | None, city: str | None) -> dict | None:
        if len(cells) < 2:
            return None
        record = {"vision2040_sector": sector, "city": city, "source": self.source_name}
        for i, h in enumerate(headers):
            if i >= len(cells):
                break
            val = cells[i].strip()
            if not val:
                continue
            if any(k in h for k in ["company", "name", "sme", "business", "trade"]):
                if self._looks_like_company(val):
                    record["company_name"] = val[:200]
            elif any(k in h for k in ["cr", "reg", "commercial", "registration"]):
                record["commercial_reg_no"] = val
            elif any(k in h for k in ["phone", "tel", "mobile", "contact"]):
                record["contact_phone"] = val
            elif "email" in h:
                record["contact_email"] = val.lower()
            elif any(k in h for k in ["city", "location", "governorate", "region"]):
                record["city"] = val
            elif any(k in h for k in ["sector", "category", "industry", "activity"]):
                record["vision2040_sector"] = val
            elif any(k in h for k in ["website", "url", "web"]):
                record["website"] = val
        return record if record.get("company_name") else None

    def _minimal_record(self, name: str, website: str | None,
                        sector: str | None, city: str | None) -> dict:
        return {
            "company_name":      name[:200],
            "website":           website,
            "city":              city or "Muscat",
            "vision2040_sector": sector,
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

    def _clean_phone(self, raw: str) -> str:
        digits = re.sub(r'[^\d+]', '', raw)
        if digits.startswith("00968"):
            digits = "+" + digits[2:]
        if len(digits) == 8 and digits[0] in "279":
            digits = "+968" + digits
        return digits[:20]

    def _looks_like_company(self, name: str) -> bool:
        if not name or len(name) < 4 or len(name) > 200:
            return False
        if re.search(r'\d{6,}', name):
            return False
        keywords = [
            "LLC", "SAOC", "SAOG", "Ltd", "Limited", "Trading",
            "Services", "Company", "Co.", "Est.", "Group",
            "Industries", "International", "Gulf", "Oman",
            "شركة", "مؤسسة", "تجارة",  # Arabic company markers
        ]
        return (
            any(kw.lower() in name.lower() for kw in keywords)
            or (len(name.split()) >= 2 and name[0].isupper())
        )
