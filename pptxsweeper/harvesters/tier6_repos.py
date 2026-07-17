"""Tier 6: repositories & associations -- Zenodo, Figshare, OSF,
Internet Archive, GitHub Code Search. Plugin-interface adapters against
each service's public API.
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from .base import CandidateURL, Harvester, register, DOC_EXTENSIONS, PRESENTATION_EXTENSIONS

log = logging.getLogger("pptxsweeper.harvest.repos")


def _ends_with_doc(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(DOC_EXTENSIONS)


@register
class ZenodoHarvester(Harvester):
    name = "zenodo"
    tier = 6

    async def discover(self) -> AsyncIterator[CandidateURL]:
        api = self.cfg.raw["harvesters"]["tier6"]["zenodo_api"]
        page = 1
        while page <= 100:   # zenodo caps deep pagination
            if not self.owns_page(page):   # global source: shard pages across nodes
                page += 1
                continue
            resp = await self.polite_get(api, params={
                "q": 'filetype:pptx OR filetype:ppt OR resource_type.type:"presentation"',
                "size": 100, "page": page, "access_right": "open",
            }, delay_s=1.5)
            if resp is None or resp.status_code != 200:
                break
            hits = ((resp.json().get("hits") or {}).get("hits")) or []
            if not hits:
                break
            for record in hits:
                for f in record.get("files", []):
                    link = (f.get("links") or {}).get("self", "")
                    key = f.get("key", "")
                    if key.lower().endswith(PRESENTATION_EXTENSIONS) and link:
                        yield CandidateURL(
                            url=link, tier=self.tier, discovery_source="zenodo",
                            metadata={"record_id": record.get("id"),
                                      "filename": key,
                                      "license": (record.get("metadata") or {}).get(
                                          "license", {}).get("id")})
            page += 1


@register
class FigshareHarvester(Harvester):
    name = "figshare"
    tier = 6

    async def discover(self) -> AsyncIterator[CandidateURL]:
        api = self.cfg.raw["harvesters"]["tier6"]["figshare_api"]
        page = 1
        while page <= 100:
            if not self.owns_page(page):   # global source: shard pages across nodes
                page += 1
                continue
            try:
                resp = await self.client.post(api, json={
                    "search_for": ":extension: pptx OR :extension: ppt",
                    "page": page, "page_size": 100, "item_type": 7,  # 7=presentation
                })
            except Exception:
                break
            if resp.status_code != 200:
                break
            items = resp.json()
            if not items:
                break
            for item in items:
                article_id = item.get("id")
                detail = await self.polite_get(
                    f"https://api.figshare.com/v2/articles/{article_id}", delay_s=1.0)
                if detail is None or detail.status_code != 200:
                    continue
                for f in detail.json().get("files", []):
                    name = f.get("name", "")
                    url = f.get("download_url", "")
                    if name.lower().endswith(PRESENTATION_EXTENSIONS) and url:
                        yield CandidateURL(url=url, tier=self.tier,
                                           discovery_source="figshare",
                                           metadata={"article_id": article_id,
                                                     "filename": name})
            page += 1


@register
class OsfHarvester(Harvester):
    name = "osf"
    tier = 6

    async def discover(self) -> AsyncIterator[CandidateURL]:
        # OSF also stores legacy .ppt; sweep both extensions.
        for ext in (".pptx", ".ppt"):
            url = f"https://api.osf.io/v2/files/?filter[name][icontains]={ext}"
            page_index = 0
            while url:
                page_index += 1
                if not self.owns_page(page_index):   # global source: page-shard
                    # still must follow the cursor to reach owned pages
                    resp = await self.polite_get(url, delay_s=1.5)
                    if resp is None or resp.status_code != 200:
                        break
                    url = (resp.json().get("links") or {}).get("next")
                    continue
                resp = await self.polite_get(url, delay_s=1.5)
                if resp is None or resp.status_code != 200:
                    break
                payload = resp.json()
                for item in payload.get("data", []):
                    attrs = item.get("attributes") or {}
                    name = attrs.get("name", "")
                    links = item.get("links") or {}
                    download = links.get("download", "")
                    if name.lower().endswith(PRESENTATION_EXTENSIONS) and download:
                        yield CandidateURL(url=download, tier=self.tier,
                                           discovery_source="osf",
                                           metadata={"filename": name})
                url = (payload.get("links") or {}).get("next")


@register
class InternetArchiveHarvester(Harvester):
    name = "internet_archive"
    tier = 6

    async def discover(self) -> AsyncIterator[CandidateURL]:
        search_api = self.cfg.raw["harvesters"]["tier6"]["ia_api"]
        page = 1
        while page <= 200:
            if not self.owns_page(page):   # global source: shard pages across nodes
                page += 1
                continue
            resp = await self.polite_get(search_api, params={
                "q": 'format:("Microsoft PowerPoint" OR "PowerPoint")',
                "fl[]": "identifier", "rows": 100, "page": page, "output": "json",
            }, delay_s=1.5)
            if resp is None or resp.status_code != 200:
                break
            docs = ((resp.json().get("response") or {}).get("docs")) or []
            if not docs:
                break
            for doc in docs:
                identifier = doc.get("identifier")
                if not identifier:
                    continue
                meta = await self.polite_get(
                    f"https://archive.org/metadata/{identifier}/files", delay_s=1.0)
                if meta is None or meta.status_code != 200:
                    continue
                for f in (meta.json().get("result") or []):
                    name = f.get("name", "")
                    if name.lower().endswith(PRESENTATION_EXTENSIONS):
                        yield CandidateURL(
                            url=f"https://archive.org/download/{identifier}/{name}",
                            tier=self.tier, discovery_source="internet_archive",
                            metadata={"identifier": identifier, "filename": name})
            page += 1


# NOT registered: GitHub's code-search index only covers TEXT source
# files, so binary .ppt/.pptx are never returned (verified: extension:pptx
# yields ~0), the REST code-search endpoint requires auth, and raw URLs
# built with a `HEAD` ref 404 (raw.githubusercontent.com needs a real
# branch/sha). Kept for reference; re-@register only after reworking it to
# resolve a real default branch and confirming a viable query.
class GithubCodeSearchHarvester(Harvester):
    name = "github_code_search"
    tier = 6

    async def discover(self) -> AsyncIterator[CandidateURL]:
        api = self.cfg.raw["harvesters"]["tier6"]["github_code_search_api"]
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            log.warning("GITHUB_TOKEN not set; github code search will be heavily rate-limited")
        for query in ("extension:pptx", "extension:ppt"):
            page = 1
            while page <= 10:   # GitHub caps code search at 1000 results
                resp = await self.polite_get(api, params={
                    "q": query, "per_page": 100, "page": page,
                }, headers=headers, delay_s=6.0)   # 10/min unauthenticated
                if resp is None or resp.status_code != 200:
                    break
                items = resp.json().get("items") or []
                if not items:
                    break
                for item in items:
                    repo = (item.get("repository") or {}).get("full_name", "")
                    path = item.get("path", "")
                    if not repo or not path.lower().endswith(PRESENTATION_EXTENSIONS):
                        continue
                    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
                    yield CandidateURL(url=url, tier=self.tier,
                                       discovery_source="github_code_search",
                                       metadata={"repo": repo, "path": path})
                page += 1
