"""Tier 1: Wayback CDX API harvester.

Domain-scoped (like all CDX indexes, Wayback's is keyed by domain --
it cannot answer "every URL ending .pptx" globally). For each seed /
already-known domain it lists archived PowerPoint captures with
digest collapse and mimetype filters, yielding ORIGINAL urls; the
download stage tries origin first and falls back to the archived copy.

Politeness: <=1 req/sec, 429 honored with backoff (polite_get).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from .base import CandidateURL, Harvester, register, PRESENTATION_EXTENSIONS
from .seeds_util import tier1_seed_domains

log = logging.getLogger("pptxsweeper.harvest.wayback")

_MIME_FILTERS = (
    "mimetype:application/vnd.ms-powerpoint",
    "mimetype:application/vnd.openxmlformats-officedocument.presentationml.presentation",
)


@register
class WaybackCdxHarvester(Harvester):
    name = "wayback_cdx"
    tier = 1

    async def discover(self) -> AsyncIterator[CandidateURL]:
        wb = self.cfg.raw["wayback"]
        base = wb["cdx_base_url"]
        page_size = int(wb["cdx_page_size"])
        delay = 1.0 / float(wb["requests_per_sec_per_worker"])

        from ..db.dao import Registry
        reg = Registry(self.cfg.path("paths", "db_path"))
        try:
            domains = tier1_seed_domains(self.cfg, reg)
        finally:
            reg.close()
        domains = [d for d in domains if self.owns_domain(d)]
        log.info("wayback CDX: walking %d seed domains owned by node %d/%d",
                 len(domains), self.node.node_id, self.node.node_count)

        for domain in domains:
            for mime_filter in _MIME_FILTERS:
                resume_key: str | None = None
                while True:
                    params = {
                        "url": domain,
                        "matchType": "domain",
                        "output": "json",
                        "collapse": "digest",
                        "filter": [mime_filter, "statuscode:200"],
                        "limit": page_size,
                        "showResumeKey": "true",
                        "fl": "original,timestamp,mimetype,length,digest",
                    }
                    if resume_key:
                        params["resumeKey"] = resume_key
                    resp = await self.polite_get(base, params=params, delay_s=delay,
                                                 retries=2)
                    if resp is None or resp.status_code != 200 or not resp.text.strip():
                        break
                    try:
                        rows = resp.json()
                    except ValueError:
                        break
                    if not rows:
                        break
                    header, *records = rows
                    resume_key = None
                    for rec in records:
                        if not rec:
                            continue
                        if len(rec) == 1:      # resume key row after blank row
                            resume_key = rec[0]
                            continue
                        record = dict(zip(header, rec))
                        url = record.get("original", "")
                        if not url:
                            continue
                        # The CDX query already filtered to PowerPoint MIME
                        # types, so trust the capture even when the URL path
                        # has no .ppt/.pptx suffix (e.g. /download?id=123 or
                        # extensionless CMS routes). Only skip captures that
                        # advertise a NON-presentation path extension, which
                        # signals a mislabeled/served-as-other resource.
                        path = urlsplit(url).path.lower()
                        suffix = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
                        if suffix and f".{suffix}" not in PRESENTATION_EXTENSIONS \
                                and suffix in ("htm", "html", "pdf", "zip", "doc", "docx"):
                            continue
                        yield CandidateURL(
                            url=url, tier=self.tier, discovery_source="wayback_cdx",
                            metadata={
                                "wayback_timestamp": record.get("timestamp"),
                                "wayback_digest": record.get("digest"),
                                "mime": record.get("mimetype"),
                            },
                        )
                    if not resume_key:
                        break
