"""
Oman SME Intel - Data Processing Pipeline
──────────────────────────────────────────
Inputs:
  data/vendors_full.json   (or .jsonl) - Tender Board vendor registry
  data/tenders_full.json   (or .jsonl) - Awarded tenders

Outputs:
  data/oman_smes_processed.json     - ready to bulk-insert into oman_smes table
  data/tender_awards_processed.json - ready to bulk-insert into tender_awards table
  data/pipeline_summary.json        - stats + top investment candidates

Run:
  python process_data.py
"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

DATA = Path("data")

# OIA-aligned categories from Tender Board registry
OIA_CATEGORIES = {
    "Construction and Maintenance",
    "Construction of Ports Roads Bridges Railways Dams and Maintenance",
    "Pipeline Network Construction and Drilling Wells",
    "Electromechanical and Telecommunications Contracting and Maintenance",
    "Information Technology Services",
    "Consulting Offices",
    "Supply",
    "Services",
    "Construction Works (Old)",
    "Supply and Services (Old)",
    "Training (Old)",
    "Consultancy  (Old)",
}

# Revenue band heuristics (contract count proxy - no OMR values in public list)
# Refined with category weight: construction/IT > services > supply
CATEGORY_AVG_CONTRACT_OMR = {
    "Construction and Maintenance": 250_000,
    "Construction of Ports Roads Bridges Railways Dams and Maintenance": 800_000,
    "Pipeline Network Construction and Drilling Wells": 600_000,
    "Electromechanical and Telecommunications Contracting and Maintenance": 300_000,
    "Information Technology Services": 150_000,
    "Consulting Offices": 100_000,
    "Supply": 80_000,
    "Services": 60_000,
    "Construction Works (Old)": 200_000,
    "Supply and Services (Old)": 70_000,
    "Training (Old)": 50_000,
    "Consultancy  (Old)": 90_000,
}
DEFAULT_AVG_CONTRACT_OMR = 75_000


def load_json_or_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        # Try alternate extension
        alt = path.with_suffix(".jsonl" if path.suffix == ".json" else ".json")
        if alt.exists():
            path = alt
        else:
            print(f"WARNING: {path} not found - skipping")
            return []

    rows = []
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return []
        # Try JSON array first
        try:
            data = json.loads(content)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and any(k in data for k in
                    ("registered_companies", "awarded_tenders", "vendors", "tenders")):
                # Combined file format
                rows = data.get("registered_companies") or data.get("vendors") or []
                return rows
        except json.JSONDecodeError:
            pass
        # JSONL
        for line in content.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def normalise_cr(cr: str) -> str:
    """Strip leading zeros and dashes for fuzzy matching."""
    if not cr:
        return ""
    return re.sub(r"[-\s]", "", cr).lstrip("0")


def normalise_name(name: str) -> str:
    """Uppercase, strip legal suffixes and punctuation."""
    name = name.upper().strip()
    for suffix in ["LLC", "L.L.C", "SAOC", "SAOG", "W.L.L", "WLL", "LTD",
                   "CO.", "CO,", "EST.", "EST,", "COMPANY", "TRADING"]:
        name = name.replace(suffix, "")
    return re.sub(r"[^A-Z0-9 ]", "", name).strip()


def parse_date(val: str) -> str | None:
    for fmt in ["%d-%m-%Y, %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"]:
        try:
            return datetime.strptime(val.strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            continue
    return None

_DATE_BLOB_RE = re.compile(r'\d{2}[-/]\d{2}[-/]\d{4}')

def extract_latest_date(val: str) -> str | None:
    """Parse a date or a concatenated multi-date blob - returns the latest ISO date."""
    if not val:
        return None
    result = parse_date(val)
    if result:
        return result
    # Pull all DD-MM-YYYY patterns from concatenated strings
    candidates = [parse_date(m) for m in _DATE_BLOB_RE.findall(val)]
    valid = [d for d in candidates if d]
    return max(valid) if valid else None


def is_expired(expiry_str: str, max_lag_days: int = 365) -> bool:
    d = parse_date(expiry_str)
    if not d:
        return False
    expiry = date.fromisoformat(d)
    return (date.today() - expiry).days > max_lag_days


def revenue_band(estimated_omr: float) -> str:
    if estimated_omr >= 20_000_000:
        return "Over 20M"
    if estimated_omr >= 5_000_000:
        return "5M–20M"
    if estimated_omr >= 1_000_000:
        return "1M–5M"
    if estimated_omr > 0:
        return "Under 1M"
    return "Unknown"


def confidence(contract_count: int) -> str:
    if contract_count >= 5:
        return "High"
    if contract_count >= 2:
        return "Medium"
    if contract_count >= 1:
        return "Low"
    return "Low"


# ── Load data ─────────────────────────────────────────────────

print("Loading vendors...")
vendors_raw = load_json_or_jsonl(DATA / "vendors_full.json")
if not vendors_raw:
    vendors_raw = load_json_or_jsonl(DATA / "vendors_full.jsonl")
print(f"  {len(vendors_raw)} vendor rows")

# Multi-registration companies store \n-joined values in category + reg_expiry.
# Take first category (primary), latest expiry date (last entry).
for v in vendors_raw:
    if '\n' in (v.get('category') or ''):
        v['category'] = v['category'].split('\n')[0].strip()
    if '\n' in (v.get('reg_expiry') or ''):
        v['reg_expiry'] = v['reg_expiry'].split('\n')[-1].strip()

# Column-shift fix: when an extra phone/fax number ends up in the 'category' slot,
# the real category is in 'reg_expiry' and the real expiry date is in 'company_type'.
# Detected when category is all-digits (6+) and reg_expiry is not a date.
_SHIFT_NUM = re.compile(r'^\d{6,}$')
_DATE_PAT  = re.compile(r'^\d{2}[-/]\d{2}[-/]\d{4}')
_shifted = 0
for v in vendors_raw:
    cat    = (v.get('category') or '').strip()
    expiry = (v.get('reg_expiry') or '').strip()
    if _SHIFT_NUM.match(cat) and not _DATE_PAT.match(expiry):
        v['category']    = expiry                             # real category
        v['reg_expiry']  = (v.get('company_type') or '').strip()  # real date
        v['company_type'] = None                              # SME/Local lost in shift
        _shifted += 1
print(f"  Column-shift fix applied to {_shifted} vendor records")

print("Loading tenders...")
tenders_raw = load_json_or_jsonl(DATA / "tenders_full.json")
if not tenders_raw:
    tenders_raw = load_json_or_jsonl(DATA / "tenders_full.jsonl")
print(f"  {len(tenders_raw)} tender rows")

# ── Build tender index (CR + name → list of tenders) ─────────

print("Building tender index...")
tenders_by_cr:   dict[str, list] = defaultdict(list)
tenders_by_name: dict[str, list] = defaultdict(list)

tender_awards_out = []

for t in tenders_raw:
    company = t.get("company_name") or t.get("awarded_company") or ""
    cr      = t.get("cr_number") or t.get("commercial_reg") or ""
    norm_cr = normalise_cr(cr)
    norm_nm = normalise_name(company)

    award = {
        "company_name":  company,
        "commercial_reg": cr,
        "tender_ref":    t.get("tender_no") or t.get("tender_number") or "",
        "tender_title":  t.get("tender_title") or "",
        "award_value_omr": t.get("awarded_value_omr") or t.get("award_value_omr"),
        "award_date":    parse_date(t.get("awarded_date") or ""),
        "buyer_entity":  t.get("entity_name") or "",
        "sector":        t.get("category_grade") or "",
        "source":        "tender_awards",
    }
    tender_awards_out.append(award)

    if norm_cr:
        tenders_by_cr[norm_cr].append(award)
    if norm_nm and len(norm_nm) > 3:
        tenders_by_name[norm_nm].append(award)

print(f"  {len(tender_awards_out)} tender records indexed")

# ── Process vendors → oman_smes ───────────────────────────────

print("Processing vendors...")
smes_out = []
stats = {"total": 0, "expired_dropped": 0, "non_oia": 0, "sme_type": 0,
         "local_type": 0, "other_type": 0, "with_tenders": 0,
         "band_1m_5m": 0, "band_under_1m": 0, "band_5m_plus": 0}

seen_cr = set()

for v in vendors_raw:
    stats["total"] += 1

    company_name = (v.get("company_name") or "").strip()
    cr_raw       = (v.get("cr_number") or "").strip()
    category     = (v.get("category") or "").strip()
    _ct_raw      = (v.get("company_type") or v.get("vendType") or "").strip()
    # Only accept known valid company types; dates and other garbage → None
    VALID_CTYPES = {"SME", "Local", "International"}
    company_type = _ct_raw if _ct_raw in VALID_CTYPES else ""
    expiry_raw   = (v.get("reg_expiry") or v.get("reg_expiry_date") or "").strip()
    tel          = (v.get("tel") or "").strip()
    address      = (v.get("address") or "").strip()

    if not company_name:
        continue

    # Dedup by CR
    norm_cr = normalise_cr(cr_raw)
    if norm_cr and norm_cr in seen_cr:
        continue
    if norm_cr:
        seen_cr.add(norm_cr)

    # Drop expired > 1 year
    if expiry_raw and is_expired(expiry_raw, max_lag_days=365):
        stats["expired_dropped"] += 1
        continue

    # OIA category check
    oia_match = any(cat.lower() in category.lower() or category.lower() in cat.lower()
                    for cat in OIA_CATEGORIES)

    # Count by type
    ct = company_type.upper()
    if "SME" in ct:
        stats["sme_type"] += 1
    elif "LOCAL" in ct:
        stats["local_type"] += 1
    else:
        stats["other_type"] += 1

    # Cross-reference tenders
    matched_tenders = []
    if norm_cr:
        matched_tenders = tenders_by_cr.get(norm_cr, [])
    if not matched_tenders:
        norm_nm = normalise_name(company_name)
        matched_tenders = tenders_by_name.get(norm_nm, [])

    contract_count = len(matched_tenders)
    if contract_count > 0:
        stats["with_tenders"] += 1

    # Sum actual award values where available; estimate the rest
    avg_val = CATEGORY_AVG_CONTRACT_OMR.get(category, DEFAULT_AVG_CONTRACT_OMR)
    actual_total = sum(t["award_value_omr"] for t in matched_tenders if t.get("award_value_omr"))
    missing_count = sum(1 for t in matched_tenders if not t.get("award_value_omr"))
    estimated_tender_total = actual_total + missing_count * avg_val
    estimated_revenue      = estimated_tender_total / 0.30 if contract_count > 0 else 0
    band = revenue_band(estimated_revenue)

    if band == "1M–5M":
        stats["band_1m_5m"] += 1
    elif band in ("5M–20M", "Over 20M"):
        stats["band_5m_plus"] += 1
    elif band == "Under 1M" and contract_count > 0:
        stats["band_under_1m"] += 1

    sme = {
        "company_name":           company_name,
        "company_name_ar":        None,
        "commercial_reg_no":      cr_raw or None,
        "cr_expiry_date":         extract_latest_date(expiry_raw),
        "governorate":            address[:100] if address else None,
        "contact_phone":          tel or None,
        "vendor_reg_expiry":      extract_latest_date(expiry_raw),
        "vendor_category":        category or None,
        "company_type":           company_type or None,
        "oia_code_match":         1 if oia_match else 0,
        "oia_match_count":        1 if oia_match else 0,
        "tender_award_total_omr": estimated_tender_total,
        "tender_contract_count":  contract_count,
        "estimated_revenue_band": band,
        "revenue_confidence":     confidence(contract_count),
        "source":                 "tender_vendor_registry",
        "enriched":               0,
    }
    smes_out.append(sme)

stats["output_total"] = len(smes_out)

# ── Save outputs ──────────────────────────────────────────────

DATA.mkdir(exist_ok=True)

with open(DATA / "oman_smes_processed.json", "w", encoding="utf-8") as f:
    json.dump(smes_out, f, ensure_ascii=False, indent=2)

with open(DATA / "tender_awards_processed.json", "w", encoding="utf-8") as f:
    json.dump(tender_awards_out, f, ensure_ascii=False, indent=2)

# Top candidates: OIA match + tenders + not expired
top = sorted(
    [s for s in smes_out if s["oia_code_match"] and s["tender_contract_count"] > 0],
    key=lambda x: (x["tender_contract_count"], x["estimated_revenue_band"]),
    reverse=True,
)[:50]

summary = {
    "generated_at": datetime.utcnow().isoformat(),
    "stats": stats,
    "revenue_distribution": {
        "1M_5M":    stats["band_1m_5m"],
        "5M_plus":  stats["band_5m_plus"],
        "under_1M": stats["band_under_1m"],
        "unknown":  len(smes_out) - stats["band_1m_5m"] - stats["band_5m_plus"] - stats["band_under_1m"],
    },
    "top_candidates_preview": top[:10],
}

with open(DATA / "pipeline_summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

# ── Print summary ─────────────────────────────────────────────

sys.stdout.buffer.write(b"\n=== PIPELINE SUMMARY ===\n")
sys.stdout.buffer.write(f"Vendors loaded:        {stats['total']}\n".encode())
sys.stdout.buffer.write(f"Expired (dropped):     {stats['expired_dropped']}\n".encode())
sys.stdout.buffer.write(f"Output SMEs:           {stats['output_total']}\n".encode())
sys.stdout.buffer.write(f"  SME type:            {stats['sme_type']}\n".encode())
sys.stdout.buffer.write(f"  Local type:          {stats['local_type']}\n".encode())
sys.stdout.buffer.write(f"With tender matches:   {stats['with_tenders']}\n".encode())
sys.stdout.buffer.write(f"Revenue band 1M-5M:    {stats['band_1m_5m']}\n".encode())
sys.stdout.buffer.write(f"Revenue band 5M+:      {stats['band_5m_plus']}\n".encode())
sys.stdout.buffer.write(f"Tenders processed:     {len(tender_awards_out)}\n".encode())
sys.stdout.buffer.write(b"\nTop candidates (OIA match + tenders):\n")
for i, c in enumerate(top[:10], 1):
    line = f"  {i:2}. {c['company_name'][:40]:<40} | {c['tender_contract_count']} contracts | {c['estimated_revenue_band']}\n"
    sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
sys.stdout.buffer.write(b"\nOutputs:\n")
sys.stdout.buffer.write(b"  data/oman_smes_processed.json\n")
sys.stdout.buffer.write(b"  data/tender_awards_processed.json\n")
sys.stdout.buffer.write(b"  data/pipeline_summary.json\n")
