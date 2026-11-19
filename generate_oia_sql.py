"""
Generate SQL for:
  1. oia_protected_codes table - all 34 OIA Mandatory List v4.0 segments
  2. oman_smes updates - oia_matched_codes, oia_match_count, oia_code_match, icv_certified

Run:
  python generate_oia_sql.py
  python generate_oia_sql.py --execute
"""

import json
import subprocess
import shutil
import sys
from pathlib import Path

DATA = Path("data")
DB_NAME = "oman-sme-intel"

# ── OIA Mandatory List v4.0 (Dec 2025) - 34 segments ─────────────────────────
# Source: OIA Protected List Version 4.0, December 2025
# 15 Service segments + 19 Product segments = 34 total
# 148 service items + 236 product items = 384 total

OIA_CODES = [
    # Services (S-01 to S-15)
    {"code": "S-01", "description": "General Administration",                                 "category": "Services", "sector": "Administration & Office"},
    {"code": "S-02", "description": "Maintenance Services & Spare Parts",                     "category": "Services", "sector": "Engineering & Maintenance"},
    {"code": "S-03", "description": "Legal",                                                  "category": "Services", "sector": "Professional Services"},
    {"code": "S-04", "description": "Transportation & Distribution",                          "category": "Services", "sector": "Logistics & Trade"},
    {"code": "S-05", "description": "End-to-End Logistics",                                   "category": "Services", "sector": "Logistics & Trade"},
    {"code": "S-06", "description": "Non-Destructive Testing",                                "category": "Services", "sector": "Engineering & Maintenance"},
    {"code": "S-07", "description": "Waste Management",                                       "category": "Services", "sector": "Environment & Utilities"},
    {"code": "S-08", "description": "Human Capital and Consultancy",                          "category": "Services", "sector": "Professional Services"},
    {"code": "S-09", "description": "Value-added Services",                                   "category": "Services", "sector": "Technology & Innovation"},
    {"code": "S-10", "description": "Media",                                                  "category": "Services", "sector": "Media & Communications"},
    {"code": "S-11", "description": "Architect",                                              "category": "Services", "sector": "Construction & Infrastructure"},
    {"code": "S-12", "description": "Communication",                                          "category": "Services", "sector": "Technology & Innovation"},
    {"code": "S-13", "description": "Training & Development Programs",                        "category": "Services", "sector": "Professional Services"},
    {"code": "S-14", "description": "Warehouse",                                              "category": "Services", "sector": "Logistics & Trade"},
    {"code": "S-15", "description": "Aviation Services",                                      "category": "Services", "sector": "Transport & Aviation"},
    # Products (P-01 to P-19)
    {"code": "P-01", "description": "Office Supplies & Uniform",                              "category": "Products", "sector": "Administration & Office"},
    {"code": "P-02", "description": "Medicine",                                               "category": "Products", "sector": "Healthcare & Pharmaceuticals"},
    {"code": "P-03", "description": "Cables",                                                 "category": "Products", "sector": "Electrical & Electronics"},
    {"code": "P-04", "description": "Sanitary Ware",                                          "category": "Products", "sector": "Construction & Infrastructure"},
    {"code": "P-05", "description": "Buses",                                                  "category": "Products", "sector": "Transport & Aviation"},
    {"code": "P-06", "description": "Industrial Paint",                                       "category": "Products", "sector": "Industrial & Manufacturing"},
    {"code": "P-07", "description": "General Consumables",                                    "category": "Products", "sector": "Administration & Office"},
    {"code": "P-08", "description": "Pipes and Fittings",                                     "category": "Products", "sector": "Construction & Infrastructure"},
    {"code": "P-09", "description": "Cleaning Products & Chemicals",                          "category": "Products", "sector": "Environment & Utilities"},
    {"code": "P-10", "description": "IT Equipment",                                           "category": "Products", "sector": "Technology & Innovation"},
    {"code": "P-11", "description": "Wooden Pallets",                                         "category": "Products", "sector": "Logistics & Trade"},
    {"code": "P-12", "description": "Industrial Chemicals & Gases",                           "category": "Products", "sector": "Industrial & Manufacturing"},
    {"code": "P-13", "description": "Animal Feed & Veterinary Products",                      "category": "Products", "sector": "Agriculture & Food"},
    {"code": "P-14", "description": "Gifts",                                                  "category": "Products", "sector": "Administration & Office"},
    {"code": "P-15", "description": "HSE Equipment",                                          "category": "Products", "sector": "Industrial & Manufacturing"},
    {"code": "P-16", "description": "Food & Beverages",                                       "category": "Products", "sector": "Agriculture & Food"},
    {"code": "P-17", "description": "Construction Material & Services",                       "category": "Products", "sector": "Construction & Infrastructure"},
    {"code": "P-18", "description": "Agriculture and Fertilizers",                            "category": "Products", "sector": "Agriculture & Food"},
    {"code": "P-19", "description": "Transformers",                                           "category": "Products", "sector": "Electrical & Electronics"},
]

# ── Vendor category → OIA code mapping ────────────────────────────────────────
# Maps Tender Board vendor_category strings to matching OIA segment codes

CATEGORY_TO_OIA = {
    "Construction and Maintenance": ["S-02", "S-11", "P-17"],
    "Construction of Ports Roads Bridges Railways Dams and Maintenance": ["P-17", "S-02", "P-08"],
    "Pipeline Network Construction and Drilling Wells": ["P-08", "P-17", "S-02"],
    "Electromechanical and Telecommunications Contracting and Maintenance": ["S-12", "S-02", "P-03"],
    "Information Technology Services": ["P-10", "S-09", "S-12"],
    "Consulting Offices": ["S-08", "S-01", "S-03"],
    "Supply": ["P-01", "P-07", "P-09", "P-15"],
    "Services": ["S-01", "S-07", "S-08", "S-09"],
    "Construction Works (Old)": ["P-17", "S-02"],
    "Supply and Services (Old)": ["P-01", "P-07", "S-01"],
    "Training (Old)": ["S-13"],
    "Consultancy  (Old)": ["S-08", "S-03"],
}

# All categories that appear in OIA mandatory list = ICV eligible
ICV_ELIGIBLE_CATEGORIES = set(CATEGORY_TO_OIA.keys())


def q(val):
    if val is None:
        return "NULL"
    return "'" + str(val).replace("'", "''") + "'"


def generate_oia_codes_sql():
    lines = [
        "-- oia_protected_codes: OIA Mandatory List v4.0 (Dec 2025)",
        "-- 34 segments: 15 services + 19 products",
        "",
        "DELETE FROM oia_protected_codes;",
        "",
        "INSERT INTO oia_protected_codes (code, description, category, sector) VALUES",
    ]
    rows = [
        f"  ({q(c['code'])}, {q(c['description'])}, {q(c['category'])}, {q(c['sector'])})"
        for c in OIA_CODES
    ]
    lines.append(",\n".join(rows) + ";")
    lines.append("")
    return "\n".join(lines)


def extract_oia_codes(cat):
    """Match a vendor_category string to OIA codes.

    Handles exact match, concatenated multi-category strings (no separator),
    and case-insensitive partial matches. Consumes longest keys first so
    'Information Technology Services' is not split into 'Services'.
    """
    if not cat:
        return []
    # Exact match - fastest path
    if cat in CATEGORY_TO_OIA:
        return list(CATEGORY_TO_OIA[cat])
    # Substring: consume longest matching keys first to avoid short-key false matches
    found = set()
    remaining = cat
    for key in sorted(CATEGORY_TO_OIA.keys(), key=len, reverse=True):
        if key in remaining:
            found.update(CATEGORY_TO_OIA[key])
            remaining = remaining.replace(key, "")
    if found:
        return sorted(found)
    # Case-insensitive fallback
    cat_lower = cat.lower()
    for key in sorted(CATEGORY_TO_OIA.keys(), key=len, reverse=True):
        if key.lower() in cat_lower:
            found.update(CATEGORY_TO_OIA[key])
            cat_lower = cat_lower.replace(key.lower(), "")
    return sorted(found)


def generate_smes_oia_sql():
    """Generate UPDATE statements for vendor_category, oia_matched_codes,
    oia_match_count, oia_code_match, icv_certified."""
    smes_path = DATA / "oman_smes_processed.json"
    if not smes_path.exists():
        print("  ERROR: data/oman_smes_processed.json not found - run process_data.py first")
        return ""

    smes = json.loads(smes_path.read_text("utf-8"))
    print(f"  Loaded {len(smes)} SMEs")

    lines = [
        "-- oman_smes OIA code + ICV + vendor_category updates (generated by generate_oia_sql.py)",
        "",
    ]

    updated = 0
    icv_count = 0

    for sme in smes:
        cat      = (sme.get("vendor_category") or "").strip()
        cr       = (sme.get("commercial_reg_no") or "").strip()
        name     = (sme.get("company_name") or "").strip()
        exp      = sme.get("vendor_reg_expiry")   # already ISO date or None
        ctype    = sme.get("company_type")         # "SME", "Local", or None

        codes     = extract_oia_codes(cat)
        oia_match = 1 if codes else 0
        icv       = 1 if codes else 0

        if icv:
            icv_count += 1

        if cr:
            where = f"commercial_reg_no = {q(cr)}"
        else:
            where = f"company_name = {q(name)}"

        codes_json = json.dumps(codes)
        lines.append(
            f"UPDATE oman_smes SET "
            f"vendor_category = {q(cat or None)}, "
            f"vendor_reg_expiry = {q(exp)}, "
            f"company_type = {q(ctype)}, "
            f"oia_matched_codes = {q(codes_json)}, "
            f"oia_match_count = {len(codes)}, "
            f"oia_code_match = {oia_match}, "
            f"icv_certified = {icv} "
            f"WHERE {where};"
        )
        updated += 1

    print(f"  {updated} UPDATE statements | {icv_count} ICV-eligible companies")
    return "\n".join(lines)


def run_wrangler(sql_file):
    print(f"\n  Running: wrangler d1 execute {DB_NAME} --file={sql_file} --remote")
    wrangler_cmd = shutil.which("wrangler") or "wrangler"
    cmd = f'"{wrangler_cmd}" d1 execute {DB_NAME} --file="{sql_file}" --remote'
    result = subprocess.run(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
    )
    out = ((result.stdout or "") + (result.stderr or "")).encode("ascii", "replace").decode("ascii")
    if result.returncode == 0:
        print(f"  OK: {out.strip()[:300]}")
    else:
        print(f"  ERROR: {out.strip()[:500]}")
    return result.returncode == 0


def main():
    execute = "--execute" in sys.argv
    DATA.mkdir(exist_ok=True)

    print("Generating oia_protected_codes SQL...")
    oia_sql = generate_oia_codes_sql()
    oia_file = DATA / "inserts_oia_codes.sql"
    oia_file.write_text(oia_sql, encoding="utf-8")
    print(f"  -> {oia_file} ({oia_file.stat().st_size} bytes)")

    print("\nGenerating oman_smes OIA/ICV update SQL...")
    smes_sql = generate_smes_oia_sql()
    if smes_sql:
        smes_file = DATA / "updates_oia_icv.sql"
        smes_file.write_text(smes_sql, encoding="utf-8")
        print(f"  -> {smes_file} ({smes_file.stat().st_size // 1024}KB)")

    if execute:
        print("\n--- Executing OIA codes ---")
        run_wrangler(oia_file)
        if smes_sql:
            print("\n--- Executing SME OIA/ICV updates ---")
            run_wrangler(smes_file)
    else:
        print("\nNext: python generate_oia_sql.py --execute")


if __name__ == "__main__":
    main()
