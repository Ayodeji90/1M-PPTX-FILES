"""Tier 7: standards / conference bodies.

Standards organizations publish enormous numbers of contribution decks
as directly-downloadable .ppt/.pptx in browsable (Apache/nginx autoindex)
directory trees:

  * 3GPP FTP        https://www.3gpp.org/ftp/   (giant .ppt/.zip tree)
  * IETF proceedings https://www.ietf.org/proceedings/ (per-meeting /slides/)
  * OASIS docs      https://docs.oasis-open.org/

This harvester is a generic recursive directory walker: it GETs a listing
page, yields links whose path ends in .ppt/.pptx, and recurses into
sub-directory links under the same host. Defensive caps (max dirs, depth,
same-host only) keep one huge tree from wedging the run; resumable via
harvest_cursor is intentionally omitted here because caps bound the work.

Domain-list source: each root is one host, sharded by owns_domain().
Roots come from config (harvesters.tier7.standards.autoindex_roots).
"""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlsplit

from .base import CandidateURL, Harvester, register, PRESENTATION_EXTENSIONS

log = logging.getLogger("pptxsweeper.harvest.standards")

_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_DEFAULT_ROOTS = [
    "https://www.3gpp.org/ftp/",
    "https://www.ietf.org/proceedings/",
    "https://docs.oasis-open.org/",
]


def _is_sort_link(href: str) -> bool:
    # Apache autoindex column-sort links (?C=N;O=D) and parent-dir links.
    return href.startswith("?") or href in ("../", "./") or href.startswith("#")


@register
class StandardsAutoindexHarvester(Harvester):
    name = "standards_bodies"
    tier = 7

    async def discover(self) -> AsyncIterator[CandidateURL]:
        conf = (self.cfg.raw.get("harvesters", {}).get("tier7", {}) or {}).get("standards", {}) or {}
        roots = conf.get("autoindex_roots") or _DEFAULT_ROOTS
        delay = float(conf.get("delay_s", 1.5))
        max_dirs = int(conf.get("max_dirs_per_root", 4000))
        max_depth = int(conf.get("max_depth", 8))

        for root in roots:
            host = urlsplit(root).netloc.lower().removeprefix("www.")
            if not self.owns_domain(host):
                continue
            count = 0
            async for url in self._walk(root, host, delay, max_dirs, max_depth):
                count += 1
                yield CandidateURL(
                    url=url, tier=self.tier,
                    discovery_source=f"standards:{host}",
                    metadata={"root": root},
                )
            log.info("standards %s: %d presentation URLs", host, count)

    async def _walk(self, root: str, host: str, delay: float,
                    max_dirs: int, max_depth: int) -> AsyncIterator[str]:
        seen: set[str] = set()
        queue: list[tuple[str, int]] = [(root, 0)]
        visited = 0
        while queue and visited < max_dirs:
            listing_url, depth = queue.pop(0)
            if listing_url in seen or depth > max_depth:
                continue
            seen.add(listing_url)
            visited += 1
            resp = await self.polite_get(listing_url, delay_s=delay, retries=2)
            if resp is None or resp.status_code != 200:
                continue
            ctype = resp.headers.get("content-type", "").lower()
            if "html" not in ctype and "text" not in ctype:
                continue
            for href in _HREF_RE.findall(resp.text):
                href = href.strip()
                if not href or _is_sort_link(href):
                    continue
                target = urljoin(listing_url, href)
                split = urlsplit(target)
                if split.netloc.lower().removeprefix("www.") != host:
                    continue   # never leave the root host
                if not target.startswith(root):
                    continue   # stay within the root subtree
                path = split.path.lower()
                if path.endswith(PRESENTATION_EXTENSIONS):
                    yield target
                elif target.endswith("/") and target not in seen:
                    queue.append((target, depth + 1))
