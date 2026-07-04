"""Harvester plugin registry.

Every harvester conforms to:  discover() -> yields CandidateURL
Harvesters only WRITE CANDIDATES to the registry; none download files.
"""
from __future__ import annotations

from .base import CandidateURL, Harvester, register, get_harvester, all_harvesters

# Importing modules registers their plugins.
from . import tier1_commoncrawl   # noqa: F401
from . import tier1_wayback       # noqa: F401
from . import tier2_ir            # noqa: F401
from . import tier2_edgar         # noqa: F401
from . import tier3_federal      # noqa: F401
from . import tier4_generic       # noqa: F401
from . import tier5_edu           # noqa: F401
from . import tier6_repos         # noqa: F401

__all__ = ["CandidateURL", "Harvester", "register", "get_harvester", "all_harvesters"]
