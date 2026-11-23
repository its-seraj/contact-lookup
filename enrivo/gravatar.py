"""Gravatar profiles: keyless, keyed on the email address, sometimes carry a LinkedIn account.

`https://gravatar.com/<md5>.json` is public JSON. Coverage in 2026 is low, but it costs one
request per verified address and 404s are cheap, so it runs before any search lookup.
"""

from __future__ import annotations

import hashlib

from .net import HttpError
from .util import linkedin_url

API = "https://gravatar.com/{hash}.json"


def email_hash(email: str) -> str:
    return hashlib.md5(email.strip().lower().encode("utf-8")).hexdigest()


def linkedin_from_entry(entry: dict) -> str:
    """Pull a personal LinkedIn URL out of a Gravatar entry, if it has one."""
    for account in entry.get("accounts") or []:
        domain = str(account.get("domain") or "").lower()
        username = str(account.get("username") or "").strip()
        if "linkedin.com" in domain and username:
            found = linkedin_url(f"linkedin.com/in/{username}")
            if found:
                return found
    for item in entry.get("urls") or []:
        found = linkedin_url(str(item.get("value") or ""))
        if found:
            return found
    return ""


def lookup(ctx, email: str) -> str:
    """LinkedIn URL for an email, or '' (also '' when no Gravatar profile exists)."""
    if not email or "@" not in email:
        return ""
    key = ("gravatar", email.strip().lower())
    cached = ctx.memo(key)
    if cached is not None:
        return cached
    try:
        data = ctx.http.get_json(API.format(hash=email_hash(email)), retries=0)
    except HttpError:
        return ctx.remember(key, "")
    entries = (data or {}).get("entry") or []
    url = linkedin_from_entry(entries[0]) if entries else ""
    return ctx.remember(key, url)
