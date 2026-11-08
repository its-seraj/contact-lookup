"""Source registry."""

from __future__ import annotations

from .apollo import Apollo
from .base import Source, rec
from .github import GitHub
from .hunter import Hunter
from .pgp import Pgp
from .prospeo import Prospeo
from .search import Search
from .snov import Snov
from .web import Web

# Insertion order is the display/report order.
ALL = {
    cls.name: cls
    for cls in (Hunter, Apollo, Snov, Prospeo, GitHub, Pgp, Web, Search)
}

# Default set: everything that can run without a key, plus keyed sources when configured.
DEFAULT = "hunter,apollo,snov,prospeo,github,pgp,web,search"

FREE = tuple(name for name, cls in ALL.items() if cls.free)


def build_sources(spec: str) -> "list[Source]":
    names = [n.strip().lower() for n in (spec or DEFAULT).split(",") if n.strip()]
    if not names or "all" in names:
        names = list(ALL)
    if "free" in names:
        # keyless-only preset: drop "free" and any keyed source
        names = [n for n in names if n != "free"] + [
            n for n in FREE if n not in names
        ]
    unknown = [n for n in names if n not in ALL]
    if unknown:
        raise ValueError(
            f"unknown source(s): {', '.join(unknown)}. valid: {', '.join(ALL)}"
        )
    return [ALL[n]() for n in dict.fromkeys(names)]


def describe() -> "list[dict]":
    out = []
    for name, cls in ALL.items():
        src = cls()
        out.append({
            "name": name,
            "label": cls.label,
            "requires": list(cls.requires),
            "available": src.available(),
            "reason": src.skip_reason(),
        })
    return out
