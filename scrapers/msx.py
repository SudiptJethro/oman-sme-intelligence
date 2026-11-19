"""
Muscat Stock Exchange (MSX) Scraper
─────────────────────────────────────
Scrapes listed companies from msx.om, targeting the Growth Market (third market)
and SME-scale companies in Vision 2040 sectors. Public financials make revenue
estimation more accurate than proxy signals alone.

Sources:
  - msx.om/en/market/equities  - full listed company directory
  - msx.om/en/issuers          - issuer profiles (sector, CR, financials)
"""
import re, logging
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

MSX_BASE = "https://www.msx.om"

# Known MSX-listed companies in target sectors (ticker → (name, sector))
# Source: MSX listed companies as of 2024. Profiles not web-accessible, so hardcoded.
MSX_KNOWN_COMPANIES: dict[str, tuple[str, str]] = {
    "ATMI": ("Al Taher Engineering & Trading LLC",     "Manufacturing"),
    "NLNG": ("National Gas Company SAOG",               "Energy"),
    "OETI": ("Oman Education & Training Investment SAOG", "Professional Services"),
    "OFCI": ("Oman Flour Mills Company SAOG",           "Food Manufacturing"),
    "SAHS": ("Salalah Mills Company SAOG",              "Food Manufacturing"),
    "SHPS": ("Shippers Energy Services SAOG",           "Logistics"),
    "DIDI": ("Dhofar International Development SAOG",   "Manufacturing"),
    "NRED": ("National Real Estate Development SAOG",   "Construction"),
    "BACS": ("Barka Desalination Company SAOG",         "Energy"),
    "ABOB": ("Alizz Islamic Bank SAOG",                 None),  # skip - financial
    "AHLI": ("Ahli Bank SAOG",                          None),  # skip - financial
    "LIVA": ("Liva Group SAOG",                         "Manufacturing"),
    "OCOI": ("Oman Chromite Company SAOG",              "Manufacturing"),
    "OIIL": ("Oman International Insurance SAOG",       None),  # skip
    "MAFI": ("Al Maha Financial Services SAOG",         None),  # skip
    "NWDI": ("National Detergent Company SAOG",         "Manufacturing"),
    "SOGS": ("Sohar International SAOG",                None),  # skip - bank
    "BKDB": ("Bank Dhofar SAOG",                        None),  # skip - bank
    "HBMO": ("HSBC Bank Oman SAOG",                     None),  # skip - bank
    "AUAB": ("Ubhar Capital SAOG",                      None),  # skip
    "OMIC": ("Oman Medical Industries SAOG",            "Healthcare"),
    "ORFC": ("Oman Refreshment Company SAOG",           "Food Manufacturing"),
    "OICC": ("Oman Investment & Finance Company SAOG",  None),  # skip
    "POWE": ("Sembcorp Salalah Power & Water SAOG",     "Energy"),
    "HSLS": ("Hadbin Salim LLC",                        "Manufacturing"),
    "MHIN": ("Mujtama Housing & Infrastructure SAOG",   "Construction"),
    "OMAN": ("Oman Cables Industry SAOG",               "Manufacturing"),
    "OCAG": ("Oman Cement Company SAOG",                "Construction"),
    "OPAZ": ("Oman Phosphate Company SAOG",             "Manufacturing"),
    "MCES": ("Muscat City Desalination SAOG",           "Energy"),
    "OGFN": ("Oman Food Investment Holdings SAOG",      "Food Manufacturing"),
    "INMA": ("Inma Holding SAOG",                       "Professional Services"),
    "OMCO": ("Gulf Mushroom Company SAOG",              "Food Manufacturing"),
    "ACCD": ("Al Madina Investment SAOG",               None),  # skip
    "APEI": ("Al-Anwar Holding SAOG",                   "Manufacturing"),
    "AMIC": ("Oman & Emirates Investment Holdings SAOG",None),  # skip
}

# Vision 2040-aligned sectors to target (skip banks, insurance, real estate)
TARGET_SECTORS = {
    "Industrial":            ("Diversified Economy", "Manufacturing"),
    "Services":              ("Knowledge Economy",   "Professional Services"),
    "Telecommunications":    ("Knowledge Economy",   "ICT"),
    "Logistics":             ("Diversified Economy", "Logistics"),
    "Agriculture":           ("Food Security",       "Agri-Food"),
    "Healthcare":            ("Human Capital",       "Healthcare"),
    "Energy":                ("Energy Transition",   "Energy"),
    "Technology":            ("Knowledge Economy",   "ICT"),
    "Construction":          ("Diversified Economy", "Construction"),
    "Food & Beverages":      ("Food Security",       "Food Manufacturing"),
    "Transport":             ("Diversified Economy", "Logistics"),
    "Education":             ("Human Capital",       "Professional Services"),
    "Media":                 ("Knowledge Economy",   "Professional Services"),
    "Tourism":               ("Diversified Economy", "Professional Services"),
}

# Skip these - large caps, banks, financial holding
SKIP_SECTORS = {
    "Banking", "Insurance", "Financial Services", "Investment",
    "Real Estate", "Utilities",
}

# Revenue band from market cap proxy (OMR)
def _cap_to_revenue_band(market_cap_omr: float) -> tuple[str, str]:
    if market_cap_omr <= 0:
        return "Unknown", "Low"
    # Listed SMEs: market cap ~1–3x revenue typically
    estimated = market_cap_omr / 2.0
    if estimated < 1_000_000:
        return "Under 1M", "Medium"
    elif estimated <= 5_000_000:
        return "1M–5M", "Medium"
    elif estimated <= 20_000_000:
        return "5M–20M", "Medium"
    else:
        return "Over 20M", "High"


class MSXScraper(BaseScraper):
    source_name = "msx"

    def scrape(self):
        companies = []
        # Primary: extract live tickers from homepage financial report links
        companies.extend(self._scrape_homepage_tickers())
        # Supplement with known company list for any tickers not found live
        companies.extend(self._seed_known_companies(
            seen={c["_ticker"] for c in companies if c.get("_ticker")}
        ))

        logger.info(f"MSX: collected {len(companies)} candidates")
        saved = 0
        for c in companies:
            c.pop("_ticker", None)
            if self.save_company(c):
                saved += 1
        logger.info(f"MSX: {saved} new companies saved")

    # ── Extract live tickers from MSX homepage ─────────────────

    def _scrape_homepage_tickers(self) -> list[dict]:
        html = self.browser_get(MSX_BASE, timeout=30_000)
        if not html:
            return []

        import re
        # Financial report filenames embed tickers: /MSMDOCS/FinancialReports/TICKER_YEAR_...
        tickers = re.findall(
            r'/MSMDOCS/FinancialReports/([A-Z]{3,5})_\d{4}_',
            html
        )
        unique_tickers = list(dict.fromkeys(tickers))  # preserve order, dedupe
        logger.info(f"MSX homepage: found {len(unique_tickers)} live tickers: {unique_tickers}")

        results = []
        for ticker in unique_tickers:
            if ticker in MSX_KNOWN_COMPANIES:
                name, sector = MSX_KNOWN_COMPANIES[ticker]
                if sector is None:
                    continue
                c = self._build_record(ticker, name, sector)
            else:
                # Unknown ticker - create minimal record, let AI enrich later
                c = self._build_record(ticker, f"{ticker} (MSX Listed)", "")
            results.append(c)
        return results

    # ── Seed known companies not found in live tickers ─────────

    def _seed_known_companies(self, seen: set) -> list[dict]:
        results = []
        for ticker, (name, sector) in MSX_KNOWN_COMPANIES.items():
            if ticker in seen or sector is None:
                continue
            results.append(self._build_record(ticker, name, sector))
        return results

    def _build_record(self, ticker: str, name: str, sector: str) -> dict:
        pillar = TARGET_SECTORS.get(sector, ("", ""))[0] if sector else ""
        return {
            "_ticker":                ticker,
            "company_name":           name,
            "city":                   "Muscat",
            "vision2040_pillar":      pillar,
            "vision2040_sector":      sector,
            "estimated_revenue_band": "Unknown",
            "revenue_confidence":     "Low",
            "source":                 self.source_name,
            "analyst_notes":          f"MSX listed. Ticker: {ticker}",
        }

    # ── Parse table / card layouts ─────────────────────────────

    def _parse_company_table(self, soup: BeautifulSoup) -> list[dict]:
        results = []

        # Table layout
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(h in " ".join(headers) for h in ["company", "issuer", "name", "symbol"]):
                continue
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                c = self._parse_row(cells, headers)
                if c:
                    results.append(c)

        # Card / list layout
        if not results:
            for card in soup.select(
                ".company-card, .issuer-card, .listed-company, "
                ".equity-item, article.company, .stock-item"
            ):
                c = self._parse_card(card)
                if c:
                    results.append(c)

        return results

    def _parse_row(self, cells: list, headers: list) -> dict | None:
        if len(cells) < 2:
            return None

        record: dict = {"source": self.source_name, "jsrs_certified": 0}

        for i, h in enumerate(headers):
            if i >= len(cells):
                break
            val = cells[i].strip()
            if not val:
                continue
            if any(k in h for k in ["company", "issuer", "name"]):
                record["company_name"] = val[:200]
            elif any(k in h for k in ["sector", "industry", "category"]):
                record["_sector_raw"] = val
            elif any(k in h for k in ["market cap", "capitalisation", "cap"]):
                record["_market_cap_raw"] = val
            elif any(k in h for k in ["symbol", "ticker", "code"]):
                record["_ticker"] = val
            elif any(k in h for k in ["cr", "commercial", "reg"]):
                record["commercial_reg_no"] = val

        if not record.get("company_name"):
            return None

        return self._enrich_record(record)

    def _parse_card(self, card) -> dict | None:
        name_el = card.select_one(
            "h2, h3, h4, .company-name, .issuer-name, [class*='name'], [class*='title']"
        )
        if not name_el:
            return None
        name = name_el.get_text(strip=True)
        if not name or len(name) < 3:
            return None

        text = card.get_text(separator=" ")
        sector_el = card.select_one(".sector, .industry, [class*='sector']")

        record: dict = {
            "company_name": name[:200],
            "source": self.source_name,
            "jsrs_certified": 0,
            "_sector_raw": sector_el.get_text(strip=True) if sector_el else "",
        }

        cap_match = re.search(r'([\d,]+(?:\.\d+)?)\s*(?:OMR|omr|M\b|K\b)', text)
        if cap_match:
            record["_market_cap_raw"] = cap_match.group(0)

        link = card.find("a", href=True)
        if link:
            href = link["href"]
            if not href.startswith("http"):
                href = MSX_BASE + href
            record["website"] = href

        return self._enrich_record(record)

    def _enrich_record(self, record: dict) -> dict | None:
        sector_raw = record.pop("_sector_raw", "") or ""
        cap_raw    = record.pop("_market_cap_raw", "") or ""
        ticker     = record.pop("_ticker", None)

        # Skip non-target sectors
        for skip in SKIP_SECTORS:
            if skip.lower() in sector_raw.lower():
                return None

        # Map sector → Vision 2040
        pillar, sector = "", ""
        for s, (p, sec) in TARGET_SECTORS.items():
            if s.lower() in sector_raw.lower():
                pillar, sector = p, sec
                break
        if not sector and sector_raw:
            sector = sector_raw[:60]

        # Parse market cap → revenue band
        market_cap = self._parse_omr(cap_raw)
        band, confidence = _cap_to_revenue_band(market_cap or 0)

        # MSX companies have public financials = higher confidence baseline
        if confidence == "Low" and market_cap:
            confidence = "Medium"

        record.update({
            "city":                   "Muscat",
            "vision2040_pillar":      pillar,
            "vision2040_sector":      sector,
            "estimated_revenue_band": band,
            "revenue_confidence":     confidence,
            "source":                 self.source_name,
        })
        if ticker:
            record.setdefault("analyst_notes", f"MSX ticker: {ticker}")

        return record

    def _parse_omr(self, val: str) -> float | None:
        if not val:
            return None
        try:
            multiplier = 1.0
            if "M" in val or "m" in val:
                multiplier = 1_000_000
            elif "K" in val or "k" in val:
                multiplier = 1_000
            cleaned = re.sub(r"[^\d.]", "", val)
            return float(cleaned) * multiplier if cleaned else None
        except Exception:
            return None
