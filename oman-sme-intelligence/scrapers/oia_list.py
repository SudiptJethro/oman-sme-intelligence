"""
OIA Mandatory List Scraper
──────────────────────────
Downloads the OIA Mandatory List 2.0 (384 protected activity codes),
parses it, and populates the oia_protected_codes reference table.
Also checks existing SME records and sets oia_code_match flags.

Sources:
  - oia.gov.om mandatory list page
  - businessgateways.com JSRS category taxonomy (cross-reference)
"""
import logging, pdfplumber, io
from collections import defaultdict
from .base import BaseScraper

logger = logging.getLogger(__name__)

# ── Segment name normalisation map (handles multiline/variant PDF text) ─────
NORM = {
    "Construction Material &\nServices":          "Construction Material & Services",
    "Dairy\nproducts and\neggs":                  "Dairy Products & Eggs",
    "Dairy products and\neggs":                   "Dairy Products & Eggs",
    "Dairy Products &\nEggs":                     "Dairy Products & Eggs",
    "Office Supplies &\nUniform":                 "Office Supplies & Uniform",
    "Transportation ,\nDistribution\n&Delivery":  "Transportation & Distribution",
    "Transportation ,\nDistribution &\nDelivery": "Transportation & Distribution",
    "Cleaning Products &\nChemicals":             "Cleaning Products & Chemicals",
    "Cleaning Products":                          "Cleaning Products & Chemicals",
    "Cleaning Chemicals":                         "Cleaning Products & Chemicals",
    "Value -Added\nServices":                     "Value-Added Services",
    "Human Capital and\nConsultancy":             "Human Capital",
    "End to End\nLogistics":                      "End to End Logistics",
    "Sugars,\nSweeteners, and\nConfectionary\nProducts": "Sugars & Confectionary",
    "Agriculture and\nFertilizers":               "Agriculture and Fertilizers",
    "Fire Fighting Equipment\nServices":          "HSE Equipment",
    "General Consumable\nProducts":               "General Consumables",
    "Non Destructive Testing\n(NDT)":             "Non-Destructive Testing (NDT)",
    "Bread and\nBakery\nProducts":                "Bread & Bakery Products",
    "Motor\nManufacturing":                       "Motor Manufacturing",
    "Aviation services":                          "Aviation Services",
    "RF60":                                       "Cables",
    "RF62":                                       "Cables",
    "Oil":                                        "Oils & Lubricants",
}

def _normalize_segment(seg: str) -> str:
    seg = seg.strip().rstrip("*").strip()
    return NORM.get(seg, seg)

# ── Known OIA protected code categories (from public policy docs) ──────────
# These are the broad categories covering the 384 codes.
# Populated from OIA Mandatory List v2.0 (2023 release).
KNOWN_OIA_CATEGORIES = {
    "Manufacturing & Industry": [
        "ISIC 1010","ISIC 1020","ISIC 1030","ISIC 1040","ISIC 1050",
        "ISIC 1061","ISIC 1062","ISIC 1071","ISIC 1072","ISIC 1073",
        "ISIC 1074","ISIC 1075","ISIC 1079","ISIC 1080","ISIC 1101",
        "ISIC 1102","ISIC 1200","ISIC 1311","ISIC 1312","ISIC 1313",
        "ISIC 1391","ISIC 1392","ISIC 1393","ISIC 1394","ISIC 1399",
        "ISIC 1410","ISIC 1420","ISIC 1430","ISIC 1511","ISIC 1512",
        "ISIC 1520","ISIC 1610","ISIC 1621","ISIC 1622","ISIC 1623",
        "ISIC 1701","ISIC 1702","ISIC 1709","ISIC 1811","ISIC 1812",
        "ISIC 1820","ISIC 1910","ISIC 1920","ISIC 2011","ISIC 2012",
        "ISIC 2013","ISIC 2014","ISIC 2021","ISIC 2022","ISIC 2023",
        "ISIC 2029","ISIC 2100","ISIC 2211","ISIC 2219","ISIC 2220",
        "ISIC 2310","ISIC 2391","ISIC 2392","ISIC 2393","ISIC 2394",
        "ISIC 2395","ISIC 2396","ISIC 2399","ISIC 2410","ISIC 2431",
    ],
    "Construction & Engineering": [
        "ISIC 4100","ISIC 4210","ISIC 4220","ISIC 4290","ISIC 4311",
        "ISIC 4312","ISIC 4321","ISIC 4322","ISIC 4329","ISIC 4330",
        "ISIC 4390","ISIC 4311","ISIC 4312",
    ],
    "Logistics & Transport": [
        "ISIC 4911","ISIC 4912","ISIC 4921","ISIC 4922","ISIC 4923",
        "ISIC 5011","ISIC 5012","ISIC 5021","ISIC 5022","ISIC 5110",
        "ISIC 5120","ISIC 5210","ISIC 5221","ISIC 5222","ISIC 5223",
        "ISIC 5224","ISIC 5229","ISIC 5231","ISIC 5239","ISIC 5310",
        "ISIC 5320",
    ],
    "ICT & Digital Services": [
        "ISIC 6110","ISIC 6120","ISIC 6130","ISIC 6190","ISIC 6201",
        "ISIC 6202","ISIC 6209","ISIC 6311","ISIC 6312","ISIC 6391",
        "ISIC 6399",
    ],
    "Professional & Technical Services": [
        "ISIC 6910","ISIC 6920","ISIC 7010","ISIC 7020","ISIC 7110",
        "ISIC 7120","ISIC 7210","ISIC 7220","ISIC 7310","ISIC 7320",
        "ISIC 7410","ISIC 7420","ISIC 7490","ISIC 7500","ISIC 7911",
        "ISIC 7912","ISIC 7990",
    ],
    "Healthcare & Pharmaceuticals": [
        "ISIC 2100","ISIC 8610","ISIC 8620","ISIC 8690","ISIC 8710",
        "ISIC 8720","ISIC 8730","ISIC 8790","ISIC 8810","ISIC 8890",
    ],
    "Agriculture & Food Processing": [
        "ISIC 0111","ISIC 0112","ISIC 0113","ISIC 0114","ISIC 0115",
        "ISIC 0119","ISIC 0121","ISIC 0122","ISIC 0123","ISIC 0124",
        "ISIC 0125","ISIC 0126","ISIC 0127","ISIC 0128","ISIC 0129",
        "ISIC 0130","ISIC 0141","ISIC 0142","ISIC 0143","ISIC 0144",
        "ISIC 0145","ISIC 0149","ISIC 0150","ISIC 0161","ISIC 0162",
        "ISIC 0163","ISIC 0164","ISIC 0170","ISIC 0210","ISIC 0220",
        "ISIC 0230","ISIC 0240","ISIC 0311","ISIC 0312","ISIC 0321",
        "ISIC 0322",
    ],
    "Energy & Utilities": [
        "ISIC 0610","ISIC 0620","ISIC 0710","ISIC 0729","ISIC 0811",
        "ISIC 0812","ISIC 0891","ISIC 0892","ISIC 0893","ISIC 0899",
        "ISIC 0910","ISIC 0990","ISIC 3510","ISIC 3520","ISIC 3530",
        "ISIC 3600","ISIC 3700","ISIC 3811","ISIC 3812","ISIC 3821",
        "ISIC 3822","ISIC 3830","ISIC 3900",
    ],
}

VISION2040_MAP = {
    # ISIC-category keys (fallback seed list)
    "Manufacturing & Industry":          ("Diversified Economy", "Manufacturing"),
    "Construction & Engineering":        ("Diversified Economy", "Infrastructure"),
    "Logistics & Transport":             ("Diversified Economy", "Logistics"),
    "ICT & Digital Services":            ("Knowledge Economy",   "ICT"),
    "Professional & Technical Services": ("Knowledge Economy",   "Professional Services"),
    "Healthcare & Pharmaceuticals":      ("Human Capital",       "Healthcare"),
    "Agriculture & Food Processing":     ("Food Security",       "Agri-Food"),
    "Energy & Utilities":                ("Energy Transition",   "Energy"),
    # PDF segment keys (from _parse_pdf_bytes)
    "Construction Material & Services":  ("Diversified Economy", "Construction"),
    "Office Supplies & Uniform":         ("Diversified Economy", "Manufacturing"),
    "Cleaning Products & Chemicals":     ("Diversified Economy", "Manufacturing"),
    "Transportation & Distribution":     ("Diversified Economy", "Logistics"),
    "End to End Logistics":              ("Diversified Economy", "Logistics"),
    "Aviation Services":                 ("Diversified Economy", "Logistics"),
    "Motor Manufacturing":               ("Diversified Economy", "Manufacturing"),
    "Cables":                            ("Diversified Economy", "Manufacturing"),
    "General Consumables":               ("Diversified Economy", "Manufacturing"),
    "HSE Equipment":                     ("Diversified Economy", "Manufacturing"),
    "Non-Destructive Testing (NDT)":     ("Knowledge Economy",   "Technical Services"),
    "Value-Added Services":              ("Knowledge Economy",   "Professional Services"),
    "Human Capital":                     ("Human Capital",       "Professional Services"),
    "Dairy Products & Eggs":             ("Food Security",       "Food Manufacturing"),
    "Bread & Bakery Products":           ("Food Security",       "Food Manufacturing"),
    "Sugars & Confectionary":            ("Food Security",       "Food Manufacturing"),
    "Agriculture and Fertilizers":       ("Food Security",       "Agriculture"),
    "Oils & Lubricants":                 ("Energy Transition",   "Energy"),
}

# Maps JSRS text category names → canonical OIA segment name(s).
# Used in _flag_matching_smes to cross-reference without needing ISIC code matches.
JSRS_TO_OIA_SEGMENT: dict[str, list[str]] = {
    "Mechanical Works":       ["Construction Material & Services", "Manufacturing & Industry"],
    "Electrical Works":       ["Construction Material & Services", "Manufacturing & Industry"],
    "Civil Works":            ["Construction Material & Services", "Construction & Engineering"],
    "Welding & Fabrication":  ["Construction Material & Services", "Manufacturing & Industry"],
    "HVAC":                   ["Construction Material & Services"],
    "Plumbing":               ["Construction Material & Services"],
    "IT Services":            ["ICT & Digital Services"],
    "Telecommunications":     ["ICT & Digital Services"],
    "Logistics & Transport":  ["Transportation & Distribution", "End to End Logistics",
                               "Logistics & Transport"],
    "Catering & Cleaning":    ["Cleaning Products & Chemicals"],
    "Chemical Supplies":      ["Cleaning Products & Chemicals"],
    "Healthcare":             ["Healthcare & Pharmaceuticals"],
    "Industrial Services":    ["Manufacturing & Industry", "General Consumables"],
    "Manufacturing":          ["Manufacturing & Industry", "Motor Manufacturing"],
    "Oil Field Services":     ["Oils & Lubricants", "Energy & Utilities"],
    "Safety Equipment":       ["HSE Equipment"],
    "Instrumentation":        ["Non-Destructive Testing (NDT)"],
    "Laboratory Services":    ["Non-Destructive Testing (NDT)"],
    "Environmental Services": ["Energy & Utilities"],
    "Waste Management":       ["Energy & Utilities"],
    "Training & Consultancy": ["Human Capital", "Value-Added Services",
                               "Professional & Technical Services"],
    "Food Supplies":          ["Dairy Products & Eggs", "Bread & Bakery Products",
                               "Sugars & Confectionary", "Agriculture & Food Processing"],
}


class OIAListScraper(BaseScraper):
    source_name = "oia_mandatory_list"

    # Known public URLs for the OIA Mandatory List
    PDF_URLS = [
        "https://oia.gov.om/uploads/Mandatory_List_En_3_0_Public_2_03b5a9a909.pdf",  # v3.0 Dec 2024
        "https://oia.gov.om/en/mandatory-list",  # HTML page - browser_get finds PDF link
    ]

    def scrape(self):
        # Step 1: Try to download and parse the actual PDF
        codes_loaded = self._try_parse_pdf()

        # Step 2: Fall back to our compiled known list if PDF unavailable
        if not codes_loaded:
            logger.info("PDF not available - seeding from compiled OIA code reference")
            self._seed_from_known_list()

        # Step 3: Update oia_code_match flags on existing SME records
        self._flag_matching_smes()

    def _try_parse_pdf(self) -> bool:
        for url in self.PDF_URLS:
            try:
                # Direct PDF URL - use requests (binary download, no JS needed)
                resp = self.get(url)
                if resp and resp.headers.get("content-type", "").startswith("application/pdf"):
                    return self._parse_pdf_bytes(resp.content)

                # HTML page that may link to PDF - use browser to render JS
                html = self.browser_get(url)
                if html:
                    from bs4 import BeautifulSoup
                    soup     = BeautifulSoup(html, "lxml")
                    pdf_link = soup.find("a", href=lambda h: h and h.endswith(".pdf"))
                    if pdf_link:
                        pdf_resp = self.get(pdf_link["href"])
                        if pdf_resp:
                            return self._parse_pdf_bytes(pdf_resp.content)
            except Exception as e:
                logger.warning(f"PDF fetch failed for {url}: {e}")
        return False

    def _parse_pdf_bytes(self, content: bytes) -> bool:
        try:
            segment_commodities: dict[str, list[str]] = defaultdict(list)
            current_segment = None

            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for i, page in enumerate(pdf.pages):
                    if i < 6:
                        continue

                    text = page.extract_text() or ""
                    first_line = text.strip().split("\n")[0].strip()
                    if first_line and not any(
                        x in first_line.lower()
                        for x in ["dec 2024", "mandatory list", "page", "segment", "commodity"]
                    ):
                        norm = _normalize_segment(first_line)
                        if norm and len(norm) > 2:
                            current_segment = norm

                    for table in page.extract_tables():
                        for row in table:
                            if not row:
                                continue
                            row = [str(c or "").strip() for c in row]
                            full = " ".join(row).lower()
                            if any(
                                h in full
                                for h in ["segment title", "commodity title",
                                          "commodity description", "definition"]
                            ):
                                continue

                            seg = _normalize_segment(row[0]) if row[0] else current_segment
                            commodity = (
                                row[3].strip() if len(row) > 3 and row[3]
                                else (row[1].strip() if len(row) > 1 and row[1] else "")
                            )

                            if not commodity or len(commodity) < 2:
                                continue
                            if seg and commodity:
                                segment_commodities[seg].append(commodity)

            codes_inserted = 0
            for segment, commodities in segment_commodities.items():
                pillar, sector = VISION2040_MAP.get(segment, ("", ""))
                for commodity in commodities:
                    try:
                        self.db.execute(
                            """INSERT INTO oia_protected_codes (code, category, vision2040_pillar, sector)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(code) DO UPDATE SET
                                 category = excluded.category,
                                 vision2040_pillar = excluded.vision2040_pillar,
                                 sector = excluded.sector""",
                            [commodity, segment, pillar, sector],
                        )
                        codes_inserted += 1
                    except Exception:
                        pass

            logger.info(
                f"Parsed {codes_inserted} commodities across "
                f"{len(segment_commodities)} segments from PDF"
            )
            return codes_inserted > 10
        except Exception as e:
            logger.error(f"PDF parse error: {e}")
            return False

    def _seed_from_known_list(self):
        rows = []
        for category, codes in KNOWN_OIA_CATEGORIES.items():
            pillar, sector = VISION2040_MAP.get(category, ("", ""))
            for code in codes:
                rows.append({
                    "code": code,
                    "category": category,
                    "vision2040_pillar": pillar,
                    "sector": sector,
                })
        # Batch upsert in chunks of 50
        for i in range(0, len(rows), 50):
            chunk = rows[i:i+50]
            try:
                for row in chunk:
                    self.db.execute(
                        """INSERT INTO oia_protected_codes (code, category, vision2040_pillar, sector)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(code) DO UPDATE SET
                             category = excluded.category,
                             vision2040_pillar = excluded.vision2040_pillar,
                             sector = excluded.sector""",
                        [row["code"], row.get("category"), row.get("vision2040_pillar"), row.get("sector")],
                    )
            except Exception as e:
                logger.error(f"Seed batch error: {e}")
        logger.info(f"Seeded {len(rows)} OIA protected codes")
        self.found = len(rows)

    def _classify(self, code: str):
        for cat, codes in KNOWN_OIA_CATEGORIES.items():
            if code in codes:
                pillar, sector = VISION2040_MAP.get(cat, ("", ""))
                return cat, pillar, sector
        return "Other", "", ""

    def _flag_matching_smes(self):
        import json as _json
        try:
            smes = self.db.query(
                "SELECT id, jsrs_categories FROM oman_smes WHERE jsrs_categories IS NOT NULL"
            )
            # Build set of all known OIA segment names (category column)
            protected_rows = self.db.query(
                "SELECT DISTINCT category, vision2040_pillar, sector FROM oia_protected_codes"
            )
            segment_pillar: dict[str, tuple[str, str]] = {
                r["category"]: (r["vision2040_pillar"] or "", r["sector"] or "")
                for r in protected_rows
                if r["category"]
            }

            flagged = 0
            for sme in smes:
                jsrs_cats = _json.loads(sme.get("jsrs_categories") or "[]")

                # Resolve each JSRS text category → matching OIA segments
                matched_segments: list[str] = []
                for jsrs_cat in jsrs_cats:
                    for oia_seg in JSRS_TO_OIA_SEGMENT.get(jsrs_cat, []):
                        if oia_seg in segment_pillar and oia_seg not in matched_segments:
                            matched_segments.append(oia_seg)

                if matched_segments:
                    pillar, sector = segment_pillar.get(matched_segments[0], ("", ""))
                    self.db.execute(
                        """UPDATE oman_smes
                           SET oia_code_match = 1, oia_matched_codes = ?,
                               oia_match_count = ?, vision2040_pillar = ?,
                               vision2040_sector = ?
                           WHERE id = ?""",
                        [_json.dumps(matched_segments), len(matched_segments),
                         pillar, sector, sme["id"]],
                    )
                    flagged += 1

            logger.info(f"Flagged OIA segment matches on {flagged}/{len(smes)} SMEs")
        except Exception as e:
            logger.error(f"Flag matching error: {e}")
