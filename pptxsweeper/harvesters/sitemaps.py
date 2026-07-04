"""Shared sitemap walker used by Tier 3/4/5 harvesters.

Discovers sitemap URLs from robots.txt (Sitemap: lines) falling back to
/sitemap.xml, recursively expands sitemap indexes, and yields document
URLs matching the wanted extensions. Defensive: caps recursion depth and
total sitemaps per domain so one bad site can't wedge a harvest run.
"""
from __future__ import annotations

import gzip
import logging
import re
from collections.abc import AsyncIterator

log = logging.getLogger("pptxsweeper.harvest.sitemaps")

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_SITEMAP_HINT_RE = re.compile(r"<sitemap[\s>]", re.IGNORECASE)
_MAX_SITEMAPS_PER_DOMAIN = 500
_MAX_DEPTH = 4


def _decode_body(resp) -> str:
    data = resp.content
    if resp.url.path.endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    return data.decode("utf-8", errors="replace")


async def walk_domain_sitemaps(harvester, domain: str, extensions: tuple[str, ...],
                               delay_s: float = 2.0) -> AsyncIterator[str]:
    """Yield URLs under `domain` whose path ends with one of `extensions`.
    `harvester` provides polite_get (Harvester base)."""
    roots: list[str] = []
    robots = await harvester.polite_get(f"https://{domain}/robots.txt", delay_s=delay_s)
    if robots is not None and robots.status_code == 200:
        for line in robots.text.splitlines():
            if line.lower().startswith("sitemap:"):
                roots.append(line.split(":", 1)[1].strip())
    if not roots:
        roots = [f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml"]

    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(r, 0) for r in roots]
    visited = 0
    while queue and visited < _MAX_SITEMAPS_PER_DOMAIN:
        sitemap_url, depth = queue.pop(0)
        if sitemap_url in seen or depth > _MAX_DEPTH:
            continue
        seen.add(sitemap_url)
        visited += 1
        resp = await harvester.polite_get(sitemap_url, delay_s=delay_s)
        if resp is None or resp.status_code != 200:
            continue
        body = _decode_body(resp)
        locs = _LOC_RE.findall(body)
        is_index = bool(_SITEMAP_HINT_RE.search(body))
        for loc in locs:
            loc = loc.strip()
            lower = loc.lower().split("?", 1)[0]
            if lower.endswith(extensions):
                yield loc
            elif is_index or lower.endswith((".xml", ".xml.gz")):
                queue.append((loc, depth + 1))
