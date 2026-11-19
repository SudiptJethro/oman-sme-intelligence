"""
run_scrapers.py
───────────────
GitHub Actions entrypoint. Run all or individual scrapers.

Usage:
  python run_scrapers.py                    # all scrapers
  python run_scrapers.py --source tender_board
  python run_scrapers.py --source riyada
  python run_scrapers.py --source moci
  python run_scrapers.py --source jsrs
  python run_scrapers.py --source oia_list
"""
import argparse, logging, sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from scrapers.oia_list               import OIAListScraper
from scrapers.tender_board           import TenderBoardScraper
from scrapers.tender_vendor_registry import TenderVendorRegistryScraper
from scrapers.tender_awards_scraper  import TenderAwardsScraper
from scrapers.riyada                 import RiyadaScraper
from scrapers.moci                   import MoCI_Scraper
from scrapers.jsrs                   import JSRSScraper
from scrapers.msx                    import MSXScraper

SCRAPERS = {
    "oia_list":               OIAListScraper,
    "tender_board":           TenderBoardScraper,
    "tender_vendor_registry": TenderVendorRegistryScraper,
    "tender_awards":          TenderAwardsScraper,
    "riyada":                 RiyadaScraper,
    "moci":                   MoCI_Scraper,
    "jsrs":                   JSRSScraper,
    "msx":                    MSXScraper,
}

# Run order: OIA codes first, then authoritative registry (Tender Board vendors),
# then supplementary sources, then JSRS verification, then discovery
RUN_ORDER = [
    "oia_list",
    "tender_vendor_registry",  # primary: ~20K vendors, SME-filtered, OIA-matched
    "tender_awards",           # awarded contracts 2019→present - enriches revenue signals
    "tender_board",            # OIA mandatory list + JSRS enrichment
    "riyada", "moci",         # supplementary directories
    "jsrs",                    # CR-based JSRS verification
    "msx",                     # discovery
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="all", choices=list(SCRAPERS.keys()) + ["all"])
    args = parser.parse_args()

    if args.source == "all":
        sources = RUN_ORDER
    else:
        sources = [args.source]

    for source in sources:
        logging.info(f"\n{'='*50}\nRunning: {source}\n{'='*50}")
        scraper = SCRAPERS[source]()
        scraper.run()

    logging.info("\n✅ All scrapers complete")


if __name__ == "__main__":
    main()
