-- ============================================================
--  Oman SME Investment Intelligence - Cloudflare D1 Schema
-- ============================================================

-- ── Core SME table ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oman_smes (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,

  -- Identity
  company_name            TEXT NOT NULL,
  company_name_ar         TEXT,
  commercial_reg_no       TEXT UNIQUE,
  cr_expiry_date          TEXT,
  city                    TEXT DEFAULT 'Muscat',
  governorate             TEXT,
  website                 TEXT,
  linkedin_url            TEXT,
  contact_email           TEXT,
  contact_phone           TEXT,

  -- JSRS
  jsrs_certified          INTEGER DEFAULT 0,
  jsrs_cert_status        TEXT CHECK (jsrs_cert_status IN ('Active','Expired','Pending','Unknown')) DEFAULT 'Unknown',
  jsrs_cert_number        TEXT,
  jsrs_cert_expiry        TEXT,
  jsrs_categories         TEXT,      -- JSON array: product/service category codes
  jsrs_classification     TEXT,

  -- OIA Protected List match
  oia_code_match          INTEGER DEFAULT 0,
  oia_matched_codes       TEXT,      -- JSON array: matched OIA codes
  oia_match_count         INTEGER DEFAULT 0,

  -- ICV & Vision 2040
  icv_score               REAL,
  icv_certified           INTEGER DEFAULT 0,
  vision2040_pillar       TEXT,
  vision2040_sector       TEXT,

  -- Tender Board vendor registry fields
  vendor_reg_expiry       TEXT,      -- Reg Expiry Date from Tender Board (YYYY-MM-DD)
  vendor_category         TEXT,      -- Registered Category from Tender Board
  company_type            TEXT,      -- SME / Local / International

  -- Revenue estimation (proxy signals)
  tender_award_total_omr  REAL DEFAULT 0,
  tender_contract_count   INTEGER DEFAULT 0,
  estimated_revenue_band  TEXT CHECK (estimated_revenue_band IN (
                            'Under 1M','1M–5M','5M–20M','Over 20M','Unknown'
                          )) DEFAULT 'Unknown',
  revenue_confidence      TEXT DEFAULT 'Low',  -- High/Medium/Low/tender_proxy

  -- Investment signals
  ai_investment_score     INTEGER CHECK (ai_investment_score BETWEEN 1 AND 10),
  ai_summary              TEXT,
  ai_growth_signals       TEXT,
  ai_risk_flags           TEXT,

  -- Pipeline
  pipeline_stage          TEXT CHECK (pipeline_stage IN (
                            'New','Reviewing','Watchlist','In DD','Pass','Portfolio'
                          )) DEFAULT 'New',
  analyst_notes           TEXT,
  flagged                 INTEGER DEFAULT 0,

  -- Meta
  source                  TEXT NOT NULL,
  enriched                INTEGER DEFAULT 0,
  enriched_at             TEXT,
  created_at              TEXT DEFAULT (datetime('now')),
  updated_at              TEXT DEFAULT (datetime('now'))
);

-- ── OIA Protected Code reference table ───────────────────────
CREATE TABLE IF NOT EXISTS oia_protected_codes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  code             TEXT UNIQUE NOT NULL,
  description      TEXT,
  description_ar   TEXT,
  category         TEXT,
  sector           TEXT,
  vision2040_pillar TEXT,
  created_at       TEXT DEFAULT (datetime('now'))
);

-- ── Tender award records ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS tender_awards (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  company_name    TEXT NOT NULL,
  commercial_reg  TEXT,
  tender_ref      TEXT UNIQUE,
  tender_title    TEXT,
  award_value_omr REAL,
  award_date      TEXT,
  buyer_entity    TEXT,
  sector          TEXT,
  activity_code   TEXT,
  source_url      TEXT,
  created_at      TEXT DEFAULT (datetime('now'))
);

-- ── Scrape run log ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  status      TEXT CHECK (status IN ('running','success','failed')) DEFAULT 'running',
  leads_found INTEGER DEFAULT 0,
  leads_new   INTEGER DEFAULT 0,
  error_msg   TEXT,
  started_at  TEXT DEFAULT (datetime('now')),
  finished_at TEXT
);

-- ── Indexes ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_smes_jsrs        ON oman_smes (jsrs_certified, jsrs_cert_status);
CREATE INDEX IF NOT EXISTS idx_smes_oia         ON oman_smes (oia_code_match, oia_match_count DESC);
CREATE INDEX IF NOT EXISTS idx_smes_revenue     ON oman_smes (estimated_revenue_band);
CREATE INDEX IF NOT EXISTS idx_smes_pipeline    ON oman_smes (pipeline_stage);
CREATE INDEX IF NOT EXISTS idx_smes_score       ON oman_smes (ai_investment_score DESC);
CREATE INDEX IF NOT EXISTS idx_smes_enriched    ON oman_smes (enriched);
CREATE INDEX IF NOT EXISTS idx_tender_company   ON tender_awards (company_name);

-- ── Auto-update timestamp trigger (SQLite syntax) ────────────
CREATE TRIGGER IF NOT EXISTS trg_smes_updated
  AFTER UPDATE ON oman_smes
  FOR EACH ROW
BEGIN
  UPDATE oman_smes SET updated_at = datetime('now') WHERE id = OLD.id;
END;

-- ── View: investment-ready SMEs ───────────────────────────────
-- Scores computed from tender_awards JOIN. No JSRS required (Phase 2).
-- Target: OIA-aligned vendors with 1M-5M OMR tender history, reg active.
CREATE VIEW IF NOT EXISTS investment_ready AS
SELECT
  s.id,
  s.company_name,
  s.company_name_ar,
  s.commercial_reg_no,
  s.governorate,
  s.contact_phone,
  s.vendor_category,
  s.company_type,
  s.oia_code_match,
  s.vendor_reg_expiry,
  'Pending Phase 2'                       AS jsrs_cert_status,
  COUNT(t.id)                             AS award_count,
  ROUND(SUM(COALESCE(t.award_value_omr, 0)), 0) AS total_omr,
  MAX(t.award_date)                       AS last_award,
  ROUND(
    -- Turnover in 1M-5M sweet spot = full 35 pts; outside range = partial
    CASE WHEN SUM(COALESCE(t.award_value_omr,0)) BETWEEN 1000000 AND 5000000
         THEN 35.0
         ELSE MIN(SUM(COALESCE(t.award_value_omr,0)) / 5000000.0 * 35.0, 30.0)
    END
    + CASE WHEN s.oia_code_match = 1 THEN 30.0 ELSE 0.0 END
    + CASE WHEN MAX(t.award_date) > date('now','-24 months') THEN 20.0 ELSE 0.0 END
    + MIN(COUNT(t.id) * 3.0, 15.0)
  , 0)                                    AS score
FROM oman_smes s
JOIN tender_awards t
  ON UPPER(TRIM(s.company_name)) = UPPER(TRIM(t.company_name))
WHERE (s.vendor_reg_expiry IS NULL OR s.vendor_reg_expiry > date('now'))
GROUP BY s.id
HAVING total_omr BETWEEN 1000000 AND 5000000
   AND award_count >= 2
ORDER BY score DESC, total_omr DESC;
