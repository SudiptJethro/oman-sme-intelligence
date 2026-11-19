"""
Oman Tender Board Scraper
──────────────────────────
Scrapes data from tenderboard.gov.om:

  1. Registered Vendor List  - /product/ReportAction?eventFlag=RegVendorPublic
     Requires credentials: set TENDER_BOARD_USERNAME + TENDER_BOARD_PASSWORD.
     Without credentials the endpoint redirects to a login page; we skip silently.

  2. Mandatory List of Goods/Services/Jobs (OIA codes)
     Accessible on the public dashboard via JS-executed form POST.
     Fetched via Playwright, parsed, stored in oia_protected_codes table.
     Used to cross-reference and score companies by OIA-protected segments.

  3. Awarded Contracts - published award pages + PDO/OQ press releases.
     Cross-references companies already in DB: if a company name appears in
     an award, we bump its revenue estimates and verify existence.

  4. JSRS cross-reference - for companies in DB with unknown JSRS status,
     searches via businessgateways.com public JSRS search page using
     company name / CR number.

Set TENDER_BOARD_USERNAME + TENDER_BOARD_PASSWORD to unlock vendor registry.
"""
import os, re, json, logging
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL       = "https://etendering.tenderboard.gov.om"
PUBLIC_DASH    = f"{BASE_URL}/product/publicDash?CTRL_STRDIRECTION=LTR"
VENDOR_REG_URL = f"{BASE_URL}/product/ReportAction?eventFlag=RegVendorPublic"
JSRS_SEARCH    = "https://businessgateways.com/jsrs"
JSRS_PORTAL    = "https://portal.businessgateways.com"

TARGET_BUYERS = [
    "PDO", "OQ", "OIA", "Oman LNG", "OPAL", "Ministry of Energy",
    "OMRAN", "Oman Rail", "Sohar Port", "Salalah Port", "NAMA",
    "Ministry of Commerce", "RIYADA", "Petroleum Development Oman",
]

# OIA mandatory list: Tender Board doc ID 262 (fetched via JS form POST)
MANDATORY_LIST_DOC_ID = "262"

# JSRS number pattern: JSRS followed by 6-12 alphanumeric chars
JSRS_NUMBER_RE = re.compile(r'\bJSRS[-/]?([A-Z0-9]{4,14})\b', re.I)


class TenderBoardScraper(BaseScraper):
    source_name = "tender_board"

    def __init__(self):
        super().__init__()
        self.tb_username = os.getenv("TENDER_BOARD_USERNAME", "")
        self.tb_password = os.getenv("TENDER_BOARD_PASSWORD", "")

    def scrape(self):
        # 1. OIA mandatory list (always public - no login needed)
        self._scrape_mandatory_list()

        # 2. Vendor registry (requires credentials)
        if self.tb_username and self.tb_password:
            logger.info("Tender Board: credentials found - scraping vendor registry")
            self._scrape_vendor_registry_authenticated()
        else:
            logger.info(
                "Tender Board vendor registry skipped - set TENDER_BOARD_USERNAME "
                "+ TENDER_BOARD_PASSWORD to enable"
            )

        # 3. Awarded contracts + PDO/OQ press releases
        award_data = []
        award_data.extend(self._scrape_awards_section())
        award_data.extend(self._scrape_pdo_awards())
        award_data.extend(self._scrape_oq_awards())
        logger.info(f"Tender award records: {len(award_data)}")
        self._aggregate_and_save(award_data)

        # 4. JSRS status enrichment for companies already in DB
        self._enrich_jsrs_status()

    # ── OIA Mandatory List (public, no login) ──────────────────

    def _scrape_mandatory_list(self):
        """
        Fetch the Tender Board's Mandatory List of Goods, Services and Jobs
        (DocID 262) via Playwright JS execution on the public dashboard.
        Stores codes in oia_protected_codes; updates oia_code_match on smes.
        """
        logger.info("Fetching OIA mandatory list from Tender Board public dash...")
        self._ensure_browser()
        page = None
        try:
            page = self._context.new_page()
            page.goto(PUBLIC_DASH, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)

            # Execute the same JS as the "Mandatory List" button
            page.evaluate("""() => {
                const f = document.forms[0];
                if (!f) return;
                const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
                set('flag', 'viewtenderlaw');
                set('DocTypeId', '2');
                set('DocID', '262');
                f.submit();
            }""")
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
            page.wait_for_timeout(2_000)
            html = page.content()
            if html:
                soup = BeautifulSoup(html, "lxml")
                codes_saved = self._parse_oia_codes(soup)
                logger.info(f"OIA mandatory list: {codes_saved} codes stored/updated")
                if codes_saved:
                    self._match_oia_codes_to_companies()
        except Exception as e:
            logger.warning(f"Mandatory list fetch failed: {e}")
        finally:
            if page:
                page.close()

    def _parse_oia_codes(self, soup: BeautifulSoup) -> int:
        """Parse OIA activity codes from the mandatory list page."""
        saved = 0
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                continue
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) < 2:
                    continue
                code = desc = sector = desc_ar = None
                for i, h in enumerate(headers):
                    if i >= len(cells):
                        break
                    v = cells[i]
                    if any(k in h for k in ["code", "رمز", "رقم"]):
                        code = v[:20]
                    elif any(k in h for k in ["description", "activity", "good", "service",
                                               "الوصف", "النشاط", "البند"]):
                        if desc is None:
                            desc = v[:200]
                        else:
                            desc_ar = v[:200]
                    elif any(k in h for k in ["sector", "category", "القطاع"]):
                        sector = v[:100]

                if not code and cells:
                    # First cell often is the code if it looks like one
                    if re.match(r'^[A-Z0-9]{2,10}$', cells[0].strip()):
                        code = cells[0].strip()
                        desc = cells[1][:200] if len(cells) > 1 else None

                if code and desc:
                    try:
                        self.db.execute(
                            """INSERT INTO oia_protected_codes (code, description, description_ar, sector)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(code) DO UPDATE SET
                                 description    = excluded.description,
                                 description_ar = excluded.description_ar,
                                 sector         = excluded.sector""",
                            [code, desc, desc_ar, sector],
                        )
                        saved += 1
                    except Exception as e:
                        logger.debug(f"OIA code insert error: {e}")

        # If no structured table, try to extract from raw text (fallback for PDF-style pages)
        if saved == 0:
            text = soup.get_text("\n")
            for m in re.finditer(r'\b([A-Z]{1,4}[-_]?\d{2,5})\b\s*[–\-:]\s*(.{10,100})', text):
                code, desc = m.group(1), m.group(2).strip()
                try:
                    self.db.execute(
                        """INSERT OR IGNORE INTO oia_protected_codes (code, description) VALUES (?, ?)""",
                        [code, desc[:200]],
                    )
                    saved += 1
                except Exception:
                    pass

        return saved

    def _match_oia_codes_to_companies(self):
        """
        For each company in oman_smes, check how many OIA codes match
        their sector/activity and update oia_code_match + oia_matched_codes.
        """
        try:
            codes = self.db.query("SELECT code, description, sector FROM oia_protected_codes")
            if not codes:
                return

            companies = self.db.query(
                "SELECT id, company_name, vision2040_sector, jsrs_categories "
                "FROM oman_smes WHERE oia_code_match = 0 LIMIT 5000"
            )
            for co in companies:
                search_text = " ".join(filter(None, [
                    co.get("company_name", ""),
                    co.get("vision2040_sector", ""),
                    co.get("jsrs_categories", ""),
                ])).lower()

                matched = []
                for c in codes:
                    code_text = " ".join(filter(None, [
                        c.get("description", ""),
                        c.get("sector", ""),
                    ])).lower()
                    # Match: any word ≥4 chars in code description appears in company data
                    words = [w for w in re.split(r'\W+', code_text) if len(w) >= 4]
                    if any(w in search_text for w in words):
                        matched.append(c["code"])

                if matched:
                    self.db.execute(
                        """UPDATE oman_smes SET
                             oia_code_match   = 1,
                             oia_matched_codes = ?,
                             oia_match_count  = ?
                           WHERE id = ?""",
                        [json.dumps(matched), len(matched), co["id"]],
                    )
            logger.info("OIA code matching complete")
        except Exception as e:
            logger.error(f"OIA matching error: {e}")

    # ── Registered Vendor Registry (authenticated) ─────────────

    def _scrape_vendor_registry_authenticated(self):
        """
        Scrape the registered vendor list using Tender Board credentials.
        Requires TENDER_BOARD_USERNAME + TENDER_BOARD_PASSWORD env vars.
        """
        logger.info("Tender Board: authenticating for vendor registry...")
        self._ensure_browser()
        page = None
        try:
            page = self._context.new_page()
            # Step 1: land on public dash to get valid session
            page.goto(PUBLIC_DASH, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1_500)

            # Step 2: find and submit the login form
            page.click("a[href*='login'], .login-btn, #loginBtn, a:has-text('Login')")
            page.wait_for_load_state("domcontentloaded", timeout=15_000)

            page.fill("input[name='CTRL_USERNAME'], input[type='text']", self.tb_username)
            page.fill("input[name='CTRL_PASSWORD'], input[type='password']", self.tb_password)
            page.click("input[type='submit'], button[type='submit']")
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
            page.wait_for_timeout(2_000)

            # Step 3: navigate to vendor report
            page.goto(VENDOR_REG_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)
            html = page.content()

            if "Security Page" in html or "logoutForce" in html:
                logger.warning("Tender Board vendor registry: login failed or insufficient permissions")
                return

            soup  = BeautifulSoup(html, "lxml")
            saved = self._parse_vendor_registry(soup)
            logger.info(f"Vendor registry page 1: {saved} companies")

            # Paginate
            for pg in range(2, 200):
                page.goto(f"{VENDOR_REG_URL}&page={pg}", wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(1_500)
                soup2 = BeautifulSoup(page.content(), "lxml")
                n = self._parse_vendor_registry(soup2)
                if n == 0:
                    break
                logger.info(f"Vendor registry page {pg}: {n} companies")
                self.polite_sleep(2)

        except Exception as e:
            logger.error(f"Vendor registry authenticated scrape error: {e}")
        finally:
            if page:
                page.close()

    # ── JSRS status enrichment ─────────────────────────────────

    def _enrich_jsrs_status(self):
        """
        For companies in DB with unknown JSRS status, search the
        JSRS public portal (businessgateways.com) by company name.
        Updates jsrs_cert_status, jsrs_cert_number, jsrs_certified.
        """
        try:
            unknowns = self.db.query(
                """SELECT id, company_name, commercial_reg_no
                   FROM oman_smes
                   WHERE jsrs_cert_status = 'Unknown'
                     AND (commercial_reg_no IS NOT NULL OR company_name IS NOT NULL)
                   ORDER BY RANDOM()
                   LIMIT 200"""
            )
            logger.info(f"JSRS enrichment: checking {len(unknowns)} companies")
            enriched = 0
            for co in unknowns:
                result = self._lookup_jsrs(co.get("company_name"), co.get("commercial_reg_no"))
                if result:
                    self.db.execute(
                        """UPDATE oman_smes SET
                             jsrs_cert_status = ?,
                             jsrs_certified   = ?,
                             jsrs_cert_number = ?
                           WHERE id = ?""",
                        [
                            result["status"],
                            1 if result["status"] == "Active" else 0,
                            result.get("jsrs_number"),
                            co["id"],
                        ],
                    )
                    enriched += 1
                self.polite_sleep(3)
            logger.info(f"JSRS enrichment: updated {enriched} companies")
        except Exception as e:
            logger.error(f"JSRS enrichment error: {e}")

    def _lookup_jsrs(self, company_name: str, cr_no: str | None) -> dict | None:
        """
        Check JSRS status for a single company.
        Strategy:
          1. Try JSRS portal search by CR number (most precise)
          2. Fall back to company name search on businessgateways.com JSRS page
          3. Extract status + JSRS certificate number from result
        Returns dict with keys: status, jsrs_number - or None if not found.
        """
        self._ensure_browser()
        page = None
        try:
            page = self._context.new_page()

            # Try CR-based lookup first
            if cr_no:
                search_term = cr_no
                url = f"{JSRS_PORTAL}/jsrs/public/supplier-search?cr={cr_no}"
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(2_000)
                result = self._parse_jsrs_result(page.content(), search_term)
                if result:
                    return result

            # Fall back to name search on businessgateways JSRS page
            if company_name:
                page.goto(JSRS_SEARCH, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(2_000)
                # Try to find a search input
                try:
                    page.fill(
                        "input[type='search'], input[name='q'], input[placeholder*='search' i], "
                        "input[placeholder*='company' i], input[placeholder*='supplier' i]",
                        company_name,
                    )
                    page.press(
                        "input[type='search'], input[name='q'], input[placeholder*='search' i]",
                        "Enter",
                    )
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(2_000)
                except Exception:
                    pass

                result = self._parse_jsrs_result(page.content(), company_name)
                if result:
                    return result

        except Exception as e:
            logger.debug(f"JSRS lookup error for {company_name}: {e}")
        finally:
            if page:
                page.close()
        return None

    def _parse_jsrs_result(self, html: str, search_term: str) -> dict | None:
        """
        Parse JSRS portal HTML for status + certificate number.
        Returns {"status": "Active"|"Expired", "jsrs_number": "JSRS-XXXXX"} or None.
        """
        if not html or len(html) < 200:
            return None
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)

        # Look for the searched company in the results
        name_lc = search_term.lower()
        if name_lc not in text.lower() and not re.search(r'\d{7}', text):
            return None

        status = None
        if re.search(r'\bactive\b', text, re.I):
            status = "Active"
        elif re.search(r'\bexpired\b', text, re.I):
            status = "Expired"
        elif re.search(r'\bsuspended\b', text, re.I):
            status = "Expired"

        jsrs_match = JSRS_NUMBER_RE.search(text)
        jsrs_number = f"JSRS-{jsrs_match.group(1)}" if jsrs_match else None

        if status:
            return {"status": status, "jsrs_number": jsrs_number}
        return None

    # ── Registered Vendor Registry (unauthenticated fallback - usually blocked) ──

    def _scrape_vendor_registry(self):
        """Unauthenticated attempt - kept for reference. Use _scrape_vendor_registry_authenticated instead."""
        logger.info("Attempting unauthenticated vendor registry access...")
        html = self.cf_browser_get(VENDOR_REG_URL, wait_for="table, .vendor-list, #reportTable", timeout=45_000)
        if not html or "Security Page" in html or "logoutForce" in html:
            logger.warning("Vendor registry blocked - needs credentials (TENDER_BOARD_USERNAME/PASSWORD)")
            return

        soup  = BeautifulSoup(html, "lxml")
        saved = self._parse_vendor_registry(soup)
        logger.info(f"Vendor registry: {saved} companies from page 1")

        for pg in range(2, 50):
            paged = self.cf_browser_get(f"{VENDOR_REG_URL}&page={pg}", wait_for="table", timeout=30_000)
            if not paged:
                break
            psoup = BeautifulSoup(paged, "lxml")
            n = self._parse_vendor_registry(psoup)
            if n == 0:
                break
            logger.info(f"Vendor registry page {pg}: {n} companies")
            self.polite_sleep(2)

    def _parse_vendor_registry(self, soup: BeautifulSoup) -> int:
        saved = 0
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                continue
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells:
                    continue
                company = self._extract_vendor_from_row(cells, headers)
                if company and self.save_company(company):
                    saved += 1
        return saved

    def _extract_vendor_from_row(self, cells: list, headers: list) -> dict | None:
        record: dict = {"source": self.source_name}
        for i, h in enumerate(headers):
            if i >= len(cells):
                break
            val = cells[i].strip()
            if not val:
                continue
            if any(k in h for k in ["company", "supplier", "vendor", "name", "establishment"]):
                record["company_name"] = val[:200]
            elif any(k in h for k in ["cr no", "commercial", "reg no", "registration"]):
                record["commercial_reg_no"] = val
            elif any(k in h for k in ["category", "class", "activity", "sector", "type"]):
                existing = record.get("jsrs_categories") or []
                if val and val not in existing:
                    existing.append(val)
                record["jsrs_categories"] = existing
            elif any(k in h for k in ["city", "governorate", "location"]):
                record["city"] = val
            elif any(k in h for k in ["phone", "tel", "mobile"]):
                record["contact_phone"] = val
            elif "email" in h:
                record["contact_email"] = val
            elif any(k in h for k in ["status", "approval"]):
                if "active" in val.lower() or "approved" in val.lower():
                    record["jsrs_cert_status"] = "Active"
        return record if record.get("company_name") else None

    # ── Tender Board main portal ───────────────────────────────

    def _scrape_awards_section(self) -> list[dict]:
        results = []
        endpoints = [
            f"{BASE_URL}/product/publicDash?CTRL_STRDIRECTION=LTR",
            f"{BASE_URL}/contract/awardedContracts",
            f"{BASE_URL}/contract/publicAwardList",
        ]
        for url in endpoints:
            html = self.browser_get(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            results.extend(self._parse_tender_table(soup, buyer="Tender Board"))
            self.polite_sleep(3)

            for page in range(2, 15):
                paged_html = self.browser_get(f"{url}?page={page}")
                if not paged_html:
                    break
                soup = BeautifulSoup(paged_html, "lxml")
                rows = self._parse_tender_table(soup, buyer="Tender Board")
                if not rows:
                    break
                results.extend(rows)
                self.polite_sleep(2)

        return results

    def _parse_tender_table(self, soup: BeautifulSoup, buyer: str) -> list[dict]:
        records = []
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if not cells:
                    continue
                record = self._extract_from_cells(cells, headers, buyer)
                if record:
                    records.append(record)

        for card in soup.select(".tender-item, .award-card, .result-item, article.tender"):
            record = self._extract_from_card(card, buyer)
            if record:
                records.append(record)

        return records

    def _extract_from_cells(self, cells: list, headers: list, buyer: str) -> dict | None:
        if len(cells) < 3:
            return None
        record = {"buyer_entity": buyer, "source": self.source_name}

        for i, h in enumerate(headers):
            if i >= len(cells):
                break
            val = cells[i].strip()
            if any(k in h for k in ["company", "supplier", "contractor", "winner", "awarded to"]):
                record["company_name"] = val
            elif any(k in h for k in ["value", "amount", "omr", "price"]):
                record["award_value_omr"] = self._parse_omr(val)
            elif any(k in h for k in ["date", "award", "issued"]):
                record["award_date"] = self._parse_date(val)
            elif any(k in h for k in ["tender", "ref", "no", "number"]):
                record["tender_ref"] = val
            elif any(k in h for k in ["title", "description", "subject"]):
                record["tender_title"] = val
            elif any(k in h for k in ["sector", "category", "type"]):
                record["sector"] = val

        return record if record.get("company_name") else None

    def _extract_from_card(self, card, buyer: str) -> dict | None:
        text    = card.get_text(separator=" ", strip=True)
        company = None
        value   = None

        company_match = re.search(
            r'(?:Awarded to|Winner|Supplier)[:\s]+([A-Z][^,\n]+(?:LLC|SAOC|SAOG|Co\.?|Ltd\.?)?)',
            text
        )
        if company_match:
            company = company_match.group(1).strip()

        value_match = re.search(r'OMR\s*([\d,]+(?:\.\d+)?)', text, re.I)
        if value_match:
            value = self._parse_omr(value_match.group(1))

        if company:
            return {
                "company_name":   company,
                "award_value_omr": value,
                "buyer_entity":   buyer,
                "source":         self.source_name,
                "tender_title":   text[:200],
            }
        return None

    # ── PDO supplier awards ────────────────────────────────────

    def _scrape_pdo_awards(self) -> list[dict]:
        results  = []
        pdo_urls = [
            "https://www.pdo.co.om/en/news/press-releases",  # contract awards announced via press releases
            "https://srm.pdo.co.om/",                        # SAP SRM supplier portal
        ]
        for url in pdo_urls:
            html = self.browser_get(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            results.extend(self._parse_tender_table(soup, buyer="PDO"))
            self.polite_sleep(4)
        return results

    # ── OQ awards ─────────────────────────────────────────────

    def _scrape_oq_awards(self) -> list[dict]:
        results = []
        oq_urls = [
            "https://oq.com/en/vendors",
            "https://oq.com/en/media/news",  # contract awards in news/press releases
        ]
        for url in oq_urls:
            html = self.browser_get(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            results.extend(self._parse_tender_table(soup, buyer="OQ"))
            self.polite_sleep(4)
        return results

    # ── Aggregation & save ─────────────────────────────────────

    def _cross_reference_with_db(self, company_name: str, award_value: float | None, buyer: str):
        """
        If company_name matches an existing SME in DB, update its
        tender_award_total and bump revenue_confidence.
        Returns True if matched.
        """
        try:
            # Fuzzy name match: try exact, then strip suffixes
            clean = re.sub(r'\b(LLC|SAOC|SAOG|Ltd|Co\.|Est\.)\b', '', company_name, flags=re.I).strip()
            rows = self.db.query(
                """SELECT id, tender_award_total_omr, tender_contract_count
                   FROM oman_smes
                   WHERE company_name = ?
                      OR company_name LIKE ?
                   LIMIT 1""",
                [company_name, f"%{clean}%"],
            )
            if not rows:
                return False
            row = rows[0]
            new_total = (row.get("tender_award_total_omr") or 0) + (award_value or 0)
            new_count = (row.get("tender_contract_count") or 0) + 1
            confidence = "High" if new_count >= 5 else ("Medium" if new_count >= 2 else "Low")
            self.db.execute(
                """UPDATE oman_smes SET
                     tender_award_total_omr = ?,
                     tender_contract_count  = ?,
                     revenue_confidence     = ?
                   WHERE id = ?""",
                [round(new_total, 2), new_count, confidence, row["id"]],
            )
            return True
        except Exception as e:
            logger.debug(f"Cross-ref error for {company_name}: {e}")
            return False

    def _aggregate_and_save(self, records: list[dict]):
        from collections import defaultdict
        company_totals: dict[str, dict] = defaultdict(lambda: {
            "award_total": 0.0, "count": 0, "sectors": set(),
            "buyers": set(), "latest_record": {}
        })

        for r in records:
            name = r.get("company_name", "").strip()
            if not name or len(name) < 3:
                continue

            if r.get("award_value_omr"):
                self.save_tender(r)

            # Cross-reference: update existing SME record if matched
            self._cross_reference_with_db(name, r.get("award_value_omr"), r.get("buyer_entity", ""))

            agg = company_totals[name]
            agg["award_total"] += r.get("award_value_omr") or 0
            agg["count"]       += 1
            if r.get("sector"):
                agg["sectors"].add(r["sector"])
            if r.get("buyer_entity"):
                agg["buyers"].add(r["buyer_entity"])
            agg["latest_record"] = r

        for name, agg in company_totals.items():
            band       = self._estimate_revenue_band(agg["award_total"], agg["count"])
            confidence = "High" if agg["count"] >= 5 else ("Medium" if agg["count"] >= 2 else "Low")

            self.save_company({
                "company_name":           name,
                "tender_award_total_omr": round(agg["award_total"], 2),
                "tender_contract_count":  agg["count"],
                "estimated_revenue_band": band,
                "revenue_confidence":     confidence,
                "vision2040_sector":      list(agg["sectors"])[0] if agg["sectors"] else None,
                "source":                 self.source_name,
            })

    def _estimate_revenue_band(self, total_omr: float, count: int) -> str:
        if total_omr <= 0:
            return "Unknown"
        estimated = total_omr * 2.5
        if estimated < 1_000_000:
            return "Under 1M"
        elif estimated <= 5_000_000:
            return "1M–5M"
        elif estimated <= 20_000_000:
            return "5M–20M"
        else:
            return "Over 20M"

    def _parse_omr(self, val: str) -> float | None:
        try:
            cleaned = re.sub(r"[^\d.]", "", val.replace(",", ""))
            return float(cleaned) if cleaned else None
        except Exception:
            return None

    def _parse_date(self, val: str) -> str | None:
        for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"]:
            try:
                from datetime import datetime
                return datetime.strptime(val.strip(), fmt).date().isoformat()
            except Exception:
                pass
        return None
