// ── Paste into Chrome DevTools Console on:
// https://etendering.tenderboard.gov.om/product/ReportAction?eventFlag=RegVendorPublic
//
// Uses fetch() - no page reload, script stays alive, resumes from localStorage if interrupted.

(async function scrapeVendors() {
  const DELAY_MS  = 400;
  const STORE_KEY = 'scraper_vendors';
  const URL       = 'https://etendering.tenderboard.gov.om/product/ReportAction';

  let checkpoint = {};
  try { checkpoint = JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch(e) {}
  const results   = checkpoint.results  || [];
  const startPage = checkpoint.lastPage ? checkpoint.lastPage + 1 : 1;
  if (startPage > 1) console.log(`[Scraper] Resuming from page ${startPage} (${results.length} rows saved)`);

  function parseHTML(html) {
    const doc  = new DOMParser().parseFromString(html, 'text/html');
    const rows = [];
    for (const table of doc.querySelectorAll('table')) {
      const ths = [...table.querySelectorAll('th')].map(t => t.innerText.trim());
      if (!ths.some(h => h.includes('Company') || h.includes('S No'))) continue;
      for (const tr of [...table.querySelectorAll('tr')].slice(1)) {
        const tds  = [...tr.querySelectorAll('td')];
        const cols = tds.map(t => t.innerText.trim());
        if (cols.length >= 9 && /^\d+$/.test(cols[0])) {
          // TD layout: 0=SNo 1=Name 2=CR 3=Address 4=Tel 5=Fax
          //            6=Category 7=RegExpiry 8=CompanyType 9=ViewDetails
          const detailAnchor = tds[9] && tds[9].querySelector('a');
          const detailHref   = detailAnchor ? detailAnchor.getAttribute('href') : '';
          const txnMatch     = detailHref && detailHref.match(/WF_MTxnNo1=(\d+)/);
          rows.push({
            sno: cols[0], company_name: cols[1]||'', cr_number: cols[2]||'',
            address: cols[3]||'', tel: cols[4]||'', fax: cols[5]||'',
            category: cols[6]||'', reg_expiry: cols[7]||'', company_type: cols[8]||'',
            detail_url: detailHref || '',
            txn_no: txnMatch ? txnMatch[1] : '',
          });
        }
      }
      return rows;
    }
    return rows;
  }

  function getMaxPageFromHTML(html) {
    const m = html.match(/name=["']hidMax["'][^>]*value=["'](\d+)["']/i)
           || html.match(/value=["'](\d+)["'][^>]*name=["']hidMax["']/i);
    return m ? parseInt(m[1]) : null;
  }

  function isExpired(html) {
    return html.includes('session') && html.includes('expired') ||
           html.includes('logoutForce') || html.includes('unable to access');
  }

  async function fetchPage(pageNo) {
    const body = new URLSearchParams({
      eventFlag:   'RegVendorPublic',
      pageNo:      String(pageNo),
      prevPageNo:  String(pageNo - 1),
      PublicUrl:   '1',
      vendType:    '-1',
      category:    '-1',
      subCategory: '-1',
      grade:       '-1',
      companyName: '',
      GTB_CRNo:    '',
      CTRL_STRDIRECTION: 'LTR',
      CTRL_USERID: 'null',
      CTRL_ROLEID: '',
      CTRL_SID:    'null',
    });
    const resp = await fetch(URL, {
      method:      'POST',
      headers:     { 'Content-Type': 'application/x-www-form-urlencoded' },
      body:        body.toString(),
      credentials: 'include',   // sends session cookies
    });
    return resp.text();
  }

  function saveCheckpoint(lastPage) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ lastPage, results })); } catch(e) {}
  }

  function download() {
    const blob = new Blob([JSON.stringify(results, null, 2)], { type: 'application/json' });
    const url  = URL.createObjectURL ? URL.createObjectURL(blob) :
                 'data:application/json,' + encodeURIComponent(JSON.stringify(results));
    const a    = document.createElement('a');
    a.href = blob ? window.URL.createObjectURL(blob) : url;
    a.download = 'vendors_full.json'; a.click();
    localStorage.removeItem(STORE_KEY);
    console.log(`[Scraper] DONE - ${results.length} companies → vendors_full.json`);
  }

  // Detect max pages from current page DOM (already loaded)
  const domMax = document.querySelector('input[name="hidMax"]');
  let maxPage  = domMax ? parseInt(domMax.value) : 402;
  console.log(`[Scraper] Starting. Max pages: ${maxPage}. From page: ${startPage}`);

  for (let p = startPage; p <= maxPage; p++) {
    try {
      const html = await fetchPage(p);

      if (isExpired(html)) {
        console.error(`[Scraper] Session expired at page ${p}. Reload page then repaste to resume.`);
        saveCheckpoint(p - 1);
        return;
      }

      const rows = parseHTML(html);
      if (rows.length === 0) {
        console.warn(`[Scraper] Page ${p}: 0 rows - done`);
        break;
      }

      // Update maxPage in case it changed
      const mp = getMaxPageFromHTML(html);
      if (mp) maxPage = mp;

      results.push(...rows);
      saveCheckpoint(p);

      if (p % 20 === 0 || p === maxPage) {
        console.log(`[Scraper] Page ${p}/${maxPage} | Total: ${results.length}`);
      }

      await new Promise(r => setTimeout(r, DELAY_MS));

    } catch(err) {
      console.error(`[Scraper] Error at page ${p}: ${err.message} - retrying in 3s`);
      await new Promise(r => setTimeout(r, 3000));
      p--; // retry same page
    }
  }

  download();
})();


// ── PHASE 2: Enrich with subcategory + grade ─────────────────────────────────
// After Phase 1 finishes and vendors_full.json is downloaded, paste vendors_full.json
// back into localStorage under 'scraper_vendors_enriched', then paste this block.
//
// Usage:
//   1. Run Phase 1 above → downloads vendors_full.json
//   2. In console: localStorage.setItem('scraper_vendors_enriched', JSON.stringify({results: <paste array>, lastIdx: 0}))
//   3. Paste and run this block
//
// (async function enrichVendors() {
//   const DETAIL_BASE = 'https://etendering.tenderboard.gov.om/product/companyNormalDynamicForm';
//   const STORE_KEY   = 'scraper_vendors_enriched';
//   const DELAY_MS    = 350;
//
//   let state = {};
//   try { state = JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch(e) {}
//   const results  = state.results  || [];
//   let   startIdx = state.lastIdx  || 0;
//   console.log(`[Enrich] Resuming from idx ${startIdx}/${results.length}`);
//
//   function parseDetail(html) {
//     const doc = new DOMParser().parseFromString(html, 'text/html');
//     const rows = [];
//     for (const tr of doc.querySelectorAll('table tr')) {
//       const cells = [...tr.querySelectorAll('td')].map(t => t.innerText.trim());
//       if (cells.length >= 2 && cells[0] && cells[0] !== 'Category') {
//         rows.push({ category: cells[0]||'', sub_category: cells[1]||'', grade: cells[2]||'' });
//       }
//     }
//     return rows;
//   }
//
//   for (let i = startIdx; i < results.length; i++) {
//     const v = results[i];
//     if (!v.txn_no) continue;
//     try {
//       const url  = `${DETAIL_BASE}?WF_MTxnNo1=${v.txn_no}&PublicUrl=1`;
//       const html = await fetch(url, { credentials: 'include' }).then(r => r.text());
//       v.detail_categories = parseDetail(html);
//       state.lastIdx = i;
//       localStorage.setItem(STORE_KEY, JSON.stringify(state));
//       if (i % 100 === 0) console.log(`[Enrich] ${i}/${results.length}`);
//       await new Promise(r => setTimeout(r, DELAY_MS));
//     } catch(err) {
//       console.error(`[Enrich] Error idx ${i}: ${err.message} - retrying`);
//       await new Promise(r => setTimeout(r, 3000));
//       i--;
//     }
//   }
//
//   const blob = new Blob([JSON.stringify(results, null, 2)], { type: 'application/json' });
//   const a = document.createElement('a');
//   a.href = window.URL.createObjectURL(blob);
//   a.download = 'vendors_enriched.json'; a.click();
//   console.log(`[Enrich] DONE - ${results.length} companies → vendors_enriched.json`);
// })();
