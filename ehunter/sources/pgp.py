"""PGP keyservers - public keys carry the developer's real name and work email in the UID."""

from __future__ import annotations

import re

from ..net import HttpError
from .base import Source, api_error, rec

KEYSERVERS = [
    "https://keyserver.ubuntu.com/pks/lookup",
    "https://keyserver.pgp.com/vkd/GetKeysByEmailID",
]

EMAIL_IN_UID = re.compile(r"<([^>]+)>")
PAREN = re.compile(r"\(([^)]*)\)")
_HEX = re.compile(r"\\x([0-9A-Fa-f]{2})")


def unescape_mr(text: str) -> str:
    """SKS machine-readable output escapes ':' as \\x3a, '%' as \\x25, '\\' as \\x5c."""
    return _HEX.sub(lambda m: chr(int(m.group(1), 16)), text)


def parse_mr_keys(text: str) -> "list[tuple[str, str]]":
    """Return [(display_name, email)] from a machine-readable key listing.

    SKS/gpg machine-readable records put the UID in the last colon-separated field
    (colons inside the UID are escaped), regardless of how many metadata fields the
    keyserver version emits.
    """
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("uid:"):
            continue
        fields = line.split(":")
        if len(fields) < 3:
            continue
        uid = unescape_mr(fields[-1]).strip()
        if not uid:
            continue
        email_m = EMAIL_IN_UID.search(uid)
        if not email_m:
            continue
        email = email_m.group(1).strip().lower()
        name = uid[: email_m.start()].strip(" ,;")
        name = PAREN.sub("", name).strip()
        out.append((name, email))
    return out


class Pgp(Source):
    name = "pgp"
    label = "PGP keyservers"
    free = True

    def run(self, ctx) -> list:
        out: list = []
        try:
            text = ctx.http.get_text(
                KEYSERVERS[0],
                params={"op": "index", "search": f"@{ctx.domain}", "options": "mr"},
                headers={"Accept": "text/plain"},
            )
        except HttpError as exc:
            # The SKS keyserver answers 404 when no key matches the domain.
            if exc.status == 404:
                return out
            ctx.info(f"    pgp: keyserver unavailable ({api_error(exc, 'pgp')})")
            return out

        wanted = "@" + ctx.domain
        seen = set()
        for name, email in parse_mr_keys(text):
            if not email.endswith(wanted) or email in seen:
                continue
            seen.add(email)
            out.append(
                rec(
                    ctx, self.name,
                    full=name,
                    email=email,
                    status="unverified",
                    confidence=70,
                    notes=["pgp uid (self-published)"],
                )
            )
        if out:
            ctx.info(f"    pgp: {len(out)} key uid(s) on {ctx.domain}")
        return out
