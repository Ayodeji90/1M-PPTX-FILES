"""Common harvester interface + async fetch helpers with politeness."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from ..config import Config
from ..db.dao import Registry
from ..node import NodeIdentity

log = logging.getLogger("pptxsweeper.harvest")

PRESENTATION_EXTENSIONS = (".pptx", ".ppt")
DOC_EXTENSIONS = PRESENTATION_EXTENSIONS   # client decision: ppt/pptx only, no pdf


@dataclass
class CandidateURL:
    url: str
    domain: str = ""
    tier: int = 0
    discovery_source: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.domain:
            self.domain = urlsplit(self.url).netloc.lower()

    def to_row(self) -> dict:
        return {"url": self.url, "domain": self.domain, "tier": self.tier,
                "discovery_source": self.discovery_source, "metadata": self.metadata}


class Harvester:
    """Base class. Subclasses set `name` and `tier`, implement discover()."""

    name: str = ""
    tier: int = 0

    def __init__(self, cfg: Config, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client
        self.node = NodeIdentity.from_env()

    # -- multi-node discovery sharding (see NodeIdentity) -----------------
    def owns_domain(self, domain: str) -> bool:
        """Domain-list sources: only walk domains this node owns."""
        return self.node.owns_domain(domain.lower().removeprefix("www."))

    def owns_page(self, index: int) -> bool:
        """Global paginated sources: only fetch pages this node owns."""
        return self.node.owns_page(index)

    async def discover(self) -> AsyncIterator[CandidateURL]:  # pragma: no cover
        raise NotImplementedError
        yield  # makes this an async generator in the type sense

    # ------------------------------------------------------------------
    async def polite_get(self, url: str, delay_s: float = 1.0, retries: int = 3,
                         **kwargs) -> httpx.Response | None:
        """GET with a fixed pre-request delay and 429/5xx backoff."""
        for attempt in range(retries):
            await asyncio.sleep(delay_s)
            try:
                resp = await self.client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                log.debug("%s: GET %s failed (%s)", self.name, url, exc)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(min(30 * (2 ** attempt), 300))
                continue
            return resp
        return None


_REGISTRY: dict[str, type[Harvester]] = {}


def register(cls: type[Harvester]) -> type[Harvester]:
    if not cls.name:
        raise ValueError(f"harvester {cls} has no name")
    _REGISTRY[cls.name] = cls
    return cls


def get_harvester(name: str) -> type[Harvester]:
    return _REGISTRY[name]


def all_harvesters(tier: int | None = None) -> dict[str, type[Harvester]]:
    if tier is None:
        return dict(_REGISTRY)
    return {n: c for n, c in _REGISTRY.items() if c.tier == tier}


async def _run_one(cfg: Config, reg: Registry, client, name: str,
                   limit: int | None) -> dict:
    harvester = get_harvester(name)(cfg, client)
    found = inserted = 0
    buffer: list[dict] = []
    try:
        async for candidate in harvester.discover():
            found += 1
            buffer.append(candidate.to_row())
            if len(buffer) >= 200:
                inserted += reg.upsert_candidates(buffer)
                buffer = []
            if limit and found >= limit:
                break
    except Exception:
        log.exception("harvester %s crashed; keeping %d found so far", name, found)
    inserted += reg.upsert_candidates(buffer)
    log.info("harvester %s: found=%d new=%d", name, found, inserted)
    return {"found": found, "new": inserted}


async def run_harvesters(cfg: Config, reg: Registry, names: list[str],
                         limit_per_source: int | None = None) -> dict:
    """Run the named harvesters CONCURRENTLY (each source owns different
    domains, so politeness is preserved) and upsert candidates."""
    from ..net.client import build_client
    client = build_client(cfg.user_agent)
    try:
        results = await asyncio.gather(
            *(_run_one(cfg, reg, client, n, limit_per_source) for n in names),
            return_exceptions=True,
        )
    finally:
        await client.aclose()
    stats: dict[str, dict] = {}
    for name, result in zip(names, results):
        stats[name] = result if isinstance(result, dict) else {"error": str(result)}
    return stats
