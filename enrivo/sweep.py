"""LinkedIn profile sweep: fan out many query variants, then classify each hit.

One `site:linkedin.com/in "domain"` query returns a capped handful of profiles. Different
phrasings of the company name and different modifiers surface almost disjoint result sets
(measured on one domain: 16 unique from the plain dork, 78 from 13 variants). This module
builds the variants and decides whether each hit actually looks like an employee.
"""

from __future__ import annotations

import csv
import html as html_lib
import json
import re
from pathlib import Path

# Tail modifiers. The empty tail is the most productive; the role/location words dig out
# people whose headline uses those words instead of the domain string.
MODIFIERS = [
    "",
    " India",
    " Bangalore",
    " Bengaluru",
    " remote",
    " engineer",
    " software",
    " founder",
    " co-founder",
    " intern",
    " manager",
    " marketing",
    " sales",
    " design",
    " product",
    " AI",
    " operations",
    " growth",
]

PREFIXES = ["at ", "@ ", "building ", "working at ", "ex-", "former "]

_LEGAL_SUFFIX = re.compile(
    r"\b(inc|ltd|llc|gmbh|pvt|private|limited|corp|corporation|technologies|"
    r"technology|solutions|systems|labs|ai)\b\.?",
    re.I,
)
_TITLE_SPLIT = re.compile(r"\s*[|•·:–—]\s*|\s+-\s+")
_SLUG = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)", re.I)


def slug_from_link(link: str) -> str:
    match = _SLUG.search(link or "")
    return match.group(1).lower() if match else ""


def company_names(domain: str, site_title: str = "") -> "list[str]":
    """Names to search for: the site's own title plus the domain label."""
    names: "list[str]" = []
    seen: set = set()

    def add(name: str) -> None:
        cleaned = " ".join((name or "").split()).strip(" -–—|•·")
        cleaned = _LEGAL_SUFFIX.sub("", cleaned).strip(" .,-")
        if not cleaned or len(cleaned) < 3 or len(cleaned) > 40:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        names.append(cleaned)

    if site_title:
        head = _TITLE_SPLIT.split(site_title.strip())[0]
        add(head)
        add(site_title)
    label = domain.split(".")[0]
    add(label)
    # "bamboobox" -> "BambooBox" reads better in a quoted query
    if label.islower():
        add(label[:1].upper() + label[1:])
    return names[:4]


def variants(domain: str, names, max_variants: int = 14) -> "list[str]":
    """Ordered `site:linkedin.com/in "<name>"<modifier>` queries, de-duplicated."""
    names = [name for name in (names or []) if name] or [domain.split(".")[0]]
    out: "list[str]" = []
    seen: set = set()

    def add(query: str) -> None:
        if query.lower() in seen:
            return
        seen.add(query.lower())
        out.append(query)

    # Domain string first: it is the most precise signal.
    add(f'site:linkedin.com/in "{domain}"')
    for name in names:
        add(f'site:linkedin.com/in "{name}"')
    for name in names[:2]:
        for modifier in MODIFIERS[1:]:
            add(f'site:linkedin.com/in "{name}"{modifier}')
        for prefix in PREFIXES:
            if prefix in ("ex-", "former "):
                continue  # those surface non-employees; they cost credits for noise
            add(f'site:linkedin.com/in "{prefix}{name}"')
    return out[: max(1, max_variants)]


FORMER = re.compile(
    r"(ex[\s\-]+|former\s+|formerly\s+|previously\s+)(?P<name>[\w.&' -]{2,30})", re.I
)
CURRENT = re.compile(
    r"(?:\bat\s+|\bwith\s+|@\s*|building\s+|working\s+at\s+)"
    r"(?P<name>[\w.&' -]{2,40})",
    re.I,
)
OPEN_TO_WORK = re.compile(
    r"(open to work|seeking|looking for|#opentowork|job seeker|student at|"
    r"aspiring|graduate|intern at)",
    re.I,
)


def classify(title: str, snippet: str, names) -> "tuple[str, str]":
    """Return (employment, reason) where employment is current/former/mentioned/unclear."""
    text = f"{title or ''} {snippet or ''}"
    low = text.lower()
    names = [name for name in (names or []) if name]
    hit = next((name for name in names if name.lower() in low), "")
    if not hit:
        return "unclear", "company not named"

    name_low = hit.lower()
    former_match = FORMER.search(low)
    if former_match and name_low in former_match.group(0).lower():
        return "former", f'matched "{former_match.group(0).strip()}"'

    # "Current employer" is read from the headline only: snippets contain prose like
    # "spoke at BambooBox" or "guest on the BambooBox podcast", which is not employment.
    for match in CURRENT.finditer((title or "").lower()):
        if match.group("name").lower().startswith(name_low):
            return "current", f'matched "{match.group(0).strip()}"'

    if OPEN_TO_WORK.search(low):
        return "mentioned", "open-to-work / student phrasing"
    if name_low in low:
        return "mentioned", "name appears without an employer phrase"
    return "unclear", ""


def title_parts(title: str) -> "tuple[str, str]":
    """'Jane Doe - VP Sales - Acme | LinkedIn' -> ('Jane Doe', 'VP Sales')."""
    from .names import looks_like_name

    cleaned = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title or "", flags=re.I)
    segments = [part.strip() for part in _TITLE_SPLIT.split(cleaned) if part.strip()]
    if segments and looks_like_name(segments[0]):
        role = segments[1] if len(segments) > 1 else ""
        return segments[0], role[:120]
    return "", ""


CONFIDENCE = {"current": 60, "mentioned": 30, "former": 25, "unclear": 35}
EMPLOYMENT_ORDER = {"current": 0, "unclear": 1, "mentioned": 2, "former": 3}


def site_title(ctx) -> str:
    """og:site_name or <title> of the homepage - the company's own display name."""
    try:
        page = ctx.http.get_text(f"https://{ctx.domain}/",
                                 headers={"Accept": "text/html"}, retries=1)
    except Exception:  # noqa: BLE001
        return ""
    match = re.search(r"""<meta[^>]+property=["']og:site_name["'][^>]+content=["']([^"']+)""",
                      page, re.I)
    if not match:
        match = re.search(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:site_name",
                          page, re.I)
    if not match:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return html_lib.unescape(match.group(1)).strip() if match else ""


def read_profiles(path):
    """Load a previous sweep (CSV or JSON written by this tool) back into profiles."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"linkedin file not found: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("profiles") or [])
    profiles = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            profiles.append({
                "employment": (row.get("employment") or "").strip(),
                "full_name": (row.get("full_name") or "").strip(),
                "title": (row.get("title") or "").strip(),
                "url": (row.get("profile_url") or row.get("url") or "").strip(),
                "slug": (row.get("slug") or "").strip(),
                "evidence": (row.get("evidence") or "").strip(),
                "queries": [q for q in (row.get("queries") or "").split(" | ") if q],
                "snippet": (row.get("snippet") or "").strip(),
            })
    return profiles


def build_records(ctx, profiles, source: str = "linkedin"):
    """Turn sweep hits into pipeline Records (name + profile URL, no email yet)."""
    from .sources.base import rec

    out = []
    for profile in profiles:
        name = profile.get("full_name") or ""
        if not name:
            continue
        employment = profile.get("employment") or "unclear"
        evidence = profile.get("evidence") or ""
        note = f"linkedin: {employment}"
        if evidence:
            note += f" ({evidence})"
        out.append(rec(
            ctx, source,
            full=name,
            title=profile.get("title") or "",
            linkedin=profile.get("url") or "",
            linkedin_status=employment,
            status="name_only",
            confidence=CONFIDENCE.get(employment, 35),
            notes=[note],
        ))
    return out


def sort_profiles(profiles) -> "list[dict]":
    return sorted(
        profiles,
        key=lambda p: (EMPLOYMENT_ORDER.get(p.get("employment"), 9),
                       (p.get("full_name") or "zzz").lower()),
    )


def write_profiles(path, profiles) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["employment", "full_name", "title", "profile_url", "slug",
               "evidence", "queries", "snippet"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for profile in profiles:
            row = dict(profile)
            row["profile_url"] = row.pop("url", "")
            row["queries"] = " | ".join(row.get("queries") or [])
            writer.writerow(row)
    return path


def write_profiles_json(path, domain: str, profiles) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"domain": domain, "profiles": profiles}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
