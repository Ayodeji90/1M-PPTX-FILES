"""Compliance screens run over extracted text during classify.

Per spec (lightweight heuristics, all outcomes recorded per file):
- PII: regex pass -- emails beyond org contact pages, phone numbers,
  SSN-like patterns, ID-number patterns. Hits -> REVIEW, never silent
  rejection.
- Minors/COPPA: keyword+context heuristic. Hits -> REVIEW.
- Prohibited content (adult/violent/extremist): keyword screen.
  Hits -> REJECT with reason.
- Third-party rights: flag-only screen (marketing decks re-using stock
  imagery etc. cannot be detected reliably by regex); records PASS with
  a "not individually reviewed" note unless obvious copyright-notice
  density suggests third-party material -> REVIEW.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

# --- PII ---------------------------------------------------------------
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_GENERIC_EMAIL_PREFIXES = (
    "info@", "contact@", "press@", "media@", "ir@", "investor", "office@",
    "support@", "sales@", "hello@", "admin@", "webmaster@", "enquiries@",
    "communications@", "pr@", "news@",
)
_PHONE_RE = re.compile(
    r"(?<![\d.\-])(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{3}[\s.\-]\d{3,4}[\s.\-]?\d{0,4}(?![\d.])"
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_ID_NUMBER_RE = re.compile(
    r"\b(?:passport|national id|nationalid|driver'?s licen[cs]e|tax id|nino|ssn)\b[^\n]{0,20}?[:#]?\s*[A-Z0-9][A-Z0-9\-]{5,}",
    re.IGNORECASE,
)

# --- Minors / COPPA ----------------------------------------------------
_MINOR_KEYWORDS = re.compile(
    r"\b(children under 13|under-13|coppa|kindergarten|elementary school students|"
    r"pupils?' (names?|records?|data)|student records?|minors?' personal|"
    r"child(?:ren)?'s personal (?:data|information))\b",
    re.IGNORECASE,
)
_MINOR_CONTEXT = re.compile(
    r"\b(name|address|birthdate|date of birth|photo|email|phone)\b", re.IGNORECASE
)

# --- Prohibited --------------------------------------------------------
_PROHIBITED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("adult", re.compile(r"\b(explicit sexual|pornograph\w+|xxx[\s-]rated|escort service)\b",
                         re.IGNORECASE)),
    ("violent", re.compile(r"\b(beheading|gore video|graphic violence|torture footage|snuff)\b",
                           re.IGNORECASE)),
    ("extremist", re.compile(r"\b(jihadist propaganda|white supremac\w+|neo-nazi|"
                             r"terrorist recruitment|extremist manifesto)\b", re.IGNORECASE)),
]

# --- Third-party rights ------------------------------------------------
_RIGHTS_RE = re.compile(
    r"(©|\(c\)|copyright)\s*(?:19|20)\d{2}\s+(?!.*(all rights reserved by (the )?author|"
    r"company confidential))[A-Z][A-Za-z&., ]{3,60}",
)


@dataclass
class ScreenResults:
    pirate: str = "PASS"        # set by filter stage; carried through
    robots: str = "PASS"        # from download stage verdict
    rights: str = "PASS"
    pii: str = "PASS"
    minors: str = "PASS"
    prohibited: str = "PASS"
    details: dict = field(default_factory=dict)

    @property
    def forces_review(self) -> bool:
        return "REVIEW" in (self.rights, self.pii, self.minors)

    @property
    def forces_reject(self) -> bool:
        return self.prohibited == "REJECT" or self.pirate == "REJECT" \
            or self.robots == "REJECT"

    def to_dict(self) -> dict:
        return asdict(self)


def _screen_pii(text: str) -> tuple[str, str]:
    personal_emails = [
        e for e in _EMAIL_RE.findall(text)
        if not any(e.lower().startswith(p) or p in e.lower() for p in _GENERIC_EMAIL_PREFIXES)
    ]
    ssns = _SSN_RE.findall(text)
    ids = _ID_NUMBER_RE.findall(text)
    phones = _PHONE_RE.findall(text)
    hits = []
    if ssns:
        hits.append(f"{len(ssns)} SSN-like patterns")
    if ids:
        hits.append(f"{len(ids)} ID-number patterns")
    if len(personal_emails) > 3:
        hits.append(f"{len(personal_emails)} non-organizational email addresses")
    if len(phones) > 10:
        hits.append(f"{len(phones)} phone-number-like strings")
    if hits:
        return "REVIEW", "; ".join(hits)
    return "PASS", ""


def _screen_minors(text: str) -> tuple[str, str]:
    kw_hits = _MINOR_KEYWORDS.findall(text)
    if not kw_hits:
        return "PASS", ""
    # keyword + context: child-focused terms near personal-data terms
    if _MINOR_CONTEXT.search(text):
        return "REVIEW", f"child-focused keywords with personal-data context: {kw_hits[:5]}"
    return "PASS", f"child-related keywords without personal-data context: {kw_hits[:5]}"


def _screen_prohibited(text: str) -> tuple[str, str]:
    for category, pattern in _PROHIBITED_PATTERNS:
        m = pattern.search(text)
        if m:
            return "REJECT", f"prohibited content ({category}): matched {m.group(0)!r}"
    return "PASS", ""


def _screen_rights(text: str) -> tuple[str, str]:
    matches = _RIGHTS_RE.findall(text)
    if len(matches) >= 5:
        return "REVIEW", f"{len(matches)} third-party copyright notices detected"
    return "PASS", "no dense third-party copyright signals; not individually reviewed"


def run_screens(full_text: str, robots_status: str | None = None,
                pirate_hit: str | None = None) -> ScreenResults:
    results = ScreenResults()
    if pirate_hit:
        results.pirate = "REJECT"
        results.details["pirate"] = f"blocklist match: {pirate_hit}"
    if robots_status == "disallowed":
        results.robots = "REJECT"
        results.details["robots"] = "robots.txt disallowed"
    elif robots_status == "unavailable":
        results.robots = "PASS"
        results.details["robots"] = "robots.txt unavailable at fetch time (treated as allow)"

    results.pii, pii_detail = _screen_pii(full_text)
    if pii_detail:
        results.details["pii"] = pii_detail
    results.minors, minors_detail = _screen_minors(full_text)
    if minors_detail:
        results.details["minors"] = minors_detail
    results.prohibited, prohibited_detail = _screen_prohibited(full_text)
    if prohibited_detail:
        results.details["prohibited"] = prohibited_detail
    results.rights, rights_detail = _screen_rights(full_text)
    results.details["rights"] = rights_detail
    return results
