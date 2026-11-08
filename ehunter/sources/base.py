"""Base class + record factory shared by all sources."""

from __future__ import annotations

from ..config import env
from ..models import Record


class Source:
    """A pluggable place to find people. Subclasses implement run(ctx)."""

    name = "source"
    label = "Source"
    # env vars that must all be present for the source to run
    requires: "tuple[str, ...]" = ()
    # works without any credential
    free = False

    def available(self) -> bool:
        return all(env(v) for v in self.requires)

    def skip_reason(self) -> str:
        missing = [v for v in self.requires if not env(v)]
        if not missing:
            return ""
        return "missing " + ", ".join(missing)

    def run(self, ctx) -> list:  # pragma: no cover - interface
        raise NotImplementedError


def rec(
    ctx,
    source: str,
    first: str = "",
    last: str = "",
    full: str = "",
    email: str = "",
    title: str = "",
    status: str = "unverified",
    confidence: int = 40,
    pattern: str = "",
    linkedin: str = "",
    linkedin_status: str = "",
    github: str = "",
    website: str = "",
    notes=(),
) -> Record:
    """Build a normalised Record stamped with the originating source."""
    r = Record(
        domain=ctx.domain,
        first_name=first or "",
        last_name=last or "",
        full_name=full or "",
        title=title or "",
        email=(email or "").strip().lower(),
        status=status,
        confidence=confidence,
        pattern=pattern,
        linkedin=linkedin or "",
        linkedin_status=linkedin_status or "",
        github=github or "",
        website=website or "",
    )
    r.sources = [source] if source else []
    r.notes = [n for n in notes if n]
    return r.finalize()


def api_error(exc: Exception, who: str) -> str:
    """Turn provider errors into one readable line."""
    status = getattr(exc, "status", 0)
    body = (getattr(exc, "body", "") or "").strip().replace("\n", " ")
    hints = {
        401: "invalid API key",
        402: "account credits exhausted / plan limit",
        403: "forbidden (key scope or plan)",
        404: "endpoint not found (API version changed?)",
        429: "rate limited",
    }
    hint = hints.get(status, "")
    detail = body[:160] if body else str(exc)[:160]
    return f"{who}: HTTP {status} {hint} - {detail}".strip()
