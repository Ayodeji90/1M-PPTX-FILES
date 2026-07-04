"""Seed-domain helpers for domain-scoped Tier 1 harvesting (CDX APIs
cannot do suffix searches, so they need a domain list to walk)."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("pptxsweeper.harvest.seeds")


def tier1_seed_domains(cfg, reg) -> list[str]:
    """Union of the optional seeds/cdx_domains.txt list and every domain
    already known to the registry (discovered by other tiers)."""
    domains: set[str] = set()

    t1 = (cfg.raw.get("harvesters") or {}).get("tier1") or {}
    seeds_file = t1.get("domain_seeds_file")
    if seeds_file:
        path = Path(seeds_file)
        if not path.is_absolute():
            path = cfg.root / path
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = line.split("#", 1)[0].strip().lower()
                if entry:
                    domains.add(entry)

    for row in reg.conn.execute("SELECT DISTINCT domain FROM urls"):
        if row[0]:
            domains.add(row[0].lower().removeprefix("www."))

    return sorted(domains)
