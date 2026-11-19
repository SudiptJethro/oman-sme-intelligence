"""
Loads schema.sql into SQLite (Cloudflare D1 is SQLite), fills it with the synthetic
demo data, and checks that the `investment_ready` view scores companies exactly as
the methodology describes.
"""
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from make_sample_data import confidence, investment_ready_score, revenue_band  # noqa: E402


def load_sample():
    text = (ROOT / "docs" / "sample-data.js").read_text(encoding="utf-8")
    return json.loads(text[text.index("=") + 1: text.rstrip().rindex(";")])


SMES_COLS = ["id", "company_name", "commercial_reg_no", "city", "governorate", "vendor_category",
             "company_type", "vendor_reg_expiry", "jsrs_certified", "jsrs_cert_status",
             "oia_code_match", "oia_match_count", "oia_matched_codes", "vision2040_pillar",
             "vision2040_sector", "estimated_revenue_band", "revenue_confidence",
             "tender_award_total_omr", "tender_contract_count", "ai_investment_score",
             "pipeline_stage", "flagged", "source", "enriched"]
TENDER_COLS = ["company_name", "commercial_reg", "tender_ref", "tender_title", "award_value_omr",
               "award_date", "buyer_entity", "sector"]


@pytest.fixture(scope="module")
def db():
    data = load_sample()
    con = sqlite3.connect(":memory:")
    con.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    for s in data["smes"]:
        row = {**s, "oia_matched_codes": json.dumps(s["oia_matched_codes"])}
        con.execute(f"INSERT INTO oman_smes ({','.join(SMES_COLS)}) VALUES ({','.join('?' * len(SMES_COLS))})",
                    [row[c] for c in SMES_COLS])
    for t in data["tenders"]:
        con.execute(f"INSERT INTO tender_awards ({','.join(TENDER_COLS)}) VALUES ({','.join('?' * len(TENDER_COLS))})",
                    [t[c] for c in TENDER_COLS])
    con.commit()
    return con, data


def test_schema_accepts_all_sample_rows(db):
    con, data = db
    assert con.execute("SELECT COUNT(*) FROM oman_smes").fetchone()[0] == len(data["smes"])
    assert con.execute("SELECT COUNT(*) FROM tender_awards").fetchone()[0] == len(data["tenders"])


def test_investment_ready_view_matches_documented_scoring(db):
    con, data = db
    today = datetime.now(timezone.utc).date()
    view = {r[0]: r[1] for r in con.execute("SELECT company_name, score FROM investment_ready")}
    expected = {}
    for s in data["smes"]:
        awards = [t for t in data["tenders"] if t["company_name"] == s["company_name"]]
        score = investment_ready_score(
            sum(t["award_value_omr"] for t in awards), len(awards), s["oia_code_match"],
            max((t["award_date"] for t in awards), default=None), s["vendor_reg_expiry"], today)
        if score is not None:
            expected[s["company_name"]] = score
    assert view == pytest.approx(expected)
    assert len(view) > 0


def test_view_only_admits_target_band_with_two_or_more_contracts(db):
    con, _ = db
    rows = con.execute("SELECT total_omr, award_count, score FROM investment_ready").fetchall()
    for total, count, score in rows:
        assert 1_000_000 <= total <= 5_000_000
        assert count >= 2
        assert 0 <= score <= 100


def test_expired_registrations_are_excluded(db):
    con, data = db
    today = date.today().isoformat()
    expired = {s["company_name"] for s in data["smes"] if s["vendor_reg_expiry"] <= today}
    in_view = {r[0] for r in con.execute("SELECT company_name FROM investment_ready")}
    assert expired and not (expired & in_view)


@pytest.mark.parametrize("omr,band", [(0, "Unknown"), (999_999, "Under 1M"), (1_000_000, "1M–5M"),
                                      (4_999_999, "1M–5M"), (5_000_000, "5M–20M"), (25_000_000, "Over 20M")])
def test_revenue_bands(omr, band):
    assert revenue_band(omr) == band


@pytest.mark.parametrize("n,level", [(1, "Low"), (2, "Medium"), (4, "Medium"), (5, "High")])
def test_confidence_levels(n, level):
    assert confidence(n) == level


def test_sample_data_contains_no_contact_details():
    for s in load_sample()["smes"]:
        assert s["company_name"].startswith("Demo ")
        assert s["contact_phone"] is None and s["contact_email"] is None and s["website"] is None
