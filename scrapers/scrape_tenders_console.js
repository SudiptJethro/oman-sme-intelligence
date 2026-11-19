// ── Paste into Chrome DevTools Console on:
// https://etendering.tenderboard.gov.om/product/ReportAction?eventFlag=CompletedTendersForPublic
//
// Single-pass: scrapes tender list + fetches Opening Report (OMR values) per tender inline.
// Resumes from localStorage checkpoint if interrupted.

(async function scrapeTenders() {
  const PAGE_DELAY_MS   = 500;
  const DETAIL_DELAY_MS = 300;
  const STORE_KEY       = 'scraper_tenders';
  const OPENING_BASE    = 'https://etendering.tenderboard.gov.om';
  const form            = document.querySelector('form[name="WF_MAIN_FORM"]') || document.querySelector('form');
  const ACTION          = form?.action || 'https://etendering.tenderboard.gov.om/product/CompletedTendersForPublic';

  let checkpoint = {};
  try { checkpoint = JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch(e) {}
  const results   = checkpoint.results  || [];
  const startPage = checkpoint.lastPage ? checkpoint.lastPage + 1 : 1;
  if (startPage > 1) console.log(`[Scraper] Resuming from page ${startPage} (${results.length} rows saved)`);

  function computeSecurityParams(pageNo) {
    // submitPage() computes hashval + encparam via SHA256 then calls form.submit().
    // We intercept form.submit to capture the fully-signed params without navigating.
    return new Promise((resolve) => {
      form.pageNo.value     = String(pageNo);
      form.prevPageNo.value = String(pageNo - 1);
      const orig = HTMLFormElement.prototype.submit;
      HTMLFormElement.prototype.submit = function() {
        HTMLFormElement.prototype.submit = orig;
        resolve(new URLSearchParams(new FormData(this)).toString());
      };
      submitPage(form);
    });
  }

  function getMaxPageFromDOM() {
    // Look for "Page X of N" pattern in pagination cells
    for (const el of document.querySelectorAll('table td, table th, span, td')) {
      const m = el.innerText?.match(/[Pp]age\s+\d+\s+of\s+(\d+)/);
      if (m) return parseInt(m[1]);
    }
    // Fallback: hidMax hidden input
    const hidMax = document.querySelector('input[name="hidMax"]');
    if (hidMax) return parseInt(hidMax.value) || null;
    return null;
  }

  function parseHTML(html) {
    const doc  = new DOMParser().parseFromString(html, 'text/html');
    const rows = [];
    const cellText = el => (el.textContent || '').replace(/\s+/g, ' ').trim();
    for (const table of doc.querySelectorAll('table')) {
      const firstRow = table.querySelector('tr');
      if (!firstRow) continue;
      const headerText = firstRow.textContent || '';
      if (!/tender/i.test(headerText) || !/s\.?\s*no/i.test(headerText)) continue;
      for (const tr of [...table.querySelectorAll('tr')].slice(1)) {
        const tds  = [...tr.querySelectorAll('td')];
        const cols = tds.map(cellText);
        if (cols.length >= 4 && /^\d+$/.test(cols[0])) {

          // Extract strTenderNo + displayFlag from onclick="showOpeningStatus_Report('12345','1')"
          let strTenderNo = '', displayFlag = '0', openingUrl = '';
          for (const td of tds) {
            const el = td.querySelector('[onclick*="showOpeningStatus_Report"]');
            if (el) {
              const m = el.getAttribute('onclick').match(/showOpeningStatus_Report\s*\(\s*'(\d+)'\s*,\s*'([^']*)'/);
              if (m) { strTenderNo = m[1]; displayFlag = m[2]; }
              break;
            }
          }
          if (strTenderNo) {
            openingUrl = `/product/tmsbidopen/TenderOpeningQCRStatusAction.action?callAction=showOpeningStatus_public&strTenderNo=${strTenderNo}&PublicUrl=1`;
          }

          // Full tender title from hidden input value (innerText is truncated)
          const titleInput = tds[1]?.querySelector('input[type="hidden"]');
          const fullTitle  = titleInput?.value || cols[2] || '';

          rows.push({
            sno:                    cols[0],
            tender_no:              cols[1]?.replace(/\s+/g,' ').trim() || '',
            tender_title:           fullTitle,
            entity_name:            cols[3] || '',
            category_grade:         cols[4] || '',
            tender_type_vendor_type:(cols[5] || '').replace(/\s+/g, ' ').trim(),
            awarded_date:           (cols[6] || '').replace(/^(\d{2}-\d{2}-\d{4}) /, '$1, '),
            opening_url:            openingUrl,
            str_tender_no:          strTenderNo,
            display_flag:           displayFlag,
          });
        }
      }
      return rows;
    }
    return rows;
  }

  function parseOpeningReport(html) {
    const doc     = new DOMParser().parseFromString(html, 'text/html');
    let winner    = null;
    const all_bids = [];

    // innerText unreliable in DOMParser docs (no layout) - use textContent
    const cellText = el => (el.textContent || '').replace(/\s+/g, ' ').trim();

    for (const tr of doc.querySelectorAll('table tr')) {
      const tds   = [...tr.querySelectorAll('td')];
      const cells = tds.map(cellText);
      if (cells.length < 4) continue;
      // Skip header row
      if (/company.?name|s\.?\s*no|offer.?type|total/i.test(cells[0] + cells[1])) continue;
      // Data rows: S.No starts with digit (e.g. "1-[1]", "1", "1.")
      if (!/^\d/.test(cells[0])) continue;

      // Winner: ribbon img or 🎖 emoji in company cell
      const companyCell = tds[1];
      const hasRibbon   = companyCell?.innerHTML.includes('🎖') ||
                          companyCell?.innerHTML.includes('ribbon') ||
                          companyCell?.querySelector('img') !== null;

      const company_name       = cells[1].replace(/🎖/g, '').trim();
      const offer_type         = cells[2] || '';
      const total_quoted_value = parseFloat((cells[3] || '').replace(/[^0-9.]/g, '')) || null;
      const status             = cells[4] || '';

      const bid = { company_name, offer_type, total_quoted_value, status, is_winner: hasRibbon };
      all_bids.push(bid);
      if (hasRibbon) winner = bid;
    }

    // Fallback: lowest value = winner
    if (!winner && all_bids.length) {
      winner = all_bids.filter(b => b.total_quoted_value != null)
                       .sort((a, b) => a.total_quoted_value - b.total_quoted_value)[0]
               || all_bids[0];
    }
    return { winner, all_bids };
  }

  async function fetchOpeningReport(row) {
    if (!row.str_tender_no || row.display_flag === '0') return;
    try {
      const basePath = `/product/tmsbidopen/TenderOpeningQCRStatusAction.action?callAction=showOpeningStatus_public&strTenderNo=${row.str_tender_no}&PublicUrl=1`;
      // URL must be signed same way as showOpeningStatus_Report() does it
      const signedPath = typeof getPublicFinalURL !== 'undefined'
        ? getPublicFinalURL(document.forms[0], basePath)
        : typeof createUrlWithoutSecurity !== 'undefined'
        ? createUrlWithoutSecurity(document.forms[0], basePath)
        : basePath;
      const url = signedPath.startsWith('http') ? signedPath : OPENING_BASE + signedPath;
      const html = await fetch(url, { credentials: 'include' }).then(r => r.text());
      if (!html || html.length < 100 || html.includes('<title>Error Page</title>')) return;
      const { winner, all_bids } = parseOpeningReport(html);
      row.awarded_company   = winner?.company_name       || '';
      row.awarded_value_omr = winner?.total_quoted_value || null;
      row.all_bids          = all_bids;
    } catch(e) { /* skip - don't block main scrape */ }
  }

  function getMaxPageFromHTML(html) {
    // Match pagination text: "Page X of 568" - avoid matching record counts like "1-50 of 1950"
    const m = html.match(/[Pp]age\s+\d+\s+of\s+(\d+)/)
           || html.match(/hidMax[^>]*value=["'](\d+)["']/i)
           || html.match(/value=["'](\d+)["'][^>]*name=["']hidMax["']/i);
    return m ? parseInt(m[1]) : null;
  }

  function isExpired(html) {
    return html.includes('logoutForce') ||
           html.includes('unable to access') ||
           /session\s*(has\s*)?(expired|timed?\s*out)/i.test(html);
  }

  async function fetchPage(pageNo) {
    const body = await computeSecurityParams(pageNo);
    const resp  = await fetch(ACTION, {
      method:      'POST',
      headers:     { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
      credentials: 'include',
    });
    return resp.text();
  }

  function saveCheckpoint(lastPage) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ lastPage, results })); } catch(e) {}
  }

  function download() {
    const blob = new Blob([JSON.stringify(results, null, 2)], { type: 'application/json' });
    const a    = document.createElement('a');
    a.href = window.URL.createObjectURL(blob);
    a.download = 'tenders_full.json'; a.click();
    localStorage.removeItem(STORE_KEY);
    console.log(`[Scraper] DONE - ${results.length} tenders → tenders_full.json`);
  }

  let maxPage = getMaxPageFromDOM() || 568;
  console.log(`[Scraper] Starting. Max pages: ${maxPage}. From page: ${startPage}`);

  const testHtml = await fetchPage(startPage);
  console.log(`[Scraper] Test fetch: len=${testHtml.length} | hasTable=${testHtml.includes('<table')} | expired=${isExpired(testHtml)}`);
  if (testHtml.length === 0 || isExpired(testHtml)) {
    console.error('[Scraper] No data - refresh tenders page, wait for it to fully load, then repaste.');
    return;
  }

  for (let p = startPage; p <= maxPage; p++) {
    try {
      const html = p === startPage ? testHtml : await fetchPage(p);

      if (isExpired(html)) {
        console.error(`[Scraper] Session expired at page ${p}. Reload and repaste to resume.`);
        saveCheckpoint(p - 1);
        return;
      }

      const rows = parseHTML(html);
      if (rows.length === 0) {
        console.warn(`[Scraper] Page ${p}: 0 rows - done`);
        break;
      }

      const mp = getMaxPageFromHTML(html);
      if (mp && mp > maxPage) maxPage = mp;

      // Fetch opening report (OMR values) for each tender row
      for (const row of rows) {
        await fetchOpeningReport(row);
        await new Promise(r => setTimeout(r, DETAIL_DELAY_MS));
      }

      results.push(...rows);
      saveCheckpoint(p);

      const withOMR = rows.filter(r => r.awarded_value_omr != null).length;
      if (p % 10 === 0 || p === maxPage) {
        console.log(`[Scraper] Page ${p}/${maxPage} | Total: ${results.length} | OMR hits this page: ${withOMR}/${rows.length}`);
      }

      await new Promise(r => setTimeout(r, PAGE_DELAY_MS));

    } catch(err) {
      console.error(`[Scraper] Error at page ${p}: ${err.message} - retrying in 3s`);
      await new Promise(r => setTimeout(r, 3000));
      p--;
    }
  }

  download();
})();
