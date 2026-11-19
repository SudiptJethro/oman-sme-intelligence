# Methodology

## The problem

The brief was to find private Omani SMEs that an investor could back or acquire. The target companies had to meet four criteria:

1. **Turnover of OMR 1M–5M.** That is large enough to be a real business, small enough to be acquirable, and below the size where private equity funds compete.
2. **Activity on the OIA Mandatory List.** These are the 384 products and services that must be procured locally, so companies in them benefit from In-Country Value (ICV) policy.
3. **An active JSRS certification**, the supplier qualification used across Oman's energy sector.
4. **Alignment with an Oman Vision 2040 sector.**

The hard part is the first criterion. Private Omani SMEs don't publish accounts, so no public source reports their revenue.

## Turnover proxy: government contracts

The one reliable public signal is the **Tender Board**. Every government contract awarded since 2019 is published with the winning company and the contract value in OMR. The pipeline uses the **total OMR a company has won across all government contracts** as a stand-in for turnover. This is reasonable for three reasons:

1. Government buyers (ministries, municipalities and state entities) are the largest customers for locally produced goods and services in Oman.
2. The OIA list covers categories that must be bought locally, so the target companies are disproportionately government-facing.
3. Adding up several contracts over several years smooths out one-off spikes.

| Field | Meaning |
|---|---|
| `tender_award_total_omr` | Sum of all contract values won since 2019 |
| `tender_contract_count` | Number of distinct contracts won |
| `estimated_revenue_band` | Band derived from the total (below) |
| `revenue_confidence` | High (5+ contracts), Medium (2–4), Low (1) |

| Band | Total OMR awarded |
|---|---|
| Under 1M | below OMR 1,000,000 |
| **1M–5M** | OMR 1,000,000 to 5,000,000 (**target**) |
| 5M–20M | OMR 5,000,000 to 20,000,000 |
| Over 20M | above OMR 20,000,000 |

When a vendor appears in the registry but its award values are missing, `pipeline/process_data.py` estimates its size from contract count multiplied by a typical contract size for its Tender Board category (for example, OMR 250,000 for construction and OMR 60,000 for services).

## Investment-ready score (0–100)

The `investment_ready` view in `schema.sql` scores every company whose registration is current, that has **at least two contracts**, and whose total awards fall in the **1M–5M band**:

| Signal | Points | Rule |
|---|---|---|
| Size in the 1M–5M sweet spot | up to 35 | 35 inside the band, otherwise pro-rata (capped at 30) |
| OIA Mandatory List alignment | 30 | Registered category matches a protected activity |
| Recency | 20 | At least one award in the last 24 months |
| Frequency | up to 15 | 3 points per contract, capped at 15 |

`tests/test_scoring.py` loads the real schema into SQLite (which is what Cloudflare D1 runs on) and checks that the view produces exactly these scores.

## Sector mapping and AI enrichment

`pipeline/enrich_sectors.py` assigns each company a Vision 2040 sector and pillar. It uses the buyers it has won contracts from (for example, Ministry of Health contracts suggest Healthcare) and keywords in its name. For shortlisted companies, the Worker's `/enrich` routes ask Workers AI (Llama 3.1 8B) for a 1–10 investment score, a short summary, growth signals and risk flags. The AI score supports the analyst's judgement and is kept separate from the rule-based score above.

## Limitations

- **Government work only.** A company that mostly sells to the private sector will be under-ranked.
- **Cumulative, not annual.** OMR 3M won over six years averages OMR 500K a year, which is below the target. The recency points only partly correct for this.
- **Contract value is not revenue.** Mark-ups and other revenue streams make the proxy understate turnover for some companies.
- **Prefer high-confidence leads.** A company with five or more contracts gives a more reliable signal than one that won a single large contract.
- **JSRS status is not yet in the score.** It needs a JSRS buyer account. In public mode the JSRS scraper only confirms individual companies. Once buyer credentials are added, the certified supplier directory would cross-check the revenue proxy, because JSRS certification has minimum financial requirements.
