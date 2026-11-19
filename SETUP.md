# Setup

Everything runs on free tiers: Cloudflare D1 (database), Workers and Workers AI (API and enrichment), GitHub Actions (scrapers) and a static dashboard.

To try the dashboard without any setup, open `docs/index.html`. It runs in demo mode on synthetic data.

## 1. Database

```bash
npm install -g wrangler
wrangler login
wrangler d1 create oman-sme-intel          # copy the database_id it prints
wrangler d1 execute oman-sme-intel --file=schema.sql
```

Paste the `database_id` into `wrangler.jsonc`. The schema creates `oman_smes`, `oia_protected_codes`, `tender_awards`, `scrape_runs` and the `investment_ready` view.

## 2. Worker and dashboard

```bash
cd worker && npm install && cd ..
wrangler secret put API_TOKEN               # choose a long random string
wrangler deploy                             # serves the API and docs/ (the dashboard)
```

**Every data route requires `Authorization: Bearer <API_TOKEN>`.** If `API_TOKEN` isn't set, the Worker refuses all data requests, so a deployment is never left open to the public. Check it's working:

```bash
curl https://oman-sme-intel.<your-subdomain>.workers.dev/                      # health check, no token
curl -H "Authorization: Bearer $API_TOKEN" https://oman-sme-intel.<your-subdomain>.workers.dev/stats
```

To connect the dashboard to your Worker, open `/#setup` on the dashboard, enter the Worker URL and token, and click Save. Both are stored only in your browser.

## 3. Scrapers (GitHub Actions)

The Python scrapers write to D1 through the Cloudflare REST API. Create an API token at https://dash.cloudflare.com/profile/api-tokens with **D1: Edit** permission. Then add these repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `CF_ACCOUNT_ID` | Cloudflare dashboard, right sidebar |
| `CF_D1_DATABASE_ID` | from `wrangler d1 create` |
| `CF_API_TOKEN` | the D1 token |
| `WORKER_URL` | your Worker URL |
| `API_TOKEN` | the same value as the Worker secret |
| `BROWSE_TOKEN` | optional: enables the Worker's `/browse` rendering proxy |
| `JSRS_BUYER_MODE`, `JSRS_USERNAME`, `JSRS_PASSWORD` | optional: only if you hold a JSRS buyer account |

Run the workflow from **Actions → Oman SME Intel Scraper → Run workflow**. Start with `oia_list` (it seeds the 384 protected activities), then `tender_vendor_registry` and `tender_awards`. The workflow runs on demand only. Add a `schedule:` trigger if you want regular runs.

## 4. Bulk loads

The full Tender Board history was exported in the browser, not by the Python scrapers. The export scripts aren't published. [docs/data-collection.md](docs/data-collection.md) describes the method and the format of the two input files, `vendors_full.json` and `tenders_full.json`.

Put both files in `data/`, then run from the repo root:

```bash
python pipeline/process_data.py      # match vendors to awards, revenue proxy, OIA alignment
python pipeline/enrich_sectors.py    # Vision 2040 sector and pillar
python pipeline/generate_oia_sql.py  # OIA protected codes and ICV flags
python pipeline/load_to_d1.py        # bulk insert via wrangler
```

Scraped data is written to `data/`, which is git-ignored.
