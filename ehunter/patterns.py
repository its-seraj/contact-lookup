"""Email-pattern inference and generation.

Given a handful of (name, email) pairs harvested for a domain, find which local-part
convention explains them, then apply that convention to names whose email is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

from .names import email_localpart, slug

# Ordered: earlier entries win ties, so the most common corporate conventions come first.
PATTERNS = [
    "{first}.{last}",   # john.smith
    "{first}{last}",    # johnsmith
    "{f}{last}",        # jsmith
    "{first}",          # john
    "{last}",           # smith
    "{f}.{last}",       # j.smith
    "{first}_{last}",   # john_smith
    "{last}.{first}",   # smith.john
    "{last}{first}",    # smithjohn
    "{first}-{last}",   # john-smith
    "{f}-{last}",       # j-smith
    "{first}{l}",       # johns
    "{last}{f}",        # smithj
    "{first}.{l}",      # john.s
    "{last}_{first}",   # smith_john
    "{f}{l}",           # js
]

# Domains where free-mail conventions tell you nothing about the corporate convention.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net", "web.de",
    "yandex.ru", "mail.ru", "qq.com", "163.com", "126.com", "zoho.com",
}

KNOWN_PATTERNS = set(PATTERNS) | {"{first}{last}", "{last}", "{first}"}


@dataclass
class PatternHit:
    pattern: str
    matched: int
    total: int

    @property
    def ratio(self) -> float:
        return (self.matched / self.total) if self.total else 0.0

    @property
    def confidence(self) -> int:
        """0-100: coverage of observed pairs, damped when the sample is tiny."""
        if not self.total:
            return 0
        base = self.ratio * 100
        sample = min(self.total, 5)
        return int(round(base * (0.6 + 0.08 * sample)))


def render(pattern: str, first: str, last: str) -> str:
    """Render a pattern to a local part. Returns '' when required name parts are missing."""
    f, l = slug(first), slug(last)
    if not f:
        return ""
    needs_last = "{last}" in pattern or "{l}" in pattern
    if needs_last and not l:
        return ""
    try:
        return pattern.format(
            first=f,
            last=l,
            f=f[0],
            l=(l[0] if l else ""),
        )
    except (KeyError, IndexError, ValueError):
        return ""


def build(pattern: str, first: str, last: str, domain: str) -> str:
    local = render(pattern, first, last)
    return f"{local}@{domain}".lower() if local else ""


def infer(pairs) -> "list[PatternHit]":
    """Score every known pattern against observed (first, last, email) triples."""
    usable = []
    for first, last, email in pairs:
        if not email or "@" not in email:
            continue
        dom = email.rsplit("@", 1)[1].lower()
        if dom in FREEMAIL:
            continue
        local = email_localpart(email)
        if not local:
            continue
        usable.append((first, last, local))
    if not usable:
        return []

    hits = []
    for pattern in PATTERNS:
        matched = 0
        for first, last, local in usable:
            if render(pattern, first, last) == local:
                matched += 1
        if matched:
            hits.append(PatternHit(pattern, matched, len(usable)))
    hits.sort(key=lambda h: (-h.matched, PATTERNS.index(h.pattern)))
    return hits


def best_pattern(pairs) -> str:
    hits = infer(pairs)
    return hits[0].pattern if hits else ""


def explain(pattern: str) -> str:
    """Human-readable pattern label, e.g. first.last -> 'First.Last'."""
    return (
        pattern.replace("{first}", "First")
        .replace("{last}", "Last")
        .replace("{f}", "F")
        .replace("{l}", "L")
    )
