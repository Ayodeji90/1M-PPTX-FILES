"""Tier 2: Investor relations harvester.

- Seed list: seeds/sp1500_tickers.csv (ticker,company,website) --
  Fortune 500 + S&P 1500; human-editable, best-effort.
- IR subdomain resolution: ir.X, investors.X, investor.X + /investors path.
- Platform adapters:
    * Q4 Inc.: /feed/Presentation.json endpoints + q4cdn.com asset URLs
    * Notified/GlobeNewswire: presentation library JSON/HTML patterns
    * generic "events & presentations" page scraper as fallback
"""
from __future__ import annotations

import csv
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .base import CandidateURL, Harvester, register, DOC_EXTENSIONS

log = logging.getLogger("pptxsweeper.harvest.ir")

_IR_HOST_PREFIXES = ("ir.", "investors.", "investor.")
_IR_PATHS = ("/investors", "/investor-relations", "/en/investors")
_PRESENTATION_PAGES = (
    "/events-and-presentations", "/presentations", "/events-presentations",
    "/news-and-events/presentations", "/financial-information/presentations",
    "/investors/presentations", "/investors/events-and-presentations",
)
_DOC_LINK_RE = re.compile(
    r"""(?:href|src|data-url|file)\s*=\s*["']([^"']+?\.pptx?)(?:\?[^"']*)?["']""",
    re.IGNORECASE,
)
_Q4_FEED_PATHS = ("/feed/Presentation.json", "/feed/PresentationList.json")


@register
class InvestorRelationsHarvester(Harvester):
    name = "investor_relations"
    tier = 2

    async def discover(self) -> AsyncIterator[CandidateURL]:
        seeds = self._load_seeds()
        log.info("IR harvester: %d companies seeded", len(seeds))
        for company in seeds:
            website = company.get("website", "").strip()
            if not website:
                continue
            base_domain = urlsplit(website if "//" in website else f"https://{website}").netloc
            base_domain = base_domain.removeprefix("www.")
            async for candidate in self._harvest_company(company, base_domain):
                yield candidate

    def _load_seeds(self) -> list[dict]:
        path = Path(self.cfg.raw["harvesters"]["tier2"]["seed_tickers_file"])
        if not path.is_absolute():
            path = self.cfg.root / path
        if not path.exists():
            log.warning("IR seed file %s missing; nothing to harvest", path)
            return []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))

    # ------------------------------------------------------------------
    async def _harvest_company(self, company: dict, base_domain: str
                               ) -> AsyncIterator[CandidateURL]:
        ticker = company.get("ticker", "")
        ir_bases = [f"https://{p}{base_domain}" for p in _IR_HOST_PREFIXES]
        ir_bases += [f"https://{base_domain}{p}" for p in _IR_PATHS]

        for ir_base in ir_bases:
            # Q4 adapter first (cheap JSON endpoints)
            for feed_path in _Q4_FEED_PATHS:
                resp = await self.polite_get(ir_base.rstrip("/") + feed_path, delay_s=2.0,
                                             retries=1)
                if resp is None or resp.status_code != 200:
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    continue
                for url in _extract_doc_urls_from_json(payload):
                    yield CandidateURL(url=url, tier=self.tier,
                                       discovery_source="ir:q4_feed",
                                       metadata={"ticker": ticker, "ir_base": ir_base})

            # Generic events & presentations page scrape
            for page in _PRESENTATION_PAGES:
                page_url = ir_base.rstrip("/") + page
                resp = await self.polite_get(page_url, delay_s=2.0, retries=1)
                if resp is None or resp.status_code != 200:
                    continue
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype:
                    continue
                found_any = False
                for match in _DOC_LINK_RE.finditer(resp.text):
                    doc_url = urljoin(page_url, match.group(1))
                    if not urlsplit(doc_url).path.lower().endswith(DOC_EXTENSIONS):
                        continue
                    found_any = True
                    source = "ir:q4cdn" if "q4cdn.com" in doc_url else "ir:page_scrape"
                    yield CandidateURL(url=doc_url, tier=self.tier,
                                       discovery_source=source,
                                       metadata={"ticker": ticker, "page": page_url})
                if found_any:
                    break   # one working presentations page per IR base is enough
            else:
                continue
            break  # found a working IR base; stop probing alternates


def _extract_doc_urls_from_json(payload, _depth: int = 0) -> list[str]:
    """Recursively pull .pdf/.ppt/.pptx URLs out of arbitrary feed JSON."""
    urls: list[str] = []
    if _depth > 6:
        return urls
    if isinstance(payload, dict):
        for value in payload.values():
            urls.extend(_extract_doc_urls_from_json(value, _depth + 1))
    elif isinstance(payload, list):
        for item in payload:
            urls.extend(_extract_doc_urls_from_json(item, _depth + 1))
    elif isinstance(payload, str):
        s = payload.strip()
        if s.startswith("http") and urlsplit(s).path.lower().endswith(DOC_EXTENSIONS):
            urls.append(s)
    return urls
