/**
 * Oman SME Investment Intelligence - Cloudflare Worker
 * ──────────────────────────────────────────────────────
 * Every route except the health check requires
 *   Authorization: Bearer <API_TOKEN>
 * (set with `wrangler secret put API_TOKEN`). Without API_TOKEN the API refuses
 * all data requests, so a fresh deployment is never open to the public.
 *
 * Routes:
 *   GET  /              Health check
 *   GET  /leads         List SMEs (filterable)
 *   POST /enrich/:id    Enrich single SME with AI
 *   POST /enrich/batch  Batch enrich up to 30 SMEs
 *   GET  /stats         Dashboard KPIs
 *   GET  /export        CSV export of investment_ready view
 *   POST /browse        CF Browser Rendering proxy (auth required)
 *
 * DB binding:      env.DB      (Cloudflare D1)
 * Browser binding: env.BROWSER (Cloudflare Browser Rendering)
 */
import puppeteer from "@cloudflare/puppeteer";

const CORS = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });

// ── Router ──────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url  = new URL(request.url);
    const path = url.pathname;

    // Fail closed: data routes need a configured API_TOKEN and a matching header.
    // /browse keeps its own X-Browse-Token check.
    if (path !== "/" && path !== "/browse") {
      if (!env.API_TOKEN) return json({ error: "API_TOKEN is not configured on this Worker" }, 503);
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.API_TOKEN}`) return json({ error: "Unauthorized" }, 401);
    }

    if (path === "/")                 return json({ status: "Oman SME Intel Worker running", version: "1.0" });
    if (path === "/leads")            return getLeads(url, env);
    if (path === "/stats")            return getStats(env);
    if (path === "/export")           return exportCSV(env);
    if (path.startsWith("/enrich/"))  return handleEnrich(path, request, env);
    if (path === "/pipeline")         return updatePipeline(request, env);
    if (path === "/tenders")          return getTenders(url, env);
    if (path === "/browse")           return browsePage(request, env);

    return json({ error: "Not found" }, 404);
  }
};

// ── GET /leads ──────────────────────────────────────────────────

async function getLeads(url, env) {
  const params = url.searchParams;

  let query = `
    SELECT
      id, company_name, commercial_reg_no, city, governorate,
      vendor_category, company_type, vendor_reg_expiry,
      jsrs_certified, jsrs_cert_status, jsrs_categories,
      oia_code_match, oia_match_count, oia_matched_codes,
      icv_certified, icv_score,
      vision2040_pillar, vision2040_sector,
      estimated_revenue_band, revenue_confidence,
      tender_award_total_omr, tender_contract_count,
      ai_investment_score, ai_summary, ai_growth_signals, ai_risk_flags,
      pipeline_stage, flagged, analyst_notes,
      website, linkedin_url, contact_email, contact_phone,
      source, enriched, created_at
    FROM oman_smes
    WHERE 1=1
  `;

  const filters = [];

  if (params.get("jsrs_active") === "true")
    filters.push(`jsrs_cert_status = 'Active'`);

  if (params.get("oia_match") === "true")
    filters.push(`oia_code_match = 1`);

  if (params.get("revenue_band")) {
    const band = params.get("revenue_band");
    filters.push(`estimated_revenue_band = '${band.replace(/'/g, "''")}'`);
  }

  // Numeric OMR range filter (preferred over revenue_band string for reliability)
  if (params.get("min_omr"))
    filters.push(`tender_award_total_omr >= ${parseFloat(params.get("min_omr"))}`);
  if (params.get("max_omr"))
    filters.push(`tender_award_total_omr <= ${parseFloat(params.get("max_omr"))}`);

  if (params.get("sector")) {
    const sector = params.get("sector");
    filters.push(`vision2040_sector LIKE '%${sector.replace(/'/g, "''")}%'`);
  }

  if (params.get("pipeline")) {
    const stage = params.get("pipeline");
    filters.push(`pipeline_stage = '${stage.replace(/'/g, "''")}'`);
  }

  if (params.get("min_score")) {
    filters.push(`ai_investment_score >= ${parseInt(params.get("min_score"))}`);
  }

  if (params.get("enriched") === "true")
    filters.push(`enriched = 1`);

  if (params.get("flagged") === "true")
    filters.push(`flagged = 1`);

  if (params.get("icv_certified") === "true")
    filters.push(`icv_certified = 1`);

  if (filters.length) query += " AND " + filters.join(" AND ");

  query += " ORDER BY tender_award_total_omr DESC, tender_contract_count DESC, oia_match_count DESC";

  const limit = Math.min(parseInt(params.get("limit") || "100"), 500);
  query += ` LIMIT ${limit}`;

  const rows = await d1Query(query, env);
  // Parse JSON-stringified arrays stored in D1
  const parsed = rows.map(r => ({
    ...r,
    oia_matched_codes: r.oia_matched_codes ? JSON.parse(r.oia_matched_codes) : [],
  }));
  return json(parsed);
}

// ── GET /tenders ───────────────────────────────────────────────

async function getTenders(url, env) {
  const params = url.searchParams;
  const cr     = params.get("cr");
  const name   = params.get("company_name");

  if (!name) return json({ error: "Provide company_name" }, 400);

  const safeName = name.replace(/'/g, "''");
  const safeCr   = cr ? cr.replace(/'/g, "''") : null;

  // CR column is NULL for most tender_awards rows - always match by name,
  // and also by CR when both tables have it populated.
  const where = safeCr
    ? `(LOWER(company_name) = LOWER('${safeName}') OR commercial_reg = '${safeCr}')`
    : `LOWER(company_name) = LOWER('${safeName}')`;

  const query = `
    SELECT tender_ref, tender_title, award_value_omr, award_date, buyer_entity, sector, source_url
    FROM tender_awards
    WHERE ${where}
    ORDER BY award_date DESC, award_value_omr DESC
    LIMIT 50
  `;
  const rows = await d1Query(query, env);
  return json(rows);
}

// ── GET /stats ─────────────────────────────────────────────────

async function getStats(env) {
  const queries = {
    total:           `SELECT COUNT(*) AS n FROM oman_smes`,
    jsrs_active:     `SELECT COUNT(*) AS n FROM oman_smes WHERE jsrs_cert_status = 'Active'`,
    oia_match:       `SELECT COUNT(*) AS n FROM oman_smes WHERE oia_code_match = 1`,
    target_band:     `SELECT COUNT(*) AS n FROM oman_smes WHERE estimated_revenue_band = '1M–5M'`,
    investment_ready:`SELECT COUNT(*) AS n FROM investment_ready`,
    enriched:        `SELECT COUNT(*) AS n FROM oman_smes WHERE enriched = 1`,
    by_sector:       `SELECT vision2040_sector, COUNT(*) AS n FROM oman_smes GROUP BY vision2040_sector ORDER BY n DESC LIMIT 8`,
    by_pipeline:     `SELECT pipeline_stage, COUNT(*) AS n FROM oman_smes GROUP BY pipeline_stage`,
    by_revenue:      `SELECT estimated_revenue_band, COUNT(*) AS n FROM oman_smes GROUP BY estimated_revenue_band`,
    avg_score:       `SELECT ROUND(AVG(ai_investment_score), 1) AS n FROM oman_smes WHERE ai_investment_score IS NOT NULL`,
    recent_runs:     `SELECT source, status, leads_found, leads_new, started_at FROM scrape_runs ORDER BY started_at DESC LIMIT 10`,
  };

  const results = {};
  for (const [key, sql] of Object.entries(queries)) {
    try {
      results[key] = await d1Query(sql, env);
    } catch (e) {
      results[key] = null;
    }
  }
  return json(results);
}

// ── POST /enrich/:id  &  POST /enrich/batch ────────────────────

async function handleEnrich(path, request, env) {
  if (path === "/enrich/batch") return enrichBatch(env);

  const id = parseInt(path.split("/enrich/")[1]);
  if (!id) return json({ error: "Invalid ID" }, 400);
  return enrichSingle(id, env);
}

async function enrichSingle(id, env) {
  const rows = await d1Query(`SELECT * FROM oman_smes WHERE id = ?`, env, [id]);
  if (!rows.length) return json({ error: "Not found" }, 404);

  const sme    = rows[0];
  const result = await runAIEnrichment(sme, env);
  await saveEnrichment(id, result, env);

  return json({ success: true, id, ...result });
}

async function enrichBatch(env) {
  const rows = await d1Query(
    `SELECT * FROM oman_smes WHERE enriched = 0 ORDER BY created_at DESC LIMIT 30`, env
  );

  let success = 0, failed = 0;
  for (const sme of rows) {
    try {
      const result = await runAIEnrichment(sme, env);
      await saveEnrichment(sme.id, result, env);
      success++;
    } catch (e) {
      failed++;
    }
  }
  return json({ success, failed, total: rows.length });
}

async function saveEnrichment(id, result, env) {
  await d1Run(
    `UPDATE oman_smes
     SET ai_investment_score = ?, ai_summary = ?, ai_growth_signals = ?, ai_risk_flags = ?,
         estimated_revenue_band = ?, vision2040_pillar = ?, vision2040_sector = ?,
         enriched = 1, enriched_at = datetime('now'), updated_at = datetime('now')
     WHERE id = ?`,
    env,
    [
      result.ai_investment_score ?? null,
      result.ai_summary ?? null,
      result.ai_growth_signals ?? null,
      result.ai_risk_flags ?? null,
      result.estimated_revenue_band ?? null,
      result.vision2040_pillar ?? null,
      result.vision2040_sector ?? null,
      id,
    ],
  );
}

// ── Workers AI Investment Enrichment ───────────────────────────

async function runAIEnrichment(sme, env) {
  const jsrsCategories  = JSON.parse(sme.jsrs_categories  || "[]");
  const oiaMatchedCodes = JSON.parse(sme.oia_matched_codes || "[]");

  const prompt = `
You are an investment analyst specialising in Oman SME acquisitions and growth equity.

Analyse this Oman SME and return a JSON object with exactly these fields:
{
  "ai_investment_score": <integer 1-10, where 10 = highest investment potential>,
  "ai_summary": "<2-sentence company summary>",
  "ai_growth_signals": "<key positive signals for investment, comma-separated>",
  "ai_risk_flags": "<key risks or red flags, comma-separated>",
  "estimated_revenue_band": "<one of: Under 1M | 1M–5M | 5M–20M | Over 20M | Unknown>",
  "vision2040_pillar": "<most relevant Vision 2040 pillar>",
  "vision2040_sector": "<specific sector tag>"
}

Scoring guide:
- 8-10: JSRS Active + OIA code match + 1M-5M band + strong ICV alignment + Vision 2040 sector
- 6-7:  Meets most criteria, minor gaps
- 4-5:  Partial match - worth monitoring
- 1-3:  Weak match - limited investment signal

Company data:
Name: ${sme.company_name}
City: ${sme.city || "Oman"}
Sector: ${sme.vision2040_sector || "Unknown"}
JSRS Status: ${sme.jsrs_cert_status || "Unknown"}
JSRS Categories: ${jsrsCategories.join(", ") || "None"}
OIA Code Match: ${sme.oia_code_match ? `YES (${sme.oia_match_count} codes matched)` : "No"}
Matched OIA Codes: ${oiaMatchedCodes.join(", ") || "None"}
ICV Certified: ${sme.icv_certified ? "Yes" : "Unknown"}
ICV Score: ${sme.icv_score || "Unknown"}
Tender Award Total (OMR): ${sme.tender_award_total_omr || 0}
Tender Contract Count: ${sme.tender_contract_count || 0}
Estimated Revenue Band: ${sme.estimated_revenue_band || "Unknown"}

Return ONLY valid JSON, no markdown, no explanation.
`.trim();

  try {
    const response = await env.AI.run("@cf/meta/llama-3.1-8b-instruct", {
      messages: [{ role: "user", content: prompt }],
      max_tokens: 500,
    });

    const text = response?.response || "";
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (!jsonMatch) throw new Error("No JSON in AI response");

    const parsed = JSON.parse(jsonMatch[0]);

    if (parsed.ai_investment_score) {
      parsed.ai_investment_score = Math.max(1, Math.min(10, parseInt(parsed.ai_investment_score)));
    }

    return parsed;
  } catch (e) {
    return {
      ai_investment_score: null,
      ai_summary: "Enrichment pending",
      ai_growth_signals: null,
      ai_risk_flags: null,
    };
  }
}

// ── POST /pipeline - update pipeline stage ─────────────────────

async function updatePipeline(request, env) {
  const body = await request.json();
  const { id, pipeline_stage, analyst_notes, flagged } = body;
  if (!id) return json({ error: "id required" }, 400);

  const sets = [];
  const vals = [];
  if (pipeline_stage !== undefined)  { sets.push("pipeline_stage = ?");  vals.push(pipeline_stage); }
  if (analyst_notes  !== undefined)  { sets.push("analyst_notes = ?");   vals.push(analyst_notes); }
  if (flagged        !== undefined)  { sets.push("flagged = ?");          vals.push(flagged ? 1 : 0); }
  if (!sets.length) return json({ error: "Nothing to update" }, 400);

  sets.push("updated_at = datetime('now')");
  vals.push(id);

  await d1Run(`UPDATE oman_smes SET ${sets.join(", ")} WHERE id = ?`, env, vals);
  return json({ success: true });
}

// ── GET /export - CSV ──────────────────────────────────────────

// OIA Mandatory List v4.0 (Dec 2025) - 34 segments only (S-01..S-15, P-01..P-19)
const OIA_DESC = {
  "S-01":"General Administration","S-02":"Maintenance Services & Spare Parts",
  "S-03":"Legal","S-04":"Transportation & Distribution","S-05":"End-to-End Logistics",
  "S-06":"Non-Destructive Testing","S-07":"Waste Management",
  "S-08":"Human Capital and Consultancy","S-09":"Value-added Services","S-10":"Media",
  "S-11":"Architect","S-12":"Communication","S-13":"Training & Development Programs",
  "S-14":"Warehouse","S-15":"Aviation Services",
  "P-01":"Office Supplies & Uniform","P-02":"Medicine","P-03":"Cables",
  "P-04":"Sanitary Ware","P-05":"Buses","P-06":"Industrial Paint",
  "P-07":"General Consumables","P-08":"Pipes and Fittings",
  "P-09":"Cleaning Products & Chemicals","P-10":"IT Equipment","P-11":"Wooden Pallets",
  "P-12":"Industrial Chemicals & Gases","P-13":"Animal Feed & Veterinary Products",
  "P-14":"Gifts","P-15":"HSE Equipment","P-16":"Food & Beverages",
  "P-17":"Construction Material & Services","P-18":"Agriculture and Fertilizers",
  "P-19":"Transformers",
};

async function exportCSV(env) {
  const rows = await d1Query(`
    SELECT
      company_name, commercial_reg_no, governorate,
      contact_phone, contact_email, website,
      vendor_category, company_type, vendor_reg_expiry,
      oia_code_match, oia_matched_codes,
      icv_certified,
      tender_award_total_omr, tender_contract_count,
      estimated_revenue_band, revenue_confidence,
      jsrs_cert_status, jsrs_categories,
      ai_investment_score,
      ai_summary, ai_growth_signals, ai_risk_flags,
      pipeline_stage, flagged, analyst_notes,
      source, created_at
    FROM oman_smes
    ORDER BY tender_award_total_omr DESC, tender_contract_count DESC
    LIMIT 5000
  `, env);

  if (!rows.length) return new Response("No data", { headers: CORS });

  const cell = (v, type) => {
    let s;
    if (type === "bool")  s = v ? "Yes" : "No";
    else if (type === "omr") s = v ? Number(v).toLocaleString("en-US", {maximumFractionDigits:0}) : "0";
    else if (type === "oia_codes") {
      const codes = (() => { try { return JSON.parse(v || "[]"); } catch { return []; } })();
      s = codes.filter(c => /^[SP]-\d{2}$/.test(c)).join(", ");
    }
    else if (type === "oia_desc") {
      const codes = (() => { try { return JSON.parse(v || "[]"); } catch { return []; } })();
      s = codes.filter(c => /^[SP]-\d{2}$/.test(c)).map(c => OIA_DESC[c] || c).join(" | ");
    }
    else if (type === "json") {
      try { const arr = JSON.parse(v || "[]"); s = Array.isArray(arr) ? arr.join(", ") : String(v || ""); }
      catch { s = String(v || ""); }
    }
    else s = v === null || v === undefined ? "" : String(v);
    s = s.replace(/\r?\n|\r/g, " ");
    return `"${s.replace(/"/g, '""')}"`;
  };

  // [db_key, column_header, type?]
  const COLS = [
    // Identity
    ["company_name",          "Company Name"],
    ["commercial_reg_no",     "CR Number"],
    ["governorate",           "Governorate"],
    ["contact_phone",         "Phone"],
    ["contact_email",         "Email"],
    ["website",               "Website"],
    // Registration
    ["vendor_category",       "Tender Board Category"],
    ["company_type",          "Company Type (SME/Local)"],
    ["vendor_reg_expiry",     "Vendor Reg. Expiry"],
    // OIA / ICV
    ["oia_code_match",        "OIA Code Match",              "bool"],
    ["oia_matched_codes",     "OIA Matched Codes",           "oia_codes"],
    ["oia_matched_codes",     "OIA Segment Descriptions",    "oia_desc"],
    ["icv_certified",         "ICV Certified (OIA v4.0)",    "bool"],
    // Revenue
    ["tender_award_total_omr","Total Gov Contracts Won (OMR)","omr"],
    ["tender_contract_count", "No. of Gov Contracts"],
    ["estimated_revenue_band","Estimated Revenue Band"],
    ["revenue_confidence",    "Revenue Confidence"],
    // JSRS
    ["jsrs_cert_status",      "JSRS Status"],
    ["jsrs_categories",       "JSRS Categories",             "json"],
    // AI
    ["ai_investment_score",   "AI Investment Score (0–100)"],
    ["ai_summary",            "AI Company Summary"],
    ["ai_growth_signals",     "Growth Signals"],
    ["ai_risk_flags",         "Risk Flags"],
    // Pipeline
    ["pipeline_stage",        "Pipeline Stage"],
    ["flagged",               "Flagged",                     "bool"],
    ["analyst_notes",         "Analyst Notes"],
    // Meta
    ["source",                "Data Source"],
    ["created_at",            "Record Added"],
  ];

  const BOM = "﻿";
  const headerRow = COLS.map(([, label]) => `"${label}"`).join(",");
  const dataRows  = rows.map(r =>
    COLS.map(([key,, type]) => cell(r[key], type)).join(",")
  );

  const csv = BOM + [headerRow, ...dataRows].join("\r\n");

  return new Response(csv, {
    headers: {
      ...CORS,
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="oman-sme-intel-${new Date().toISOString().slice(0,10)}.csv"`,
    }
  });
}

// ── POST /browse - CF Browser Rendering proxy ──────────────────
// Renders JavaScript-heavy pages in headless Chrome for the Python scrapers.
// Requires the X-Browse-Token header for auth.

async function browsePage(request, env) {
  // Auth check
  const token = request.headers.get("X-Browse-Token");
  if (!token || token !== env.BROWSE_TOKEN) {
    return json({ error: "Unauthorized" }, 401);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }

  const { url, wait_for, timeout_ms = 30000 } = body;
  if (!url || !url.startsWith("http")) {
    return json({ error: "url required and must be http(s)" }, 400);
  }

  let browser;
  try {
    browser = await puppeteer.launch(env.BROWSER);
    const page = await browser.newPage();

    // Mimic a real browser
    await page.setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
      "AppleWebKit/537.36 (KHTML, like Gecko) " +
      "Chrome/124.0.0.0 Safari/537.36"
    );
    await page.setExtraHTTPHeaders({
      "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    });

    await page.goto(url, { waitUntil: "domcontentloaded", timeout: timeout_ms });

    if (wait_for) {
      await page.waitForSelector(wait_for, { timeout: 10000 }).catch(() => {});
    } else {
      await new Promise(r => setTimeout(r, 2000));
    }

    const html = await page.content();
    await browser.close();
    return json({ html, url });

  } catch (e) {
    if (browser) await browser.close().catch(() => {});
    return json({ error: String(e), url }, 500);
  }
}

// ── D1 helpers ─────────────────────────────────────────────────

async function d1Query(sql, env, params = []) {
  const stmt   = params.length ? env.DB.prepare(sql).bind(...params) : env.DB.prepare(sql);
  const result = await stmt.all();
  return result.results;
}

async function d1Run(sql, env, params = []) {
  const stmt = params.length ? env.DB.prepare(sql).bind(...params) : env.DB.prepare(sql);
  return stmt.run();
}
