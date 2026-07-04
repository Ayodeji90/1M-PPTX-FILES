"""Identified httpx client. One global UA -- never rotated, never spoofed."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("pptxsweeper.net.client")


def build_client(user_agent: str, connect_timeout: float = 15,
                 read_timeout: float = 90) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent, "Accept": "*/*"},
        timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout,
                              write=60, pool=60),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=300, max_keepalive_connections=100),
        http2=True,
    )


async def fetch_text(client: httpx.AsyncClient, url: str,
                     max_bytes: int = 1_000_000) -> tuple[int | None, str | None]:
    """(status_code, text) with body size cap; (None, None) on network error.
    Used for robots.txt and small metadata fetches."""
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        log.debug("fetch_text %s failed: %s", url, exc)
        return None, None
    body = resp.content[:max_bytes]
    try:
        return resp.status_code, body.decode(resp.encoding or "utf-8", errors="replace")
    except LookupError:
        return resp.status_code, body.decode("utf-8", errors="replace")
