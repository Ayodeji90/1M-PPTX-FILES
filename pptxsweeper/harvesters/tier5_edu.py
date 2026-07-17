"""Tier 5: universities / OpenCourseWare.

Sitemap + listing crawlers for OCW sites seeded from
seeds/ocw_sites.csv (domain per row). Low acceptance expected; the
filter stage enforces the per-domain cap (filter.tier_domain_caps[5]).
.edu domains from the Common Crawl universe arrive via the Tier 1
harvesters and are capped the same way.
"""
from __future__ import annotations

import csv
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from .base import CandidateURL, Harvester, register, DOC_EXTENSIONS
from .sitemaps import walk_domain_sitemaps

log = logging.getLogger("pptxsweeper.harvest.edu")


@register
class OcwHarvester(Harvester):
    name = "universities_ocw"
    tier = 5

    async def discover(self) -> AsyncIterator[CandidateURL]:
        path = Path(self.cfg.raw["harvesters"]["tier5"]["ocw_seed_file"])
        if not path.is_absolute():
            path = self.cfg.root / path
        if not path.exists():
            log.warning("OCW seed file %s missing", path)
            return
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            domain = (row.get("domain") or "").strip()
            if not domain or not self.owns_domain(domain):
                continue
            async for url in walk_domain_sitemaps(self, domain, DOC_EXTENSIONS,
                                                  delay_s=3.0):
                yield CandidateURL(url=url, tier=self.tier,
                                   discovery_source=f"ocw:{domain}")
