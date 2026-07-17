"""Tier 2: SEC EDGAR full-text search for investor-presentation exhibits.

EDGAR rules honored: declared User-Agent with contact email, <=10 req/s.
Searches 8-K/6-K filings for investor presentation exhibits, then builds
Archives URLs for the exhibit documents (.pdf; .htm exhibits are skipped
because the pipeline only delivers pptx/pdf).
"""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

from .base import CandidateURL, Harvester

log = logging.getLogger("pptxsweeper.harvest.edgar")

_FTS_API = "https://efts.sec.gov/LATEST/search-index"
_QUERIES = ('"investor presentation"', '"investor day presentation"',
            '"earnings presentation"')
_FORMS = "8-K,6-K"
_DOC_OK_RE = re.compile(r"\.pptx?$", re.IGNORECASE)


# NOT registered: SEC EDGAR investor-presentation exhibits are filed as
# .htm or .pdf, essentially never .ppt/.pptx, so a pptx-only filter yields
# ~0 while consuming the full 10 req/s crawl budget. Corporate decks are
# better reached via the IR harvester (Q4 CDN / events pages). Kept for
# reference; re-@register only if the client accepts PDF exhibits.
class EdgarHarvester(Harvester):
    name = "sec_edgar"
    tier = 2

    async def discover(self) -> AsyncIterator[CandidateURL]:
        delay = 1.0 / float(self.cfg.raw["politeness"]["edgar"]["requests_per_sec"])
        headers = {"User-Agent": self.cfg.user_agent_for(
            ("politeness", "edgar", "user_agent_template"))}

        for query in _QUERIES:
            offset = 0
            while offset < 10_000:   # EDGAR FTS pagination ceiling
                resp = await self.polite_get(
                    _FTS_API,
                    params={"q": query, "forms": _FORMS, "from": offset},
                    headers=headers, delay_s=delay,
                )
                if resp is None or resp.status_code != 200:
                    break
                try:
                    data = resp.json()
                except ValueError:
                    break
                hits = (data.get("hits") or {}).get("hits") or []
                if not hits:
                    break
                for hit in hits:
                    source = hit.get("_source") or {}
                    hit_id = hit.get("_id", "")          # "accession:document"
                    if ":" not in hit_id:
                        continue
                    accession, document = hit_id.split(":", 1)
                    if not _DOC_OK_RE.search(document):
                        continue
                    ciks = source.get("ciks") or []
                    if not ciks:
                        continue
                    cik = str(int(ciks[0]))              # strip zero padding
                    acc_nodash = accession.replace("-", "")
                    url = (f"https://www.sec.gov/Archives/edgar/data/"
                           f"{cik}/{acc_nodash}/{document}")
                    yield CandidateURL(
                        url=url, tier=self.tier, discovery_source="sec_edgar_fts",
                        metadata={"accession": accession, "cik": cik,
                                  "form": (source.get("root_forms") or [""])[0],
                                  "filed": source.get("file_date"),
                                  "query": query},
                    )
                offset += len(hits)
