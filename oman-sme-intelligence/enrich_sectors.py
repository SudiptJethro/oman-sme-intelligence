"""
Generate UPDATE SQL to populate vision2040_sector, vision2040_pillar,
oia_matched_codes for all companies that have tender award data.

Source: data/tenders_full.json (category_grade field)
Output: data/updates_sector_from_tenders.sql
Run:    python enrich_sectors.py
Load:   cd worker && wrangler d1 execute oman-sme-intel --file=../data/updates_sector_from_tenders.sql --remote
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path("data")

# category_grade field examples:
#   "Supply[Excellent,First,Second,Third,Fourth]"
#   "Construction and Maintenance[First,Second]"
#   "Information Technology Services[First]"

CAT_MAP = {
    "supply":                          ("Logistics & Trade",     "Economic Diversification", ["L-001", "L-002"]),
    "services":                        ("Business Services",     "Economic Diversification", ["B-001"]),
    "information technology":          ("ICT & Digital",         "Digital Economy",          ["D-001", "D-002"]),
    "consulting":                      ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "construction and maintenance":    ("Construction",          "Infrastructure",           ["C-001", "C-002"]),
    "construction of ports":           ("Ports & Logistics",     "Economic Diversification", ["L-003", "L-004"]),
    "pipeline":                        ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "electromechanical":               ("Engineering Services",  "Infrastructure",           ["C-003"]),
    "training":                        ("Education & Training",  "Knowledge Economy",        ["P-002"]),
    "medical":                         ("Healthcare",            "Social Development",       ["H-001"]),
    "health":                          ("Healthcare",            "Social Development",       ["H-001"]),
    "tourism":                         ("Tourism & Hospitality", "Tourism Development",      ["T-001", "T-002"]),
    "hospitality":                     ("Tourism & Hospitality", "Tourism Development",      ["T-001"]),
    "food":                            ("Agri & Food",           "Food Security",            ["A-001"]),
    "agriculture":                     ("Agri & Food",           "Food Security",            ["A-001", "A-002"]),
    "fisheries":                       ("Fisheries & Aquaculture","Food Security",           ["A-003"]),
    "manufacturing":                   ("Manufacturing",         "Economic Diversification", ["M-001", "M-002"]),
    "mining":                          ("Mining & Quarrying",    "Economic Diversification", ["M-003"]),
    "transport":                       ("Logistics & Trade",     "Economic Diversification", ["L-001"]),
    "logistics":                       ("Logistics & Trade",     "Economic Diversification", ["L-001", "L-002"]),
    "environment":                     ("Environment & Sustainability", "Green Economy",     ["G-001"]),
    "cleaning":                        ("Facilities Management", "Economic Diversification", ["B-002"]),
    "facilities":                      ("Facilities Management", "Economic Diversification", ["B-002"]),
    "security":                        ("Safety & Security",     "Social Development",       ["S-001"]),
    "printing":                        ("Media & Printing",      "Knowledge Economy",        ["P-003"]),
    "media":                           ("Media & Printing",      "Knowledge Economy",        ["P-003"]),
    "real estate":                     ("Real Estate",           "Economic Diversification", ["R-001"]),
    "energy":                          ("Energy & Utilities",    "Economic Diversification", ["E-001", "E-002"]),
    "oil":                             ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "gas":                             ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "telecom":                         ("ICT & Digital",         "Digital Economy",          ["D-003"]),
    "financial":                       ("Financial Services",    "Knowledge Economy",        ["F-001"]),
    "insurance":                       ("Financial Services",    "Knowledge Economy",        ["F-001"]),
    "legal":                           ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "engineering":                     ("Engineering Services",  "Infrastructure",           ["C-003", "C-004"]),
    "lab":                             ("Research & Testing",    "Knowledge Economy",        ["P-004"]),
    "testing":                         ("Research & Testing",    "Knowledge Economy",        ["P-004"]),
    "maintenance":                     ("Engineering Services",  "Infrastructure",           ["C-003"]),
    "automotive":                      ("Logistics & Trade",     "Economic Diversification", ["L-002"]),
    "furniture":                       ("Manufacturing",         "Economic Diversification", ["M-002"]),
    "textile":                         ("Manufacturing",         "Economic Diversification", ["M-002"]),
    "chemical":                        ("Manufacturing",         "Economic Diversification", ["M-001"]),
    "pharmaceutical":                  ("Healthcare",            "Social Development",       ["H-002"]),
    "aviation":                        ("Ports & Logistics",     "Economic Diversification", ["L-004"]),
    "shipping":                        ("Ports & Logistics",     "Economic Diversification", ["L-003"]),
    "port":                            ("Ports & Logistics",     "Economic Diversification", ["L-003", "L-004"]),
    "road":                            ("Construction",          "Infrastructure",           ["C-001"]),
    "bridge":                          ("Construction",          "Infrastructure",           ["C-001"]),
    "dam":                             ("Construction",          "Infrastructure",           ["C-002"]),
    "railway":                         ("Ports & Logistics",     "Economic Diversification", ["L-005"]),
    "water":                           ("Energy & Utilities",    "Economic Diversification", ["E-003"]),
    "waste":                           ("Environment & Sustainability", "Green Economy",     ["G-001"]),
    "solar":                           ("Energy & Utilities",    "Green Economy",            ["E-002", "G-002"]),
    "renewable":                       ("Energy & Utilities",    "Green Economy",            ["E-002", "G-002"]),
}

DEFAULT_SECTOR  = ("Business Services",     "Economic Diversification", ["B-001"])

# entity_name → sector override (more reliable than category_grade for Supply companies)
ENTITY_MAP = {
    "health":       ("Healthcare",            "Social Development",       ["H-001", "H-002"]),
    "hospital":     ("Healthcare",            "Social Development",       ["H-001"]),
    "medical":      ("Healthcare",            "Social Development",       ["H-001"]),
    "education":    ("Education & Training",  "Knowledge Economy",        ["P-002"]),
    "school":       ("Education & Training",  "Knowledge Economy",        ["P-002"]),
    "university":   ("Education & Training",  "Knowledge Economy",        ["P-002"]),
    "agriculture":  ("Agri & Food",           "Food Security",            ["A-001", "A-002"]),
    "fisheries":    ("Fisheries & Aquaculture","Food Security",           ["A-003"]),
    "tourism":      ("Tourism & Hospitality", "Tourism Development",      ["T-001", "T-002"]),
    "heritage":     ("Tourism & Hospitality", "Tourism Development",      ["T-001"]),
    "environment":  ("Environment & Sustainability","Green Economy",       ["G-001"]),
    "water":        ("Energy & Utilities",    "Economic Diversification", ["E-003"]),
    "electricity":  ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "energy":       ("Energy & Utilities",    "Economic Diversification", ["E-001", "E-002"]),
    "oil":          ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "transport":    ("Logistics & Trade",     "Economic Diversification", ["L-001", "L-005"]),
    "port":         ("Ports & Logistics",     "Economic Diversification", ["L-003", "L-004"]),
    "information":  ("ICT & Digital",         "Digital Economy",          ["D-001", "D-002"]),
    "technology":   ("ICT & Digital",         "Digital Economy",          ["D-001"]),
    "telecom":      ("ICT & Digital",         "Digital Economy",          ["D-003"]),
    "commerce":     ("Logistics & Trade",     "Economic Diversification", ["L-001"]),
    "industry":     ("Manufacturing",         "Economic Diversification", ["M-001"]),
    "housing":      ("Construction",          "Infrastructure",           ["C-001", "C-002"]),
    "municipality": ("Construction",          "Infrastructure",           ["C-001"]),
    "defence":      ("Safety & Security",     "Social Development",       ["S-001"]),
    "police":       ("Safety & Security",     "Social Development",       ["S-001"]),
    "finance":      ("Financial Services",    "Knowledge Economy",        ["F-001"]),
    "manpower":     ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "social":       ("Social Services",       "Social Development",       ["H-003"]),
    "justice":      ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "royal":        ("Government Services",   "Social Development",       ["G-003"]),
    "court":        ("Government Services",   "Social Development",       ["G-003"]),
}

# Company name keywords for secondary enrichment
NAME_MAP = {
    "pharmacy":     ("Healthcare",            "Social Development",       ["H-001", "H-002"]),
    "medical":      ("Healthcare",            "Social Development",       ["H-001"]),
    "healthcare":   ("Healthcare",            "Social Development",       ["H-001"]),
    "hospital":     ("Healthcare",            "Social Development",       ["H-001"]),
    "pharma":       ("Healthcare",            "Social Development",       ["H-002"]),
    "drug":         ("Healthcare",            "Social Development",       ["H-002"]),
    "optical":      ("Healthcare",            "Social Development",       ["H-001"]),
    "biomedical":   ("Healthcare",            "Social Development",       ["H-001"]),
    "software":     ("ICT & Digital",         "Digital Economy",          ["D-001"]),
    "technology":   ("ICT & Digital",         "Digital Economy",          ["D-001"]),
    "computer":     ("ICT & Digital",         "Digital Economy",          ["D-002"]),
    "digital":      ("ICT & Digital",         "Digital Economy",          ["D-001"]),
    "telecom":      ("ICT & Digital",         "Digital Economy",          ["D-003"]),
    "energy":       ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "oil":          ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "petroleum":    ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "solar":        ("Energy & Utilities",    "Green Economy",            ["E-002", "G-002"]),
    "construction": ("Construction",          "Infrastructure",           ["C-001"]),
    "engineering":  ("Engineering Services",  "Infrastructure",           ["C-003"]),
    "contracting":  ("Construction",          "Infrastructure",           ["C-001"]),
    "logistics":    ("Logistics & Trade",     "Economic Diversification", ["L-001", "L-002"]),
    "transport":    ("Logistics & Trade",     "Economic Diversification", ["L-001"]),
    "shipping":     ("Ports & Logistics",     "Economic Diversification", ["L-003"]),
    "travel":       ("Tourism & Hospitality", "Tourism Development",      ["T-001"]),
    "tourism":      ("Tourism & Hospitality", "Tourism Development",      ["T-001"]),
    "hotel":        ("Tourism & Hospitality", "Tourism Development",      ["T-001"]),
    "catering":     ("Tourism & Hospitality", "Tourism Development",      ["T-002"]),
    "food":         ("Agri & Food",           "Food Security",            ["A-001"]),
    "agriculture":  ("Agri & Food",           "Food Security",            ["A-001"]),
    "fishing":      ("Fisheries & Aquaculture","Food Security",           ["A-003"]),
    "fisheries":    ("Fisheries & Aquaculture","Food Security",           ["A-003"]),
    "consulting":   ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "training":     ("Education & Training",  "Knowledge Economy",        ["P-002"]),
    "security":     ("Safety & Security",     "Social Development",       ["S-001"]),
    "cleaning":     ("Facilities Management", "Economic Diversification", ["B-002"]),
    "facilities":   ("Facilities Management", "Economic Diversification", ["B-002"]),
    "printing":     ("Media & Printing",      "Knowledge Economy",        ["P-003"]),
    "media":        ("Media & Printing",      "Knowledge Economy",        ["P-003"]),
    "financial":    ("Financial Services",    "Knowledge Economy",        ["F-001"]),
    "insurance":    ("Financial Services",    "Knowledge Economy",        ["F-001"]),
    "bank":         ("Financial Services",    "Knowledge Economy",        ["F-001"]),
    "manufacturing":("Manufacturing",         "Economic Diversification", ["M-001"]),
    "industrial":   ("Manufacturing",         "Economic Diversification", ["M-001"]),
    "chemical":     ("Manufacturing",         "Economic Diversification", ["M-001"]),
    "mining":       ("Mining & Quarrying",    "Economic Diversification", ["M-003"]),
    "real estate":  ("Real Estate",           "Economic Diversification", ["R-001"]),
    "property":     ("Real Estate",           "Economic Diversification", ["R-001"]),
    "legal":        ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "law":          ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "audit":        ("Professional Services", "Knowledge Economy",        ["P-001"]),
    "environment":  ("Environment & Sustainability","Green Economy",       ["G-001"]),
    "waste":        ("Environment & Sustainability","Green Economy",       ["G-001"]),
    "water":        ("Energy & Utilities",    "Economic Diversification", ["E-003"]),
    "power":        ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
    "electric":     ("Energy & Utilities",    "Economic Diversification", ["E-001"]),
}


def parse_category(raw: str) -> str:
    """Extract base category from 'Category[Grade1,Grade2]' format."""
    if not raw:
        return ""
    return raw.split("[")[0].strip().lower()


def match_entity(entity_name: str) -> tuple | None:
    """Try to match entity_name to a sector."""
    e = entity_name.lower()
    for key, val in ENTITY_MAP.items():
        if key in e:
            return val
    return None


def match_name(company_name: str) -> tuple | None:
    """Try to match company name keywords to a sector."""
    n = company_name.lower()
    for key, val in NAME_MAP.items():
        if key in n:
            return val
    return None


def match_cat(category_lower: str) -> tuple:
    for key, val in CAT_MAP.items():
        if key in category_lower:
            return val
    return DEFAULT_SECTOR


def esc(s: str) -> str:
    return s.replace("'", "''")


# ── Load tenders ──────────────────────────────────────────────

print("Loading tenders...")
raw = []
p = DATA / "tenders_full.json"
with open(p, encoding="utf-8") as f:
    content = f.read().strip()
try:
    data = json.loads(content)
    raw = data if isinstance(data, list) else []
except json.JSONDecodeError:
    for line in content.splitlines():
        line = line.strip()
        if line:
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError:
                pass

print(f"  {len(raw)} tenders loaded")

# ── Build sector votes per company ───────────────────────────

company_votes: dict[str, Counter] = defaultdict(Counter)

for t in raw:
    name = (t.get("company_name") or t.get("awarded_company") or "").strip().upper()
    if not name:
        continue

    # Priority: company name keywords > category_grade > default
    # entity_name intentionally excluded - buyer's sector contaminates vendor classification
    result = match_name(name)

    if result is None:
        cat_raw = t.get("category_grade") or t.get("sector") or ""
        cat_low = parse_category(cat_raw)
        result = match_cat(cat_low) if cat_low else DEFAULT_SECTOR

    sector, pillar, codes = result
    vote_key = f"{sector}|||{pillar}|||{','.join(codes)}"
    company_votes[name][vote_key] += 1

print(f"  Companies with sector data: {len(company_votes)}")

# Preview top 3
for name, ctr in list(company_votes.items())[:3]:
    top = ctr.most_common(1)[0]
    print(f"  {name}: {top}")

# ── Load oman_smes_processed to get CR mapping ────────────────

sme_by_name: dict[str, dict] = {}
sme_by_cr:   dict[str, dict] = {}

processed_path = DATA / "oman_smes_processed.json"
if processed_path.exists():
    with open(processed_path, encoding="utf-8") as f:
        processed = json.load(f)
    for s in processed:
        name = (s.get("company_name") or "").strip().upper()
        cr   = (s.get("commercial_reg_no") or "").strip()
        if name:
            sme_by_name[name] = s
        if cr:
            sme_by_cr[cr] = s
    print(f"  {len(processed)} processed SMEs loaded for CR mapping")

# ── Generate UPDATE SQL ───────────────────────────────────────

updates = []

for company_upper, ctr in company_votes.items():
    top_key, _ = ctr.most_common(1)[0]
    sector, pillar, codes = top_key.split("|||")
    codes_list = codes.split(",") if codes else []
    codes_json = json.dumps(codes_list)
    oia_match  = 1
    oia_count  = len(codes_list)

    sql = (
        f"UPDATE oman_smes SET "
        f"vision2040_sector = '{esc(sector)}', "
        f"vision2040_pillar = '{esc(pillar)}' "
        f"WHERE UPPER(TRIM(company_name)) = '{esc(company_upper)}';"
    )
    updates.append(sql)

out_path = DATA / "updates_sector_from_tenders.sql"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(f"-- Sector enrichment from tender categories - {len(updates)} companies\n")
    f.write("-- Generated by enrich_sectors.py\n\n")
    for sql in updates:
        f.write(sql + "\n")

print(f"\nWrote {len(updates)} UPDATE statements -> {out_path}")
print(f"Load with:")
print(f"  cd worker && wrangler d1 execute oman-sme-intel --file=../data/updates_sector_from_tenders.sql --remote")
