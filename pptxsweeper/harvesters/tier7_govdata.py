"""Tier 7: government / EU open-data portals (CKAN).

Most national and EU open-data portals run CKAN, whose Action API lets us
select datasets by resource format directly:

    {portal}/api/3/action/package_search
        ?fq=res_format:(PPT OR PPTX)&rows=100&start=N

Each matching dataset carries resources with a `format` field and a `url`
to the actual file. We keep only PPT/PPTX resources. Because CKAN reports
the format authoritatively, resource URLs are trusted even when they are
extensionless (see filter_stage _FORMAT_VERIFIED_SOURCES: "govdata").

Global (page-sharded) source: portals are walked as one combined page
stream, sharded by owns_page() across nodes.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlsplit

from .base import CandidateURL, Harvester, register

log = logging.getLogger("pptxsweeper.harvest.govdata")

_DEFAULT_PORTALS = [
    "https://catalog.data.gov",        # US
    "https://data.gov.uk",             # UK
    "https://open.canada.ca/data/en",  # Canada
    "https://data.gov.au",             # Australia
    "https://datos.gob.es",            # Spain
    "https://data.gov.ie",             # Ireland
]
_PPT_FORMATS = ("PPT", "PPTX", "ppt", "pptx", "Ppt", "Pptx")


@register
class GovDataCkanHarvester(Harvester):
    name = "govdata_ckan"
    tier = 7

    async def discover(self) -> AsyncIterator[CandidateURL]:
        conf = (self.cfg.raw.get("harvesters", {}).get("tier7", {}) or {}).get("govdata", {}) or {}
        portals = conf.get("ckan_portals") or _DEFAULT_PORTALS
        rows = int(conf.get("rows", 100))
        delay = float(conf.get("delay_s", 1.5))
        max_pages = int(conf.get("max_pages_per_portal", 100))

        page_index = 0
        for portal in portals:
            api = portal.rstrip("/") + "/api/3/action/package_search"
            for page in range(max_pages):
                page_index += 1
                if not self.owns_page(page_index):   # page-shard across nodes
                    continue
                resp = await self.polite_get(api, params={
                    "fq": "res_format:(PPT OR PPTX)",
                    "rows": rows, "start": page * rows,
                }, delay_s=delay, retries=2)
                if resp is None or resp.status_code != 200:
                    break
                try:
                    payload = resp.json()
                except ValueError:
                    break
                results = ((payload.get("result") or {}).get("results")) or []
                if not results:
                    break
                for dataset in results:
                    for res in dataset.get("resources", []):
                        fmt = (res.get("format") or "").strip()
                        url = res.get("url", "")
                        if not url or fmt not in _PPT_FORMATS:
                            continue
                        # Some CKAN portals (e.g. open.canada.ca) return
                        # RELATIVE resource paths (/data/dataset/...) with
                        # no scheme/host. Absolutize against the portal
                        # base; absolute URLs pass through unchanged. A URL
                        # that still has no host is dropped here.
                        abs_url = _absolute_resource_url(portal, url)
                        if not abs_url:
                            continue
                        yield CandidateURL(
                            url=abs_url, tier=self.tier,
                            discovery_source=f"govdata:{urlhost(portal)}",
                            metadata={"portal": portal, "format": fmt,
                                      "dataset": dataset.get("name")},
                        )


def _absolute_resource_url(portal: str, url: str) -> str:
    """Absolutize a CKAN resource URL against the portal base.

    Some portals (e.g. open.canada.ca) return relative resource paths
    (/data/dataset/...) with no scheme/host; joining against the portal
    base recovers the full URL. Absolute URLs pass through unchanged.
    Returns '' when no host can be recovered.
    """
    if not url:
        return ""
    joined = urljoin(portal.rstrip("/") + "/", url)
    return joined if urlsplit(joined).netloc else ""


def urlhost(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc.lower().removeprefix("www.")
