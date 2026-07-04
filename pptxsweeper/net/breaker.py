"""Per-domain circuit breaker. Never fight a server.

State machine:
- any 2xx           -> reset failure streak (and backoff stage)
- 403/429/5xx       -> exponential backoff (30s -> 5min -> 30min)
- 3 consecutive     -> park domain 48h (`parked_until`)
- parked again      -> permanent blacklist
Parked/blacklisted domains route remaining URLs to the Wayback fallback.

State is persisted on the `domains` row so restarts don't forget
punishments; in-memory backoff handles the short stages.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..db.dao import Registry, utcnow

log = logging.getLogger("pptxsweeper.net.breaker")

RETRYABLE_STATUS = frozenset({403, 429} | set(range(500, 600)))


@dataclass
class _DomainState:
    failure_streak: int = 0
    backoff_stage: int = 0
    blocked_until: float = 0.0   # monotonic ts for short backoffs


@dataclass
class CircuitBreaker:
    reg: Registry
    backoff_stages_s: tuple[float, ...] = (30, 300, 1800)
    park_after: int = 3
    park_duration_hours: float = 48
    blacklist_after_parks: int = 2
    _states: dict[str, _DomainState] = field(default_factory=dict)

    def _state(self, domain: str) -> _DomainState:
        return self._states.setdefault(domain, _DomainState())

    # ------------------------------------------------------------------
    def domain_status(self, domain: str) -> str:
        """'ok' | 'backoff' | 'parked' | 'blacklisted'"""
        row = self.reg.get_domain(domain)
        if row:
            if row["state"] == "blacklisted":
                return "blacklisted"
            if row["state"] == "parked":
                if row["parked_until"] and row["parked_until"] <= utcnow():
                    # Park expired: reactivate (streak reset, park_count kept).
                    self.reg.upsert_domain(domain, state="active", failure_streak=0,
                                           parked_until=None)
                else:
                    return "parked"
        if self._state(domain).blocked_until > time.monotonic():
            return "backoff"
        return "ok"

    def backoff_remaining(self, domain: str) -> float:
        return max(0.0, self._state(domain).blocked_until - time.monotonic())

    # ------------------------------------------------------------------
    def record_success(self, domain: str) -> None:
        state = self._state(domain)
        state.failure_streak = 0
        state.backoff_stage = 0
        state.blocked_until = 0.0
        row = self.reg.get_domain(domain)
        if row and row["failure_streak"]:
            self.reg.upsert_domain(domain, failure_streak=0)

    def record_failure(self, domain: str, status_code: int | None) -> str:
        """Register a 403/429/5xx or network failure.
        Returns resulting domain status ('backoff'|'parked'|'blacklisted')."""
        state = self._state(domain)
        state.failure_streak += 1

        row = self.reg.get_domain(domain)
        park_count = row["park_count"] if row else 0
        self.reg.upsert_domain(domain, failure_streak=state.failure_streak)

        if state.failure_streak >= self.park_after:
            if park_count + 1 >= self.blacklist_after_parks:
                # this would be the Nth park (default N=2) -> permanent
                self.reg.upsert_domain(domain, state="blacklisted",
                                       park_count=park_count + 1)
                self.reg.log_event("blacklist", domain,
                                   f"status={status_code}, streak={state.failure_streak}")
                log.warning("domain %s BLACKLISTED (second park)", domain)
                return "blacklisted"
            parked_until = (datetime.now(timezone.utc)
                            + timedelta(hours=self.park_duration_hours))
            self.reg.upsert_domain(
                domain, state="parked", park_count=park_count + 1,
                parked_until=parked_until.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                failure_streak=0,
            )
            state.failure_streak = 0
            state.backoff_stage = 0
            self.reg.log_event("park", domain,
                               f"status={status_code}, until={parked_until.isoformat()}")
            log.warning("domain %s parked until %s", domain, parked_until)
            return "parked"

        stage = min(state.backoff_stage, len(self.backoff_stages_s) - 1)
        delay = self.backoff_stages_s[stage]
        state.backoff_stage += 1
        state.blocked_until = time.monotonic() + delay
        log.info("domain %s backoff %ss (streak %d, status %s)",
                 domain, delay, state.failure_streak, status_code)
        return "backoff"
