"""
Tender Board - Awarded Tenders Scraper
──────────────────────────────────────
Scrapes the public awarded-tender report from:
  https://etendering.tenderboard.gov.om/product/ReportAction

Strategy:
  1. POST probe - try common eventFlag values used by the public report
     interface (SearchAwardPublic, AwardedTendersPublic, etc.) with year
     filters covering 2019 → current year.
  2. XHR discovery - load the public dashboard via Playwright, intercept all
     network requests to find the AJAX endpoint that populates the award stats
     and charts. Replay that endpoint with year filters.
  3. Playwright form-eval fallback - submit the awards form via JS evaluation,
     logging page URL + HTML snippet to diagnose what's returned.
  4. Parse each results table: company name, CR/PTLC, tender ref, award value
     (OMR), award date, buyer entity, sector.
  5. Insert rows into tender_awards; cross-reference oman_smes to update
     tender_award_total_omr, tender_contract_count, and revenue_confidence.

The portal shows 28,350 awarded tenders on the public dashboard - the stats
are public; we use network interception to find the real endpoint.
"""
import re
import json
import logging
from datetime import datetime

from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL    = "https://etendering.tenderboard.gov.om"
REPORT_URL  = f"{BASE_URL}/product/ReportAction"
PUBLIC_DASH = f"{BASE_URL}/product/publicDash?CTRL_STRDIRECTION=LTR"

# eventFlag values to probe - in priority order (expanded list)
AWARD_EVENT_FLAGS = [
    "SearchAwardPublic",
    "AwardedTendersPublic",
    "SearchContractPublic",
    "awardReport",
    "publicAwardSearch",
    "RegAwardPublic",
    "getAwardedContracts",
    "PublicAwardReport",
    "AwardedContractPublicReport",
    "SearchPubAwardedContract",
    "viewAwardPublic",
    "listAwardPublic",
]

START_YEAR   = 2019
CURRENT_YEAR = datetime.now().year


class TenderAwardsScraper(BaseScraper):
    source_name = "tender_awards"

    def scrape(self):
        logger.info("TenderAwards: probing POST endpoints for awarded tenders")

        working_flag = self._probe_post_flags()

        if working_flag:
            logger.info(f"TenderAwards: POST working with eventFlag={working_flag}")
            self._scrape_via_post(working_flag)
            logger.info(f"TenderAwards: done - found={self.found} new={self.new}")
            return

        logger.info("TenderAwards: POST probes failed - intercepting XHR to find real endpoint")
        xhr_endpoint = self._discover_xhr_endpoint()

        if xhr_endpoint:
            logger.info(f"TenderAwards: XHR endpoint discovered: {xhr_endpoint}")
            self._scrape_via_xhr(xhr_endpoint)
        else:
            logger.info("TenderAwards: XHR discovery failed - trying Playwright form-eval with debug")
            self._scrape_via_playwright_debug()

        logger.info(f"TenderAwards: done - found={self.found} new={self.new}")

    # ── POST probe ─────────────────────────────────────────────

    def _probe_post_flags(self) -> str | None:
        """Try each candidate eventFlag; return the first that yields table rows."""
        for flag in AWARD_EVENT_FLAGS:
            html = self._post_awards(flag, page_no=1, year=CURRENT_YEAR)
            if html and self._has_award_rows(html):
                return flag
            # also try without year filter
            html = self._post_awards(flag, page_no=1, year=None)
            if html and self._has_award_rows(html):
                return flag
        return None

    def _post_awards(self, event_flag: str, page_no: int,
                     year: int | None) -> str | None:
        payload = {
            "eventFlag":         event_flag,
            "pageNo":            str(page_no),
            "awardYear":         str(year) if year else "-1",
            "year":              str(year) if year else "-1",
            "buyerEntity":       "-1",
            "sector":            "-1",
            "companyName":       "",
            "tenderRef":         "",
            "PublicUrl":         "1",
            "CTRL_STRDIRECTION": "LTR",
            "CTRL_USERID":       "null",
            "CTRL_ROLEID":       "",
            "CTRL_SID":          "null",
        }
        headers = {
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
            "Referer":      f"{REPORT_URL}?eventFlag=AwardedTendersPublic",
            "Origin":       BASE_URL,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = self.session.post(
                REPORT_URL,
                data=payload,
                headers=headers,
                timeout=30,
                allow_redirects=True,
            )
            if resp.ok and len(resp.text) > 1000 and "logoutForce" not in resp.url:
                return resp.text
        except Exception as e:
            logger.debug(f"TenderAwards POST {event_flag}: {e}")
        return None

    def _has_award_rows(self, html: str) -> bool:
        soup = BeautifulSoup(html, "lxml")
        for row in soup.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) >= 4 and cells[0].isdigit():
                return True
        return False

    # ── POST-based full scrape (year × page) ──────────────────

    def _scrape_via_post(self, event_flag: str):
        for year in range(START_YEAR, CURRENT_YEAR + 1):
            logger.info(f"TenderAwards: year {year}")
            saved = 0
            for page in range(1, 1000):
                html = self._post_awards(event_flag, page_no=page, year=year)
                if not html:
                    break
                rows = self._parse_award_rows(html, year=year)
                if not rows:
                    break
                for r in rows:
                    self._save_award(r)
                    saved += 1
                max_page = self._extract_max_page(html)
                if max_page and page >= max_page:
                    break
                self.polite_sleep(1.5)
            logger.info(f"TenderAwards: year {year} → {saved} awards saved")

    # ── XHR endpoint discovery ─────────────────────────────────

    def _discover_xhr_endpoint(self) -> dict | None:
        """
        Load the public dashboard in Playwright, intercept all network requests,
        then interact with the page (click 'Awarded' stat, wait for chart load)
        to surface the real AJAX endpoint + payload used to fetch award data.
        Returns dict with keys: url, method, post_data - or None.
        """
        self._ensure_browser()
        page = None
        captured = []
        try:
            page = self._context.new_page()

            # Intercept all requests from the page
            def on_request(req):
                url = req.url
                # Look for anything that isn't a static asset
                if any(skip in url for skip in [".css", ".js", ".png", ".svg", ".jpg", ".ico", ".woff"]):
                    return
                captured.append({
                    "url":       url,
                    "method":    req.method,
                    "post_data": req.post_data or "",
                })

            page.on("request", on_request)

            page.goto(PUBLIC_DASH, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(3_000)

            logger.info(f"TenderAwards XHR: {len(captured)} requests captured on dashboard load")
            for r in captured:
                logger.info(f"  {r['method']} {r['url'][:120]} | body={r['post_data'][:80]}")

            # Try clicking the "Awarded" stat box / chart
            for sel in [
                "text=Awarded", "[data-stat='awarded']", ".awarded", "#awarded",
                "text=28,350", ".stat-awarded", ".kpi-awarded",
            ]:
                try:
                    if page.query_selector(sel):
                        before = len(captured)
                        page.click(sel, timeout=3_000)
                        page.wait_for_timeout(2_000)
                        new = captured[before:]
                        if new:
                            logger.info(f"TenderAwards XHR after click '{sel}': {new}")
                        break
                except Exception:
                    pass

            # Find the most promising captured request
            for r in reversed(captured):
                u = r["url"].lower()
                if any(k in u for k in ["award", "contract", "report", "stat"]):
                    logger.info(f"TenderAwards: promising endpoint → {r}")
                    return r
                if r["method"] == "POST" and "product" in u:
                    logger.info(f"TenderAwards: POST endpoint → {r}")
                    return r

            # Log all captured for diagnosis
            logger.info(f"TenderAwards XHR: no award endpoint found. All captured URLs:")
            for r in captured:
                logger.info(f"  {r['method']} {r['url']}")

        except Exception as e:
            logger.error(f"TenderAwards XHR discovery error: {e}")
        finally:
            if page:
                page.close()
        return None

    def _scrape_via_xhr(self, endpoint: dict):
        """Replay discovered XHR endpoint with year filters."""
        url     = endpoint["url"]
        method  = endpoint.get("method", "POST")
        base_pd = endpoint.get("post_data", "")

        for year in range(START_YEAR, CURRENT_YEAR + 1):
            logger.info(f"TenderAwards [XHR]: year {year}")
            saved = 0
            for page_no in range(1, 1000):
                try:
                    pd = base_pd
                    for k, v in [("awardYear", str(year)), ("year", str(year)),
                                 ("pageNo", str(page_no)), ("PublicUrl", "1"),
                                 ("CTRL_USERID", "null")]:
                        pd = re.sub(rf'{k}=[^&]*', f'{k}={v}', pd)
                        if k not in pd:
                            pd += f"&{k}={v}"

                    headers = {
                        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                        "Referer":      PUBLIC_DASH,
                        "Origin":       BASE_URL,
                        "Content-Type": "application/x-www-form-urlencoded",
                    }
                    resp = self.session.post(url, data=pd, headers=headers, timeout=30)
                    if not resp.ok or len(resp.text) < 500:
                        break
                    rows = self._parse_award_rows(resp.text, year=year)
                    if not rows:
                        break
                    for r in rows:
                        self._save_award(r)
                        saved += 1
                    max_page = self._extract_max_page(resp.text)
                    if max_page and page_no >= max_page:
                        break
                    self.polite_sleep(1.5)
                except Exception as e:
                    logger.debug(f"TenderAwards XHR page error: {e}")
                    break
            logger.info(f"TenderAwards [XHR]: year {year} → {saved} awards")

    # ── Playwright debug fallback ──────────────────────────────

    def _scrape_via_playwright_debug(self):
        """
        Playwright form-eval with full debug logging:
        logs URL, response status, and first 800 chars of HTML after each
        form submission so we can diagnose what the server actually returns.
        """
        self._ensure_browser()
        page = None
        try:
            page = self._context.new_page()
            page.goto(PUBLIC_DASH, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)
            logger.info(f"TenderAwards Debug: landed on {page.url}")
            html_snap = page.content()
            logger.info(f"TenderAwards Debug: page HTML snippet = {html_snap[:500]}")

            for flag in AWARD_EVENT_FLAGS:
                logger.info(f"TenderAwards Debug: trying flag={flag}")
                try:
                    page.evaluate(f"""() => {{
                        const f = document.forms[0];
                        if (!f) return;
                        const set = (n, v) => {{
                            let el = document.querySelector('[name="' + n + '"]');
                            if (!el) {{ el = document.createElement('input'); el.type='hidden'; el.name = n; f.appendChild(el); }}
                            el.value = v;
                        }};
                        set('eventFlag', '{flag}');
                        set('awardYear', '2024');
                        set('year', '2024');
                        set('pageNo', '1');
                        set('PublicUrl', '1');
                        set('CTRL_USERID', 'null');
                        set('CTRL_STRDIRECTION', 'LTR');
                        f.submit();
                    }}""")
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    page.wait_for_timeout(2_000)
                    result_url  = page.url
                    result_html = page.content()
                    logger.info(f"TenderAwards Debug flag={flag}: url={result_url}")
                    logger.info(f"TenderAwards Debug flag={flag}: html[0:600]={result_html[:600]}")

                    if "logoutForce" in result_url:
                        logger.info(f"  → redirected to logoutForce, auth required")
                        # reload dash for next attempt
                        page.goto(PUBLIC_DASH, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(2_000)
                        continue

                    rows = self._parse_award_rows(result_html, year=2024)
                    if rows:
                        logger.info(f"  → GOT {len(rows)} rows! Working flag: {flag}")
                        for r in rows:
                            self._save_award(r)
                        # Scrape remaining years
                        for year in range(START_YEAR, CURRENT_YEAR + 1):
                            self._playwright_paginate(page, flag, year)
                        return
                    else:
                        logger.info(f"  → 0 rows, moving to next flag")
                        page.goto(PUBLIC_DASH, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(1_500)
                except Exception as e:
                    logger.info(f"  → exception: {e}")
                    try:
                        page.goto(PUBLIC_DASH, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(1_500)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"TenderAwards Playwright debug error: {e}")
        finally:
            if page:
                page.close()

    def _playwright_paginate(self, page, flag: str, year: int):
        saved = 0
        for pg in range(1, 1000):
            try:
                page.evaluate(f"""() => {{
                    const f = document.forms[0];
                    if (!f) return;
                    const set = (n, v) => {{
                        let el = document.querySelector('[name="' + n + '"]');
                        if (!el) {{ el = document.createElement('input'); el.type='hidden'; el.name=n; f.appendChild(el); }}
                        el.value = v;
                    }};
                    set('eventFlag', '{flag}');
                    set('awardYear', '{year}');
                    set('year', '{year}');
                    set('pageNo', '{pg}');
                    set('PublicUrl', '1');
                    set('CTRL_USERID', 'null');
                    f.submit();
                }}""")
                page.wait_for_load_state("domcontentloaded", timeout=20_000)
                page.wait_for_timeout(1_500)
                html  = page.content()
                rows  = self._parse_award_rows(html, year=year)
                if not rows:
                    break
                for r in rows:
                    self._save_award(r)
                    saved += 1
                max_page = self._extract_max_page(html)
                if max_page and pg >= max_page:
                    break
                self.polite_sleep(1.0)
            except Exception as e:
                logger.debug(f"TenderAwards paginate year={year} pg={pg}: {e}")
                break
        logger.info(f"TenderAwards [Playwright] year {year} → {saved} awards")

    # ── HTML parsing ───────────────────────────────────────────

    def _parse_award_rows(self, html: str, year: int | None = None) -> list[dict]:
        soup  = BeautifulSoup(html, "lxml")
        awards = []

        for row in soup.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells or not cells[0].isdigit():
                continue
            award = self._parse_row(cells, year=year)
            if award:
                awards.append(award)

        return awards

    def _parse_row(self, cells: list[str], year: int | None) -> dict | None:
        """
        Column order varies - we sniff by content rather than fixed index.
        Typical layout:
          S.No | Tender Ref | Title | Company Name | CR/PTLC | Value OMR | Date | Buyer/Category
        """
        if len(cells) < 4:
            return None

        award = {"source": self.source_name}

        # S.No in cells[0] - skip
        for i, cell in enumerate(cells[1:], start=1):
            # Tender reference: alphanumeric with dashes, 6-20 chars
            if not award.get("tender_ref") and re.match(r'^[A-Z0-9/\-]{4,25}$', cell):
                award["tender_ref"] = cell

            # CR / PTLC: pattern like "00009556-1392582" or pure digits 6-10
            elif not award.get("commercial_reg") and re.match(r'^\d{5,}-\d+$', cell):
                parts = cell.split("-")
                award["commercial_reg"] = parts[-1].lstrip("0") or parts[-1]

            # Award value: digits with optional commas/decimal, possibly prefixed OMR
            elif not award.get("award_value_omr"):
                val = self._parse_omr(cell)
                if val and val > 0:
                    award["award_value_omr"] = val

            # Date: DD-MM-YYYY or YYYY-MM-DD or DD/MM/YYYY
            elif not award.get("award_date"):
                d = self._parse_date(cell)
                if d:
                    award["award_date"] = d

            # Company name: longest text cell that looks like a company
            elif not award.get("company_name") and len(cell) >= 4:
                if re.search(r'\b(LLC|SAOC|SAOG|Co\.|Ltd|Est\.|W\.L\.L|Trading|Services|Contracting|Engineering)\b', cell, re.I):
                    award["company_name"] = cell[:250]
                elif len(cell) > 10 and not cell.isdigit():
                    award.setdefault("_candidate_name", cell)

        # Use candidate name if no company found via suffix match
        if not award.get("company_name") and award.get("_candidate_name"):
            award["company_name"] = award.pop("_candidate_name")[:250]
        award.pop("_candidate_name", None)

        # Fall back: company is usually cells[3] or cells[4]
        if not award.get("company_name"):
            for idx in [3, 4, 2]:
                if idx < len(cells) and len(cells[idx]) > 3 and not cells[idx].isdigit():
                    award["company_name"] = cells[idx][:250]
                    break

        # Require company name
        if not award.get("company_name") or len(award["company_name"]) < 3:
            return None

        # Infer year if no date parsed
        if not award.get("award_date") and year:
            award["award_date"] = f"{year}-01-01"

        return award

    def _extract_max_page(self, html: str) -> int | None:
        for pattern in [
            r'hidMax["\s]+value["\s]+=?\s*["\']?(\d+)',
            r'of\s+(\d+)\s+page',
            r'totalPages\s*=\s*(\d+)',
            r'maxPage\s*=\s*(\d+)',
        ]:
            m = re.search(pattern, html, re.I)
            if m:
                return int(m.group(1))
        return None

    # ── Save + cross-reference ─────────────────────────────────

    def _save_award(self, award: dict):
        try:
            self.save_tender(award)
            self.found += 1
            name = award.get("company_name", "")
            val  = award.get("award_value_omr")
            if name:
                self._cross_reference(name, award.get("commercial_reg"), val)
        except Exception as e:
            logger.debug(f"TenderAwards save error: {e}")

    def _cross_reference(self, company_name: str, cr: str | None, award_value: float | None):
        try:
            clean = re.sub(r'\b(LLC|SAOC|SAOG|Ltd|Co\.|Est\.|W\.L\.L)\b', '',
                           company_name, flags=re.I).strip()
            if cr:
                rows = self.db.query(
                    "SELECT id, tender_award_total_omr, tender_contract_count "
                    "FROM oman_smes WHERE commercial_reg_no = ? LIMIT 1", [cr]
                )
            else:
                rows = self.db.query(
                    "SELECT id, tender_award_total_omr, tender_contract_count "
                    "FROM oman_smes WHERE company_name = ? OR company_name LIKE ? LIMIT 1",
                    [company_name, f"%{clean[:30]}%"],
                )
            if not rows:
                return
            row       = rows[0]
            new_total = (row.get("tender_award_total_omr") or 0) + (award_value or 0)
            new_count = (row.get("tender_contract_count") or 0) + 1
            confidence = "High" if new_count >= 5 else ("Medium" if new_count >= 2 else "Low")
            band = self._revenue_band(new_total)
            self.db.execute(
                """UPDATE oman_smes SET
                     tender_award_total_omr = ?,
                     tender_contract_count  = ?,
                     revenue_confidence     = ?,
                     estimated_revenue_band = COALESCE(
                         NULLIF(estimated_revenue_band, 'Unknown'), ?)
                   WHERE id = ?""",
                [round(new_total, 2), new_count, confidence, band, row["id"]],
            )
        except Exception as e:
            logger.debug(f"TenderAwards cross-ref error for {company_name}: {e}")

    # ── Helpers ────────────────────────────────────────────────

    def _parse_omr(self, val: str) -> float | None:
        try:
            cleaned = re.sub(r"[^\d.]", "", val.replace(",", ""))
            f = float(cleaned)
            return f if f > 0 else None
        except Exception:
            return None

    def _parse_date(self, val: str) -> str | None:
        for fmt in ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y"]:
            try:
                return datetime.strptime(val.strip(), fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def _revenue_band(self, total_omr: float) -> str:
        if total_omr >= 5_000_000:
            return "5M–20M"
        if total_omr >= 1_000_000:
            return "1M–5M"
        if total_omr >= 100_000:
            return "Under 1M"
        return "Unknown"
