# How the Tender Board data was collected

The full Tender Board history behind this project, about 20,000 registered vendors and 20,000 awarded tenders, came from two public reports on the Oman Tender Board's eTendering portal:

- **Registered vendors**: every supplier registered with the Tender Board, with its category, registration expiry and company type.
- **Completed (awarded) tenders**: every awarded tender, with the buying entity, category and award date. Each tender links to a public Opening Report that lists the bids and the winning value in OMR.

The export scripts themselves are not published. This page describes the logic so the approach can be understood and reproduced responsibly.

## Method

The data was exported from a normal Chrome session with the report page open, using a short script run from the DevTools Console. For each report, the script:

1. **Read the page count** from the report's own pagination.
2. **Requested each results page in turn**, the same request the page sends when you click "Next".
3. **Parsed the results table** into one record per row (the fields are listed below).
4. **For tenders, opened each tender's Opening Report** to read the winning company and the awarded value in OMR. This value is what the turnover proxy is built on.
5. **Saved progress after every page** in the browser's local storage. If the session timed out, reloading the page and running the script again resumed from the last saved page.
6. **Paused 300–500 ms between requests**, so the load stayed close to a person browsing.
7. **Downloaded the results as JSON** when finished (`vendors_full.json`, `tenders_full.json`).

A second, optional pass over the vendor detail pages added each vendor's sub-category and grade.

## Output format

The pipeline accepts any collection method that produces these two files in `data/`, as JSON arrays or JSON Lines.

`vendors_full.json`, one object per vendor:

| Field | Example |
|---|---|
| `company_name` | `Demo Horizon Engineering LLC` |
| `cr_number` | commercial registration number |
| `address`, `tel` | as listed in the registry |
| `category` | Tender Board registered category |
| `reg_expiry` | `31-12-2026` |
| `company_type` | `SME` or `Local` |

`tenders_full.json`, one object per awarded tender:

| Field | Example |
|---|---|
| `tender_no`, `tender_title` | tender reference and title |
| `entity_name` | buying ministry or authority |
| `category_grade` | `Construction and Maintenance[First,Second]` |
| `awarded_date` | `14-05-2024` |
| `awarded_company` | winning bidder (`company_name` also accepted) |
| `awarded_value_omr` | winning bid value |

## Next

From the repo root:

```bash
python pipeline/process_data.py      # match vendors to awards, turnover proxy, OIA alignment
python pipeline/enrich_sectors.py    # Vision 2040 sector and pillar
python pipeline/generate_oia_sql.py  # OIA protected codes and ICV flags
python pipeline/load_to_d1.py        # bulk insert into Cloudflare D1 via wrangler
```

## Responsible use

If you collect this data yourself, read the portal's terms of use first, keep the request rate low, and don't republish the raw exports. The vendor registry includes addresses and phone numbers, and some of them belong to sole proprietors, which can make them personal data under Oman's Personal Data Protection Law. `data/` is git-ignored so these files stay out of version control.
