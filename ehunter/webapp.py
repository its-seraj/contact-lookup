"""Headless entry point used by the Hugging Face Space (and testable without Gradio).

`run_scan()` wraps pipeline.scan() with the limits a shared Space needs: bounded page
counts, bounded search spend, and SMTP detection, because most cloud hosts - Hugging Face
included - block outbound port 25, which makes mailbox verification impossible there.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

from .config import env
from .models import CSV_COLUMNS
from .pipeline import scan
from .report import write_csv
from .util import normalize_domain, now_stamp

# A host that always listens on 25; used only to test whether *we* can reach port 25.
PROBE_HOST = "gmail-smtp-in.l.google.com"
_PORT25: "bool | None" = None
_PORT25_AT = 0.0
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


def port25_available(timeout: float = 3.0) -> bool:
    """True when this host can open an outbound SMTP connection.

    IPv4 is resolved first and the cache expires, because a plain create_connection()
    spends 10s+ walking IPv6 candidates before failing.
    """
    global _PORT25, _PORT25_AT
    if _PORT25 is not None and (time.time() - _PORT25_AT) < PORT25_TTL:
        return _PORT25
    try:
        info = socket.getaddrinfo(PROBE_HOST, 25, socket.AF_INET, socket.SOCK_STREAM)
        family, socktype, proto, _canon, address = info[0]
        with socket.socket(family, socktype, proto) as sock:
            sock.settimeout(timeout)
            sock.connect(address)
        _PORT25 = True
    except OSError:
        _PORT25 = False
    _PORT25_AT = time.time()
    return _PORT25


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
    permute_max: int = 6,
    max_verify: int = 400,
    out_dir: str = "out",
    log=None,
) -> dict:
    """Discover people for `domain` and return rows + a CSV path.

    Raises ValueError for an invalid domain; every other failure is logged and, where
    possible, degraded (verification is disabled when port 25 is unreachable).
    """
    log = log or (lambda *_a, **_k: None)
    domain = normalize_domain(domain)

    if verify and not port25_available():
        log("  [!] outbound port 25 is blocked on this host - email verification "
            "and permutation are disabled (addresses stay unverified guesses)")
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

    result = scan(domain, opts, log=log)

    outdir = Path(out_dir) / domain
    stamp = now_stamp()
    csv_path = write_csv(outdir / f"{domain}_{stamp}.csv", result.records)

    return {
        "domain": domain,
        "rows": [record.to_row() for record in result.records],
        "columns": list(CSV_COLUMNS),
        "csv_path": str(csv_path),
        "meta": result.meta,
        "counts": {
            "people": len(result.records),
            "with_email": sum(1 for r in result.records if r.email),
            "verified": sum(1 for r in result.records if r.status == "valid"),
            "with_linkedin": sum(1 for r in result.records if r.linkedin),
        },
    }
