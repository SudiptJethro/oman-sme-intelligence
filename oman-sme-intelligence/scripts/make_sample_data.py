"""
Generate the synthetic dataset used by the dashboard's demo mode (docs/sample-data.js).

Every company, registration number and tender in the output is invented. The
fields follow the D1 schema in schema.sql, and the revenue band, confidence and
investment-ready score are derived with the same rules as process_data.py and
the `investment_ready` view, so the demo behaves like the real pipeline.

Run:  python scripts/make_sample_data.py
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 2040
TODAY = date(2026, 11, 19)
OUT = Path(__file__).resolve().parents[1] / "docs" / "sample-data.js"

# Typical contract size by Tender Board category (same table as process_data.py)
CATEGORIES = {
    "Construction and Maintenance": 250_000,
    "Electromechanical and Telecommunications Contracting and Maintenance": 300_000,
    "Information Technology Services": 150_000,
    "Consulting Offices": 100_000,
    "Supply": 80_000,
    "Services": 60_000,
}
SECTORS = {
    "Construction": "Economy & Development",
    "Healthcare": "People & Society",
    "ICT & Digital": "Economy & Development",
    "Logistics & Trade": "Economy & Development",
    "Engineering Services": "Economy & Development",
    "Professional Services": "Governance & Institutional Performance",
    "Business Services": "Economy & Development",
}
SECTOR_CATEGORY = {
    "Construction": "Construction and Maintenance",
    "Healthcare": "Supply",
    "ICT & Digital": "Information Technology Services",
    "Logistics & Trade": "Services",
    "Engineering Services": "Electromechanical and Telecommunications Contracting and Maintenance",
    "Professional Services": "Consulting Offices",
    "Business Services": "Services",
}
OIA_CODES = {
    "Construction": ["C-004", "C-011"], "Healthcare": ["H-001", "H-002"],
    "ICT & Digital": ["I-003"], "Logistics & Trade": ["L-001", "L-006"],
    "Engineering Services": ["E-002"], "Professional Services": ["P-005"],
    "Business Services": ["B-007"],
}
CITIES = [("Muscat", "Muscat"), ("Sohar", "North Al Batinah"), ("Salalah", "Dhofar"),
          ("Nizwa", "Ad Dakhiliyah"), ("Sur", "South Ash Sharqiyah"), ("Ibri", "Ad Dhahirah")]
WORDS_A = ["Harbor", "Summit", "Crescent", "Granite", "Cedar", "Meridian", "Beacon", "Atlas",
           "Silverline", "Northgate", "Bluewave", "Ironwood", "Sandstone", "Horizon", "Keystone"]
WORDS_B = {"Construction": "Builders", "Healthcare": "Medical Supplies", "ICT & Digital": "Digital Systems",
           "Logistics & Trade": "Logistics", "Engineering Services": "Engineering",
           "Professional Services": "Consultants", "Business Services": "Facility Services"}
BUYERS = ["Government buyer (health)", "Government buyer (education)", "Government buyer (transport)",
          "Municipal buyer", "State-owned enterprise", "Government buyer (housing)"]
STAGES = ["New"] * 40 + ["Reviewing"] * 6 + ["Watchlist"] * 5 + ["In DD"] * 2 + ["Pass"] * 3


def revenue_band(omr: float) -> str:
    if omr >= 20_000_000: return "Over 20M"
    if omr >= 5_000_000:  return "5M–20M"
    if omr >= 1_000_000:  return "1M–5M"
    if omr > 0:           return "Under 1M"
    return "Unknown"


def confidence(n: int) -> str:
    return "High" if n >= 5 else "Medium" if n >= 2 else "Low"


def investment_ready_score(total: float, n: int, oia: int, last_award: str | None,
                           reg_expiry: str | None, today: date = TODAY) -> float | None:
    """Python mirror of the `investment_ready` view in schema.sql."""
    if reg_expiry and reg_expiry <= today.isoformat():
        return None
    if not (1_000_000 <= total <= 5_000_000 and n >= 2):
        return None
    cutoff = date(today.year - 2, today.month, min(today.day, 28)).isoformat()
    score = 35.0 if 1_000_000 <= total <= 5_000_000 else min(total / 5_000_000 * 35, 30)
    score += 30.0 if oia else 0.0
    score += 20.0 if last_award and last_award > cutoff else 0.0
    score += min(n * 3.0, 15.0)
    return round(score)


def main() -> None:
    rng = random.Random(SEED)
    smes, tenders, used = [], [], set()
    ref = 1000
    for i in range(1, 57):
        sector = rng.choice(list(SECTORS))
        while True:
            name = f"Demo {rng.choice(WORDS_A)} {WORDS_B[sector]} LLC"
            if name not in used:
                used.add(name); break
        category = SECTOR_CATEGORY[sector]
        city, gov = rng.choice(CITIES)
        n = rng.choices([0, 1, 2, 3, 4, 5, 6, 8, 11], weights=[10, 12, 12, 10, 8, 7, 5, 3, 2])[0]
        awards = []
        for _ in range(n):
            ref += 1
            value = round(CATEGORIES[category] * rng.lognormvariate(0.6, 0.8), -2)
            awarded = TODAY - timedelta(days=rng.randint(30, 7 * 365))
            awards.append({
                "company_name": name, "commercial_reg": f"DEMO-{i:04d}",
                "tender_ref": f"DEMO/TB/{ref}", "tender_title": f"Sample {category.split(' and ')[0].lower()} contract {ref}",
                "award_value_omr": value, "award_date": awarded.isoformat(),
                "buyer_entity": rng.choice(BUYERS), "sector": sector, "source_url": None,
            })
        tenders += awards
        total = round(sum(a["award_value_omr"] for a in awards), 0)
        last = max((a["award_date"] for a in awards), default=None)
        oia = 1 if rng.random() < 0.4 else 0
        codes = OIA_CODES[sector][: rng.randint(1, len(OIA_CODES[sector]))] if oia else []
        expiry = (TODAY + timedelta(days=rng.randint(-200, 1400))).isoformat()
        enriched = 1 if n >= 3 and rng.random() < 0.6 else 0
        ai = None
        if enriched:
            ai = max(1, min(10, round(3 + n * 0.5 + oia * 2 + rng.uniform(-1.5, 1.5))))
        smes.append({
            "id": i, "company_name": name, "commercial_reg_no": f"DEMO-{i:04d}",
            "city": city, "governorate": gov, "vendor_category": category, "company_type": "SME",
            "vendor_reg_expiry": expiry, "jsrs_certified": 0, "jsrs_cert_status": "Unknown",
            "jsrs_categories": None, "oia_code_match": oia, "oia_match_count": len(codes),
            "oia_matched_codes": codes, "icv_certified": 0, "icv_score": None,
            "vision2040_pillar": SECTORS[sector], "vision2040_sector": sector,
            "estimated_revenue_band": revenue_band(total), "revenue_confidence": confidence(n),
            "tender_award_total_omr": total, "tender_contract_count": n,
            "ai_investment_score": ai,
            "ai_summary": ("Synthetic example: government-facing supplier with a repeat contract record "
                           f"in {sector.lower()}.") if enriched else None,
            "ai_growth_signals": "Repeat awards across several buyers (synthetic)" if enriched else None,
            "ai_risk_flags": "Revenue inferred from public contracts only (synthetic)" if enriched else None,
            "pipeline_stage": rng.choice(STAGES), "flagged": 1 if rng.random() < 0.08 else 0,
            "analyst_notes": "", "website": None, "linkedin_url": None,
            "contact_email": None, "contact_phone": None,
            "source": "synthetic_demo", "enriched": enriched,
            "created_at": (TODAY - timedelta(days=rng.randint(5, 200))).isoformat() + " 05:00:00",
            "investment_ready_score": investment_ready_score(total, n, oia, last, expiry),
        })
    runs = [
        {"source": s, "status": "success", "leads_found": rng.randint(5, 60), "leads_new": rng.randint(0, 8),
         "started_at": (TODAY - timedelta(days=d)).isoformat() + " 01:00:00"}
        for d, s in [(3, "tender_awards"), (3, "tender_vendor_registry"), (3, "oia_mandatory_list"),
                     (7, "riyada"), (7, "tender_board")]
    ]
    data = {"note": "Synthetic demo data. Every company and contract here is invented.",
            "generated_for": TODAY.isoformat(), "smes": smes, "tenders": tenders, "runs": runs}
    OUT.write_text("// Generated by scripts/make_sample_data.py. Synthetic data only.\n"
                   f"window.SAMPLE_DATA = {json.dumps(data, ensure_ascii=False, indent=1)};\n",
                   encoding="utf-8")
    ready = sum(1 for s in smes if s["investment_ready_score"] is not None)
    print(f"Wrote {OUT}: {len(smes)} companies, {len(tenders)} contracts, {ready} investment-ready")


if __name__ == "__main__":
    main()
