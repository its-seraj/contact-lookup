"""Request-level logic shared by the HTTP API (and reusable by any other front end).

Kept free of FastAPI and pymongo so it can be unit-tested without a server or a database.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Tuple

from .util import normalize_domain
from .webapp import run_scan

MAX_DOMAINS = 10


@dataclass(frozen=True)
class ScanFlags:
    """What the caller asked for. Mirrors the JSON body's isVerify / isLinkedin."""

    is_verify: bool = False
    is_linkedin: bool = True
    permute_max: int = 19
    max_pages: int = 8
    sweep_variants: int = 10

    def as_dict(self) -> dict:
        return {"isVerify": self.is_verify, "isLinkedin": self.is_linkedin}


def normalize_domains(raw, limit: int = MAX_DOMAINS) -> "Tuple[List[str], List[dict]]":
    """Accept a list or a comma/space separated string. Returns (domains, errors)."""
    if raw is None:
        items: "List[str]" = []
    elif isinstance(raw, str):
        items = re.split(r"[\s,;]+", raw)
    elif isinstance(raw, Iterable):
        items = [str(item) for item in raw]
    else:
        items = [str(raw)]

    domains: "List[str]" = []
    errors: "List[dict]" = []
    seen: set = set()
    for item in items:
        text = (item or "").strip()
        if not text:
            continue
        try:
            domain = normalize_domain(text)
        except ValueError as exc:
            errors.append({"input": text, "error": str(exc)})
            continue
        if domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)

    if len(domains) > limit:
        for dropped in domains[limit:]:
            errors.append({"input": dropped, "error": f"over the limit of {limit} domains per request"})
        domains = domains[:limit]
    return domains, errors


def cache_covers(cached, flags: ScanFlags) -> bool:
    """A cached document is reusable when it was produced with at least these flags.

    A LinkedIn-disabled run cannot answer a LinkedIn-enabled request, so that domain is
    rescanned instead of serving a thinner list.
    """
    if not cached:
        return False
    stored = cached.get("flags") or {}
    if bool(stored.get("isLinkedin")) < bool(flags.is_linkedin):
        return False
    if bool(stored.get("isVerify")) < bool(flags.is_verify):
        return False
    return bool(cached.get("rows") is not None)


def build_doc(domain: str, result: dict, flags: ScanFlags, scan_ms: "int | None" = None) -> dict:
    """The document stored in MongoDB / returned in a `result` event.

    `flags` records what actually ran, not what was asked for: a verify request that was
    silently downgraded (port 25 blocked) must not be cached as a verified scan, or later
    requests would be served the email-less result instead of triggering a rescan.
    """
    effective = ScanFlags(
        is_verify=flags.is_verify and bool(result.get("verify_ran", True)),
        is_linkedin=flags.is_linkedin,
    )
    return {
        "domain": domain,
        "rows": result.get("rows") or [],
        "meta": result.get("meta") or {},
        "counts": result.get("counts") or {},
        "warnings": list(result.get("warnings") or []),
        "flags": effective.as_dict(),
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scan_ms": int(scan_ms if scan_ms is not None else result.get("scan_ms") or 0),
    }


def run_domain(domain: str, flags: ScanFlags, log=None, progress=None) -> dict:
    """Blocking scan for one domain (called from a worker thread)."""
    started = time.time()
    result = run_scan(
        domain,
        linkedin=flags.is_linkedin,
        verify=flags.is_verify,
        permute_max=flags.permute_max,
        max_pages=flags.max_pages,
        sweep_variants=flags.sweep_variants,
        log=log,
        progress=progress,
    )
    result["scan_ms"] = int((time.time() - started) * 1000)
    return result
