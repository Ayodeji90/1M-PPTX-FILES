"""Pre-download filtering: pirate/exclusion blocklists, extension
sanity, per-domain caps. URL dedup is structural (urls.url UNIQUE)."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from ..compliance.blocklist import Blocklist
from ..config import Config
from ..db.dao import Registry

log = logging.getLogger("pptxsweeper.filter")

# Sources that verify the file format at DISCOVERY time (by MIME, API
# filetype field, or repository file key) rather than by URL suffix.
# Their download URLs are frequently extensionless (Figshare/OSF use
# opaque /files/<id> routes; Wayback captures CMS /download?id= paths),
# so the URL-suffix gate would wrongly drop them. The download stage's
# magic-byte check is the authoritative format guard for these.
_FORMAT_VERIFIED_SOURCES = (
    "wayback", "zenodo", "figshare", "osf", "internet_archive", "ietf",
    "govdata",
)


def _extension_ok(url: str, extensions: tuple[str, ...]) -> bool:
    path = urlsplit(url).path.lower().split("?", 1)[0]
    return path.endswith(extensions)


def _format_pre_verified(discovery_source: str) -> bool:
    return discovery_source.startswith(_FORMAT_VERIFIED_SOURCES)


def run_filter(cfg: Config, reg: Registry) -> dict:
    extensions = tuple("." + f.lstrip(".")
                       for f in cfg.raw.get("allowed_formats", ["pptx", "ppt"]))
    blocklist = Blocklist.load(cfg.path("compliance", "blocklist_file"))
    excluded = Blocklist.load(cfg.path("compliance", "excluded_sources_file")) \
        if cfg.raw["compliance"].get("excluded_sources_file") else None
    per_domain_cap = int(cfg.raw["filter"]["per_domain_cap"])
    tier_caps = {int(k): int(v) for k, v in
                 (cfg.raw["filter"].get("tier_domain_caps") or {}).items()}

    stats = {"scanned": 0, "blocklisted": 0, "bad_extension": 0, "over_cap": 0, "kept": 0}
    domain_counts: dict[str, int] = {
        row["domain"]: row["n"] for row in reg.conn.execute(
            "SELECT domain, COUNT(*) AS n FROM urls "
            "WHERE status NOT IN ('discovered','filtered_out') GROUP BY domain"
        )
    }

    updates: list[tuple[int, dict]] = []
    for row in reg.conn.execute(
        "SELECT id, url, domain, tier, discovery_source FROM urls "
        "WHERE status='discovered' ORDER BY id"
    ).fetchall():
        stats["scanned"] += 1
        # Malformed URLs (no scheme / no host -- e.g. a portal returning
        # relative resource paths) can never be fetched and must not burn
        # download attempts. Rejected here, once, instead of 4 retries
        # of exception noise in the downloader.
        parsed = urlsplit(row["url"])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            updates.append((row["id"], {"status": "filtered_out",
                                        "reject_reason": "malformed_url"}))
            stats["malformed_url"] = stats.get("malformed_url", 0) + 1
            continue
        hit = blocklist.blocked(row["url"])
        if hit:
            updates.append((row["id"], {"status": "filtered_out",
                                        "reject_reason": f"blocklist:{hit}"}))
            stats["blocklisted"] += 1
            continue
        excl = excluded.blocked(row["url"]) if excluded else None
        if excl:
            updates.append((row["id"], {"status": "filtered_out",
                                        "reject_reason": f"excluded_source:{excl}"}))
            stats["excluded_source"] = stats.get("excluded_source", 0) + 1
            continue
        if not _format_pre_verified(row["discovery_source"]) \
                and not _extension_ok(row["url"], extensions):
            updates.append((row["id"], {"status": "filtered_out",
                                        "reject_reason": "extension"}))
            stats["bad_extension"] += 1
            continue
        cap = tier_caps.get(row["tier"], per_domain_cap)
        count = domain_counts.get(row["domain"], 0)
        if count >= cap:
            updates.append((row["id"], {"status": "filtered_out",
                                        "reject_reason": f"domain_cap:{cap}"}))
            stats["over_cap"] += 1
            continue
        domain_counts[row["domain"]] = count + 1
        stats["kept"] += 1

        if len(updates) >= 500:
            reg.update_urls(updates)
            updates = []

    reg.update_urls(updates)
    log.info("filter done: %s", stats)
    return stats
