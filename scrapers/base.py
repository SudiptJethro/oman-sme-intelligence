"""
Base scraper - Cloudflare D1 for storage, Playwright for browser-based scraping.
"""
import os, time, logging, json, requests
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, BrowserContext

logger = logging.getLogger(__name__)

ARRAY_FIELDS = {"jsrs_categories", "oia_matched_codes"}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class D1Client:
    """Thin wrapper around the Cloudflare D1 REST API."""

    def __init__(self):
        account_id  = os.environ["CF_ACCOUNT_ID"]
        database_id = os.environ["CF_D1_DATABASE_ID"]
        api_token   = os.environ["CF_API_TOKEN"]
        self._url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type":  "application/json",
        }

    def query(self, sql: str, params: list = None) -> list:
        """Run a SELECT - returns list of row dicts."""
        resp = requests.post(
            self._url,
            headers=self._headers,
            json={"sql": sql, "params": params or []},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"D1 error: {data.get('errors')}")
        return data["result"][0]["results"]

    def execute(self, sql: str, params: list = None) -> dict:
        """Run INSERT / UPDATE / DELETE - returns D1 result meta."""
        resp = requests.post(
            self._url,
            headers=self._headers,
            json={"sql": sql, "params": params or []},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise Exception(f"D1 error: {data.get('errors')}")
        return data["result"][0]


class BaseScraper:
    source_name: str = "base"

    def __init__(self):
        self.db    = D1Client()
        self.run_id: int | None = None
        self.found = 0
        self.new   = 0

        # For binary downloads only (PDFs, raw APIs) - not for scraping HTML
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        })

        # Playwright browser - lazy-initialised on first browser_get() call
        self._pw:      object | None = None
        self._browser: object | None = None
        self._context: BrowserContext | None = None

    # ── Browser lifecycle ──────────────────────────────────────

    def _ensure_browser(self):
        if self._browser:
            return
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=BROWSER_UA,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Asia/Muscat",
            extra_http_headers={
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        # Hide automation fingerprint
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

    def browser_get(self, url: str, wait_for: str = None, timeout: int = 30_000) -> str | None:
        """
        Load a URL in the headless browser and return the rendered page HTML.
        Use this instead of self.get() for any site that may block bots or
        requires JavaScript to render content.
        """
        self._ensure_browser()
        page = None
        try:
            page = self._context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if wait_for:
                page.wait_for_selector(wait_for, timeout=timeout)
            else:
                # Give JS time to render dynamic content
                page.wait_for_timeout(2_000)
            return page.content()
        except Exception as e:
            logger.warning(f"[{self.source_name}] browser_get {url} failed: {e}")
            return None
        finally:
            if page:
                page.close()

    def _close_browser(self):
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._context = self._browser = self._pw = None

    # ── Run lifecycle ──────────────────────────────────────────

    def start_run(self):
        result = self.db.execute(
            "INSERT INTO scrape_runs (source, status) VALUES (?, ?)",
            [self.source_name, "running"],
        )
        self.run_id = result["meta"]["last_row_id"]
        logger.info(f"[{self.source_name}] Run #{self.run_id} started")

    def finish_run(self, error: str | None = None):
        self.db.execute(
            """UPDATE scrape_runs
               SET status = ?, leads_found = ?, leads_new = ?, error_msg = ?,
                   finished_at = datetime('now')
               WHERE id = ?""",
            ["failed" if error else "success", self.found, self.new, error, self.run_id],
        )
        logger.info(f"[{self.source_name}] Done - found={self.found} new={self.new}")

    # ── Serialisation helpers ──────────────────────────────────

    def _encode(self, data: dict) -> dict:
        """Serialize Python lists to JSON strings; coerce bools to 0/1."""
        out = dict(data)
        for field in ARRAY_FIELDS:
            if field in out and isinstance(out[field], list):
                out[field] = json.dumps(out[field])
        for k, v in out.items():
            if isinstance(v, bool):
                out[k] = int(v)
        return out

    # ── Save / upsert ──────────────────────────────────────────

    def save_company(self, data: dict) -> bool:
        """
        Upsert on commercial_reg_no (preferred) or company_name.
        Returns True if a new record was inserted.
        """
        self.found += 1
        data.setdefault("source", self.source_name)
        data = self._encode(data)

        try:
            cr = data.get("commercial_reg_no")
            if cr:
                rows = self.db.query(
                    "SELECT id FROM oman_smes WHERE commercial_reg_no = ?", [cr]
                )
            else:
                rows = self.db.query(
                    "SELECT id FROM oman_smes WHERE company_name = ?",
                    [data["company_name"]],
                )

            if rows:
                update = {k: v for k, v in data.items() if v is not None}
                sets   = ", ".join(f"{k} = ?" for k in update)
                vals   = list(update.values()) + [rows[0]["id"]]
                self.db.execute(f"UPDATE oman_smes SET {sets} WHERE id = ?", vals)
                return False
            else:
                cols         = ", ".join(data.keys())
                placeholders = ", ".join("?" * len(data))
                self.db.execute(
                    f"INSERT INTO oman_smes ({cols}) VALUES ({placeholders})",
                    list(data.values()),
                )
                self.new += 1
                return True

        except Exception as e:
            logger.error(f"[{self.source_name}] save_company error: {e} | data={data}")
            return False

    def save_tender(self, data: dict):
        try:
            data         = self._encode(data)
            cols         = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            self.db.execute(
                f"INSERT OR IGNORE INTO tender_awards ({cols}) VALUES ({placeholders})",
                list(data.values()),
            )
        except Exception as e:
            logger.error(f"[{self.source_name}] save_tender error: {e}")

    # ── CF Browser Rendering proxy ─────────────────────────────

    def cf_browser_get(self, url: str, wait_for: str = None, timeout: int = 30_000) -> str | None:
        """
        Fetch a JavaScript-rendered page via Cloudflare Browser Rendering (Worker /browse),
        for portals whose content only appears after scripts run.
        Falls back to local Playwright if WORKER_URL or BROWSE_TOKEN not set.
        """
        worker_url  = os.environ.get("WORKER_URL", "").rstrip("/")
        browse_token = os.environ.get("BROWSE_TOKEN", "")

        if worker_url and browse_token:
            try:
                resp = self.session.post(
                    f"{worker_url}/browse",
                    json={"url": url, "wait_for": wait_for, "timeout_ms": timeout},
                    headers={"X-Browse-Token": browse_token},
                    timeout=timeout / 1000 + 10,
                )
                if resp.ok:
                    html = resp.json().get("html", "")
                    if html and len(html) > 500:
                        logger.debug(f"[{self.source_name}] CF browser: {url} ({len(html)} bytes)")
                        return html
                logger.warning(f"[{self.source_name}] CF browser {url} returned {resp.status_code}")
            except Exception as e:
                logger.warning(f"[{self.source_name}] CF browser failed for {url}: {e}")

        # Fallback to local Playwright
        return self.browser_get(url, wait_for=wait_for, timeout=timeout)

    # ── HTTP helper (binary/API only) ──────────────────────────

    def get(self, url: str, **kwargs) -> requests.Response | None:
        """Plain HTTP GET - use only for PDFs or JSON APIs, not for HTML pages."""
        try:
            resp = self.session.get(url, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            logger.warning(f"[{self.source_name}] GET {url} failed: {e}")
            return None

    def polite_sleep(self, seconds: float = 2.0):
        time.sleep(seconds)

    # ── Entry point ────────────────────────────────────────────

    def run(self):
        self.start_run()
        error = None
        try:
            self.scrape()
        except Exception as e:
            logger.exception(f"[{self.source_name}] Fatal error: {e}")
            error = str(e)
        finally:
            self._close_browser()
        self.finish_run(error=error)

    def scrape(self):
        raise NotImplementedError
