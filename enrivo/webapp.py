"""Headless entry point used by the Hugging Face Space (and testable without Gradio).

`run_scan()` wraps pipeline.scan() with the limits a shared Space needs: bounded page
counts, bounded search spend, and SMTP detection, because most cloud hosts - Hugging Face
included - block outbound port 25, which makes mailbox verification impossible there.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import smtp
from .config import env
from .models import CSV_COLUMNS
from .pipeline import scan
from .report import write_csv
from .util import normalize_domain, now_stamp

# A host that always listens on 25; used only to test whether *we* can reach port 25.
PROBE_HOST = "gmail-smtp-in.l.google.com"
_PORT25: "bool | None" = None
_PORT25_AT = 0.0
_PORT25_WHY = ""
PORT25_TTL = 300

DISPLAY_COLUMNS = [
    "full_name",
    "title",
    "email",
    "email_status",
    "confidence",
    "linkedin",
    "linkedin_status",
    "github",
    "sources",
]


def port25_available(timeout: float = 8.0) -> bool:
    """True when this host can open an outbound SMTP connection.

    Tries IPv6 and IPv4 (a network may block only one) and caches the answer briefly.
    """
    global _PORT25, _PORT25_AT, _PORT25_WHY
    if _PORT25 is not None and (time.time() - _PORT25_AT) < PORT25_TTL:
        return _PORT25
    # Goes through SMTP_PROXY when one is configured, so a working tunnel counts as "open".
    _PORT25, _PORT25_WHY = smtp.check_port25(PROBE_HOST, timeout)
    _PORT25_AT = time.time()
    return _PORT25


def port25_problem() -> str:
    """Why the last port-25 check failed, worded for the person who has to fix it."""
    if smtp.proxy_settings() is not None:
        return (f"the SMTP proxy ({env('SMTP_PROXY')}) could not reach port 25: "
                f"{_PORT25_WHY}. Is the tunnel up (ssh -D) and does its host allow port 25?")
    return ("outbound port 25 is blocked on this host. Switch networks, or set SMTP_PROXY "
            "to a SOCKS5 tunnel through a host that allows port 25.")


def port25_cached() -> "bool | None":
    """Last probe result without triggering a new one (health endpoints)."""
    return _PORT25


def run_scan(
    domain: str,
    *,
    linkedin: bool = False,
    verify: bool = False,
    max_pages: int = 8,
    sweep_variants: int = 10,
    sweep_depth: int = 3,
    permute_max: int = 19,
    max_verify: int = 400,
    out_dir: str = "out",
    log=None,
    progress=None,
) -> dict:
    """Discover people for `domain` and return rows + a CSV path.

    Raises ValueError for an invalid domain; every other failure is logged and, where
    possible, degraded (verification is disabled when port 25 is unreachable).
    """
    log = log or (lambda *_a, **_k: None)
    domain = normalize_domain(domain)

    warnings: "list[str]" = []
    if verify and not port25_available():
        message = (f"email verification and permutation did not run, so people without a "
                   f"published address have no email: {port25_problem()}")
        log(f"  [!] {message}")
        warnings.append(message)
        verify = False

    from .context import Options

    opts = Options(
        sources="",                     # default source set
        max_pages=max(1, min(int(max_pages), 25)),
        search_pages=2,
        linkedin_sweep=bool(linkedin),
        sweep_variants=max(1, min(int(sweep_variants), 20)),
        sweep_depth=max(1, min(int(sweep_depth), 5)),
        verify=bool(verify),
        permute=bool(verify),
        permute_max=max(1, min(int(permute_max), 19)),
        max_verify=max(50, min(int(max_verify), 1000)),
        from_addr=env("VERIFY_FROM") or "noreply@example.com",
        delay=0.3,
        timeout=15.0,
        verify_timeout=8.0,
    )

    result = scan(domain, opts, log=log, progress=progress)

    outdir = Path(out_dir) / domain
    stamp = now_stamp()
    csv_path = write_csv(outdir / f"{domain}_{stamp}.csv", result.records)

    return {
        "domain": domain,
        "rows": [record.to_row() for record in result.records],
        "columns": list(CSV_COLUMNS),
        "csv_path": str(csv_path),
        "meta": result.meta,
        "verify_ran": bool(verify),     # False when it was requested but port 25 is blocked
        "warnings": warnings,
        "counts": {
            "people": len(result.records),
            "with_email": sum(1 for r in result.records if r.email),
            "verified": sum(1 for r in result.records if r.status == "valid"),
            "with_linkedin": sum(1 for r in result.records if r.linkedin),
        },
    }
