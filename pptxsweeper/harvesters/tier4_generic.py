"""Tier 4: international / intergovernmental / country-registry sources.

One generic, YAML-configurable harvester covers most of the tier. Each
source in seeds/sources/*.yaml:

    name: world_bank_okr
    domain: openknowledge.worldbank.org
    tier: 4
    method: api_json | sitemap | oai_pmh | listing_page
    url: https://...            # api endpoint / listing page / sitemap root
    delay_s: 2.0                # politeness override
    # api_json:
    page_param: page            # increments from 0/1 until empty
    page_start: 1
    params: {rows: 100}
    # listing_page:
    max_pages: 50

The ~180 central banks live in seeds/central_banks.csv
(name,domain,method) -- best-effort, human-editable, harvested as
sitemap (default) or listing_page per row.
"""
from __future__ import annotations

import csv
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml

from .base import CandidateURL, Harvester, register, DOC_EXTENSIONS
from .sitemaps import walk_domain_sitemaps

log = logging.getLogger("pptxsweeper.harvest.generic")

_DOC_LINK_RE = re.compile(
    r"""href\s*=\s*["']([^"']+?\.pptx?)(?:\?[^"']*)?["']""", re.IGNORECASE)
_OAI_RECORD_RE = re.compile(r"<dc:identifier>\s*(https?://[^<]+?)\s*</dc:identifier>",
                            re.IGNORECASE)
_OAI_TOKEN_RE = re.compile(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>")


class GenericSourceHarvester(Harvester):
    """Harvest one YAML-defined source. Instantiated per source spec."""

    name = "generic_source"
    tier = 4

    def __init__(self, cfg, client, spec: dict):
        super().__init__(cfg, client)
        self.spec = spec
        self.name = f"generic:{spec['name']}"
        self.tier = int(spec.get("tier", 4))
        self.delay = float(spec.get("delay_s", 2.0))

    async def discover(self) -> AsyncIterator[CandidateURL]:
        method = self.spec.get("method", "sitemap")
        dispatch = {
            "sitemap": self._sitemap,
            "api_json": self._api_json,
            "oai_pmh": self._oai_pmh,
            "listing_page": self._listing_page,
        }
        if method not in dispatch:
            log.error("source %s: unknown method %s", self.name, method)
            return
        async for candidate in dispatch[method]():
            yield candidate

    # ------------------------------------------------------------------
    async def _sitemap(self) -> AsyncIterator[CandidateURL]:
        async for url in walk_domain_sitemaps(self, self.spec["domain"],
                                              DOC_EXTENSIONS, delay_s=self.delay):
            yield self._candidate(url)

    async def _api_json(self) -> AsyncIterator[CandidateURL]:
        page = int(self.spec.get("page_start", 0))
        page_param = self.spec.get("page_param", "page")
        base_params = dict(self.spec.get("params") or {})
        empty_pages = 0
        while empty_pages < 2:
            params = {**base_params, page_param: page}
            resp = await self.polite_get(self.spec["url"], params=params,
                                         delay_s=self.delay)
            if resp is None or resp.status_code != 200:
                break
            try:
                payload = resp.json()
            except ValueError:
                break
            urls = _doc_urls_from_json(payload)
            if urls:
                empty_pages = 0
                for url in urls:
                    yield self._candidate(url)
            else:
                empty_pages += 1
            page += 1

    async def _oai_pmh(self) -> AsyncIterator[CandidateURL]:
        base = self.spec["url"]
        token: str | None = None
        while True:
            params = {"verb": "ListRecords"}
            if token:
                params["resumptionToken"] = token
            else:
                params["metadataPrefix"] = "oai_dc"
            resp = await self.polite_get(base, params=params, delay_s=self.delay)
            if resp is None or resp.status_code != 200:
                break
            for match in _OAI_RECORD_RE.finditer(resp.text):
                url = match.group(1)
                if urlsplit(url).path.lower().endswith(DOC_EXTENSIONS):
                    yield self._candidate(url)
            m = _OAI_TOKEN_RE.search(resp.text)
            token = m.group(1).strip() if m else None
            if not token:
                break

    async def _listing_page(self) -> AsyncIterator[CandidateURL]:
        base_url = self.spec["url"]
        max_pages = int(self.spec.get("max_pages", 20))
        page_param = self.spec.get("page_param")
        for page in range(int(self.spec.get("page_start", 1)), max_pages + 1):
            url = base_url if not page_param else f"{base_url}{'&' if '?' in base_url else '?'}{page_param}={page}"
            resp = await self.polite_get(url, delay_s=self.delay)
            if resp is None or resp.status_code != 200:
                break
            found = False
            for match in _DOC_LINK_RE.finditer(resp.text):
                found = True
                yield self._candidate(urljoin(url, match.group(1)))
            if not page_param or not found:
                break

    def _candidate(self, url: str) -> CandidateURL:
        return CandidateURL(url=url, tier=self.tier,
                            discovery_source=self.name,
                            metadata={"source_spec": self.spec.get("name")})


@register
class Tier4Harvester(Harvester):
    """Runs every YAML source spec + the central-banks seed CSV."""

    name = "tier4_international"
    tier = 4

    async def discover(self) -> AsyncIterator[CandidateURL]:
        for spec in self._load_specs():
            sub = GenericSourceHarvester(self.cfg, self.client, spec)
            try:
                async for candidate in sub.discover():
                    yield candidate
            except Exception:
                log.exception("source %s failed; continuing", spec.get("name"))

    def _load_specs(self) -> list[dict]:
        specs: list[dict] = []
        t4 = self.cfg.raw["harvesters"]["tier4"]
        sources_dir = Path(t4["generic_sources_dir"])
        if not sources_dir.is_absolute():
            sources_dir = self.cfg.root / sources_dir
        if sources_dir.exists():
            for path in sorted(sources_dir.glob("*.yaml")):
                try:
                    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if isinstance(spec, dict) and spec.get("name"):
                        specs.append(spec)
                    elif isinstance(spec, list):
                        specs.extend(s for s in spec if isinstance(s, dict) and s.get("name"))
                except yaml.YAMLError as exc:
                    log.error("bad source spec %s: %s", path, exc)

        cb_path = Path(t4["central_banks_seed"])
        if not cb_path.is_absolute():
            cb_path = self.cfg.root / cb_path
        if cb_path.exists():
            with open(cb_path, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    domain = (row.get("domain") or "").strip()
                    if not domain:
                        continue
                    specs.append({
                        "name": f"central_bank_{domain}",
                        "domain": domain,
                        "tier": 4,
                        "method": (row.get("method") or "sitemap").strip(),
                        "url": f"https://{domain}/",
                        "delay_s": 3.0,
                    })
        log.info("tier4: %d source specs loaded", len(specs))
        return specs


def _doc_urls_from_json(payload, _depth: int = 0) -> list[str]:
    urls: list[str] = []
    if _depth > 8:
        return urls
    if isinstance(payload, dict):
        for value in payload.values():
            urls.extend(_doc_urls_from_json(value, _depth + 1))
    elif isinstance(payload, list):
        for item in payload:
            urls.extend(_doc_urls_from_json(item, _depth + 1))
    elif isinstance(payload, str):
        s = payload.strip()
        if s.startswith("http") and urlsplit(s).path.lower().endswith(DOC_EXTENSIONS):
            urls.append(s)
    return urls
