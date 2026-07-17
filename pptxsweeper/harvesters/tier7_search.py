"""Tier 7: open-web filetype dorking via the Brave Search API.

The single biggest untapped vein for .ppt/.pptx is the open web itself:
`filetype:pptx` / `filetype:ppt` queries return direct links to
PowerPoint files hosted anywhere. Brave's Search API is used because it
exposes a clean JSON endpoint with generous, cheap quotas and (unlike
Bing) is not being retired.

Global (page-sharded) source: results are domain-diverse, so this node
harvests the query/offset pages it owns (owns_page) and downloads
whatever it discovered -- the download stage does not re-shard by domain.

Auth: set BRAVE_API_KEY in .env. Without it the harvester logs a warning
and yields nothing (no anonymous access).

Coverage beyond the ~200-result-per-query ceiling is obtained by pairing
each filetype with a rotating list of broadening terms (topics) from
config, e.g. `filetype:pptx lecture`, `filetype:pptx quarterly results`.
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from .base import CandidateURL, Harvester, register, PRESENTATION_EXTENSIONS

log = logging.getLogger("pptxsweeper.harvest.brave")

_API = "https://api.search.brave.com/res/v1/web/search"


@register
class BraveSearchHarvester(Harvester):
    name = "brave_search"
    tier = 7

    async def discover(self) -> AsyncIterator[CandidateURL]:
        key = os.environ.get("BRAVE_API_KEY", "").strip()
        if not key:
            log.warning("BRAVE_API_KEY not set; brave_search harvester disabled")
            return
        conf = (self.cfg.raw.get("harvesters", {}).get("tier7", {}) or {}).get("brave", {}) or {}
        api = conf.get("api", _API)
        count = int(conf.get("results_per_query", 20))      # Brave max 20
        max_offset = int(conf.get("max_offset", 9))          # Brave max 9 -> 200 results
        delay = float(conf.get("delay_s", 1.1))              # free tier ~1 req/s
        terms = conf.get("queries") or [""]
        headers = {"X-Subscription-Token": key, "Accept": "application/json"}

        page_index = 0
        for ext in ("pptx", "ppt"):
            for term in terms:
                query = f"filetype:{ext}" + (f" {term}" if term else "")
                for offset in range(0, max_offset + 1):
                    page_index += 1
                    if not self.owns_page(page_index):   # page-shard across nodes
                        continue
                    resp = await self.polite_get(
                        api, params={"q": query, "count": count, "offset": offset},
                        headers=headers, delay_s=delay, retries=2,
                    )
                    if resp is None or resp.status_code != 200:
                        break
                    try:
                        results = ((resp.json().get("web") or {}).get("results")) or []
                    except ValueError:
                        break
                    if not results:
                        break
                    got = 0
                    for item in results:
                        url = item.get("url", "")
                        if not url:
                            continue
                        path = urlsplit(url).path.lower().split("?", 1)[0]
                        if not path.endswith(PRESENTATION_EXTENSIONS):
                            continue
                        got += 1
                        yield CandidateURL(
                            url=url, tier=self.tier, discovery_source=f"brave:{ext}",
                            metadata={"query": query, "title": (item.get("title") or "")[:200]},
                        )
                    if got == 0 and offset > 0:
                        break   # tail pages returning no ppt: stop paging this query
