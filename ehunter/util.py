"""Small shared helpers: domain normalisation, email parsing, timestamps."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?!-)[a-z0-9\-]{1,63}(\.[a-z0-9\-]{1,63})+$"
)

LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]+",
    re.I,
)


def linkedin_url(text: str) -> str:
    """Extract and normalise the first personal LinkedIn URL in a string ('' if none)."""
    if not text:
        return ""
    match = LINKEDIN_RE.search(str(text))
    if not match:
        return ""
    raw = match.group(0).strip()
    profile = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%.]+)", raw, re.I)
    if not profile:
        return ""
    # Canonical form so http/https, www and tracking-parameter variants dedupe.
    return "https://www.linkedin.com/in/" + profile.group(1).lower()


def linkedin_slug(url: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url or "", re.I)
    return match.group(1).lower() if match else ""


def linkedin_matches_name(url: str, first: str, last: str) -> bool:
    """Is this profile slug plausibly the same person? (last+first both present)"""
    from .names import slug

    path = slug(linkedin_slug(url))
    if not path:
        return False
    f, l = slug(first), slug(last)
    if f and l:
        return f in path and l in path
    needle = f or l
    return bool(needle and needle in path)



def normalize_domain(raw: str) -> str:
    """Accept 'https://www.Example.com/team?x=1' or 'Example.com' and return 'example.com'."""
    if not raw:
        raise ValueError("empty domain")
    d = raw.strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", d)   # scheme
    d = d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    d = d.split("@")[-1]                            # user@host
    d = d.split(":", 1)[0]                          # port
    d = d.strip().rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    # IDN -> punycode
    try:
        d = d.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass
    if not DOMAIN_RE.match(d):
        raise ValueError(f"not a valid registrable domain: {raw!r}")
    if d.endswith((".local", ".internal", ".test", ".invalid", ".example")):
        raise ValueError(f"not a public domain: {d!r}")
    return d


def registrable(domain: str) -> str:
    """Rough eTLD+1 (enough for 'is this link on-site' checks)."""
    parts = domain.lower().split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    two_level = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "co.nz",
                 "co.jp", "or.jp", "ne.jp", "com.br", "com.cn", "com.tr", "co.in",
                 "co.za", "com.mx", "com.sg", "com.hk", "co.kr", "com.ar", "com.pl"}
    tail2 = ".".join(parts[-2:])
    if tail2 in two_level and len(parts) >= 3:
        return ".".join(parts[-3:])
    return tail2


def is_email(value: str) -> bool:
    return bool(value) and bool(EMAIL_RE.fullmatch(value.strip()))


def find_emails(text: str, domain: str = "") -> "list[str]":
    """Extract, normalise and de-duplicate emails from free text; optionally filter by domain."""
    out, seen = [], set()
    for raw in EMAIL_RE.findall(text or ""):
        e = raw.strip().strip(".").lower()
        if e.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")):
            continue
        if len(e) > 2 and e in seen:
            continue
        if domain and not e.endswith("@" + domain.lower()):
            continue
        seen.add(e)
        out.append(e)
    return out


def parse_email_input(raw: str) -> "list[str]":
    """Accept a comma/space/newline separated list, a file path, or '-' for stdin."""
    import sys

    text = raw or ""
    p = Path(text)
    if text and text != "-" and "\n" not in text and len(text) < 260 and p.is_file():
        text = p.read_text(encoding="utf-8-sig", errors="ignore")
    elif text == "-":
        text = sys.stdin.read()
    found = find_emails(text)
    if not found:
        found = [t.strip().lower() for t in re.split(r"[,\s]+", text) if is_email(t.strip())]
    return list(dict.fromkeys(found))


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_name(text: str, default: str = "output") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
    return s.strip("._-")[:80] or default
