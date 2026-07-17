"""Tier 3: US federal sources.

- Sitemap harvester over federalreserve.gov + 12 regional Fed banks and
  the statistical agencies listed in config.
- OSTI.gov API adapter for DOE national-lab documents.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from .base import CandidateURL, Harvester, register, DOC_EXTENSIONS
from .sitemaps import walk_domain_sitemaps

log = logging.getLogger("pptxsweeper.harvest.federal")


@register
class FederalSitemapHarvester(Harvester):
    name = "us_federal_sitemaps"
    tier = 3

    async def discover(self) -> AsyncIterator[CandidateURL]:
        t3 = self.cfg.raw["harvesters"]["tier3"]
        domains = list(t3["sitemap_domains"]) + list(t3["fed_regional_banks"])
        domains = [d for d in domains if self.owns_domain(d)]
        for domain in domains:
            count = 0
            async for url in walk_domain_sitemaps(self, domain, DOC_EXTENSIONS,
                                                  delay_s=2.0):
                count += 1
                yield CandidateURL(url=url, tier=self.tier,
                                   discovery_source=f"sitemap:{domain}")
            log.info("sitemap %s: %d documents", domain, count)


# NOT registered: OSTI serves DOE national-lab documents, and the revised
# client criteria exclude prestigious US research labs/FFRDCs. Re-add
# @register only with explicit written client approval.
class OstiHarvester(Harvester):
    name = "osti"
    tier = 3

    async def discover(self) -> AsyncIterator[CandidateURL]:
        api = self.cfg.raw["harvesters"]["tier3"]["osti_api_base"]
        page = 0
        while True:
            resp = await self.polite_get(api, params={
                "rows": 100, "page": page,
                "product_type": "Conference",   # slides/talks live here
            }, delay_s=1.0)
            if resp is None or resp.status_code != 200:
                break
            try:
                records = resp.json()
            except ValueError:
                break
            if isinstance(records, dict):
                records = records.get("records") or records.get("data") or []
            if not records:
                break
            for record in records:
                for link in record.get("links", []):
                    href = link.get("href", "")
                    if href.lower().split("?")[0].endswith(DOC_EXTENSIONS):
                        yield CandidateURL(
                            url=href, tier=self.tier, discovery_source="osti_api",
                            metadata={"osti_id": record.get("osti_id"),
                                      "title": (record.get("title") or "")[:200]},
                        )
                # fulltext landing links: try the direct PDF pattern
                osti_id = record.get("osti_id")
                if osti_id:
                    yield CandidateURL(
                        url=f"https://www.osti.gov/servlets/purl/{osti_id}",
                        tier=self.tier, discovery_source="osti_purl",
                        metadata={"osti_id": osti_id,
                                  "note": "purl redirects to fulltext (usually pdf)"},
                    )
            page += 1
