"""GitHub - org members and their public profile emails (free, but rate limited without a token)."""

from __future__ import annotations

import re

from ..config import env
from ..net import HttpError
from ..util import linkedin_url
from .base import Source, api_error, rec

API = "https://api.github.com"
CAPTION_RE = re.compile(r"^(.*?)\s*\(([^)]*)\)\s*(?:·|$)")


def guess_orgs(domain: str) -> "list[str]":
    label = domain.split(".")[0].lower()
    cands = [label]
    if "-" in label:
        cands.append(label.replace("-", ""))
        cands.append(label.replace("-", "_"))
    if "_" in label:
        cands.append(label.replace("_", "-"))
    if label.startswith("get") and len(label) > 5:
        cands.append(label[3:])
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


class GitHub(Source):
    name = "github"
    label = "GitHub"
    free = True

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = env("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def run(self, ctx) -> list:
        token = env("GITHUB_TOKEN")
        headers = self._headers()
        org = ctx.opts.github_org or ""
        out: list = []

        if not org:
            for candidate in guess_orgs(ctx.domain):
                try:
                    data = ctx.http.get_json(f"{API}/orgs/{candidate}", headers=headers)
                    if data and data.get("login"):
                        org = data["login"]
                        ctx.info(f"    github: matched org '{org}' for {ctx.domain}")
                        break
                except HttpError as exc:
                    if exc.status == 403:
                        ctx.info(f"    github: rate limited ({api_error(exc, 'github')})")
                        return out
                    continue
            if not org:
                org = self._search_org(ctx, headers)
                if org:
                    ctx.info(f"    github: found org '{org}' via search")
                else:
                    ctx.info("    github: no org matched (pass --github-org to set it)")
                    return out

        members = self._members(ctx, org, headers)
        if not members:
            ctx.info("    github: org lists no public members (private membership). "
                     "Set GITHUB_TOKEN to see the full member list.")
            return out

        limit = ctx.opts.max_per_source
        if not token and len(members) > 30:
            ctx.info(f"    github: {len(members)} members, capping at 30 without GITHUB_TOKEN")
            members = members[:30]

        nameless = 0
        nameless_logins: "list[str]" = []
        for login in members[:limit]:
            try:
                profile = ctx.http.get_json(f"{API}/users/{login}", headers=headers)
            except HttpError as exc:
                if exc.status == 403:
                    ctx.info("    github: rate limit hit while reading profiles, stopping")
                    break
                continue
            if not profile:
                continue
            # A GitHub login is a handle, not a person's name: "harsh-b2" is not
            # "Harsh". Members without a display name are still kept - as a row that
            # carries the handle - but they are never turned into a guessed address.
            name = (profile.get("name") or "").strip()
            email = (profile.get("email") or "").strip().lower()
            company = profile.get("company") or ""
            notes = [f"github: @{login}"]
            if company:
                notes.append(f"github company: {company[:60]}")
            if email and not email.endswith("@" + ctx.domain):
                notes.append(f"github email (external): {email}")
                email = ""
            # Developers often park their LinkedIn URL in the profile's website field.
            website = (profile.get("blog") or "").strip()
            if website and not website.startswith(("http://", "https://")):
                website = "https://" + website.lstrip("/")
            linkedin = (linkedin_url(profile.get("blog") or "")
                        or linkedin_url(profile.get("bio") or "")
                        or linkedin_url(profile.get("company") or ""))
            if linkedin:
                notes.append("linkedin from github profile")

            if not name:
                nameless += 1
                nameless_logins.append(login)
                notes.append("no display name (login is not a name)")
                out.append(
                    rec(
                        ctx, self.name,
                        full="", email=email, title="", linkedin=linkedin,
                        website=website, github=login,
                        status="name_only", confidence=30, notes=notes,
                    )
                )
                continue

            out.append(
                rec(
                    ctx, self.name,
                    full=name,
                    email=email,
                    title="",
                    linkedin=linkedin,
                    website=website,
                    github=login,
                    status="unverified",
                    confidence=65 if email else 45,
                    notes=notes,
                )
            )
        if nameless:
            sample = ", ".join(f"@{login}" for login in nameless_logins[:12])
            more = "" if nameless <= 12 else f", +{nameless - 12} more"
            ctx.info(f"    github: {nameless} member(s) have no display name - kept as "
                     f"handle-only rows, no address guessed: {sample}{more}")
            ctx.remember("github_nameless_logins", nameless_logins)
        return out

    def _search_org(self, ctx, headers: dict) -> str:
        """Last resort: GitHub org search, accepted only if the profile mentions the domain."""
        label = ctx.domain.split(".")[0]
        try:
            data = ctx.http.get_json(
                f"{API}/search/users",
                params={"q": f"{label} type:org", "per_page": 5},
                headers=headers,
            )
        except HttpError:
            return ""
        for item in (data or {}).get("items") or []:
            login = item.get("login")
            if not login:
                continue
            try:
                org = ctx.http.get_json(f"{API}/orgs/{login}", headers=headers)
            except HttpError:
                continue
            blob = " ".join(str(org.get(key) or "") for key in
                            ("blog", "name", "description", "html_url")).lower()
            if ctx.domain in blob or label in (org.get("login") or "").lower():
                return org.get("login") or login
        return ""

    def _members(self, ctx, org: str, headers: dict) -> "list[str]":
        logins: list = []
        page = 1
        while page <= 10:
            try:
                data = ctx.http.get_json(
                    f"{API}/orgs/{org}/members",
                    params={"per_page": 100, "page": page},
                    headers=headers,
                )
            except HttpError as exc:
                ctx.info(f"    github: member listing failed ({api_error(exc, 'github')})")
                break
            if not isinstance(data, list) or not data:
                break
            logins.extend(str(m.get("login")) for m in data if m.get("login"))
            if len(data) < 100:
                break
            page += 1
        return list(dict.fromkeys(logins))
