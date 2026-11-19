# Browser-console exporters

The full Tender Board history behind this project (about 20,000 registered vendors and 20,000 awarded tenders) was collected with these two scripts. They were pasted into the Chrome DevTools Console on the Tender Board's public report pages and run there, not from Python.

| Script | Run it on | Downloads |
|---|---|---|
| `scrape_vendors_console.js` | Registered vendors report: `https://etendering.tenderboard.gov.om/product/ReportAction?eventFlag=RegVendorPublic` | `vendors_full.json` |
| `scrape_tenders_console.js` | Completed tenders report: `https://etendering.tenderboard.gov.om/product/ReportAction?eventFlag=CompletedTendersForPublic` | `tenders_full.json` |

## Why the console

The report pages page through results with a session-bound form, and the tenders report signs each page request in the page's own JavaScript before submitting it. A script running inside the open page uses the same session and the page's own pagination function, so every request is the same one a click on "Next" would send. That proved more reliable than reproducing the form from outside the browser.

## How to run

1. Open the report page in Chrome and wait for the first page of results to load.
2. Open DevTools (F12) and go to the **Console** tab. If Chrome asks, type `allow pasting` first.
3. Paste the whole script and press Enter.
4. Watch the progress lines (`[Scraper] Page 12/402 | Total: 600`). The scripts pause between requests (300–500 ms) to keep the load on the site light.
5. When it finishes, the browser downloads the JSON file. Move it into the repo's `data/` folder.

## Resuming

Progress is saved to `localStorage` after every page. If the session expires or the tab is closed, reload the report page and paste the script again. It picks up from the last saved page. The checkpoint is cleared once the download completes.

## Extra steps

- `scrape_tenders_console.js` also opens each tender's Opening Report to read the awarded value in OMR, which is what the turnover proxy is built on.
- `scrape_vendors_console.js` ends with an optional, commented-out Phase 2 that adds each vendor's sub-category and grade from its detail page. Instructions are in the script.

## Next

From the repo root:

```bash
python pipeline/process_data.py      # match vendors to awards, turnover proxy, OIA alignment
python pipeline/enrich_sectors.py    # Vision 2040 sector and pillar
python pipeline/generate_oia_sql.py  # OIA protected codes and ICV flags
python pipeline/load_to_d1.py        # bulk insert into Cloudflare D1 via wrangler
```

Use these only on pages you are allowed to access, at a gentle pace, and under the site's terms of use. The exported files contain company contact details, so keep them out of version control (`data/` is git-ignored).
