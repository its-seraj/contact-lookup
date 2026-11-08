"""Apollo.io - people search by company domain; email reveal is a separate, credited call."""

from __future__ import annotations

from ..config import env
from ..net import HttpError
from .base import Source, api_error, rec

SEARCH_URLS = [
    "https://api.apollo.io/api/v1/mixed_people/api_search",
    "https://api.apollo.io/v1/mixed_people/search",
]
MATCH_URL = "https://api.apollo.io/api/v1/people/match"


class Apollo(Source):
    name = "apollo"
    label = "Apollo.io"
    requires = ("APOLLO_API_KEY",)

    def run(self, ctx) -> list:
        key = env("APOLLO_API_KEY")
        headers = {"x-api-key": key, "Content-Type": "application/json"}
        out: list = []
        people_out: list = []
        page = 1
        try:
            while len(people_out) < ctx.opts.max_per_source:
                data = self._search(ctx, key, headers, page)
                if data is None:
                    break
                people = data.get("people") or data.get("contacts") or []
                if not people:
                    break
                for person in people:
                    people_out.append(person)
                    out.append(self._to_record(ctx, person))
                pagination = data.get("pagination") or {}
                total_pages = pagination.get("total_pages") or 0
                if len(people) < 10 or (total_pages and page >= total_pages):
                    break
                page += 1
        except HttpError as exc:
            if not out:
                raise
            ctx.info(f"    apollo: partial ({api_error(exc, 'apollo')})")

        if ctx.opts.apollo_reveal:
            out.extend(self._reveal(ctx, headers, out, people_out))
        return out

    # ------------------------------------------------------------------
    def _search(self, ctx, key, headers, page: int):
        last_error = None
        for url in SEARCH_URLS:
            body = {
                "q_organization_domains": ctx.domain,
                "page": page,
                "per_page": 100,
            }
            if url.endswith("/mixed_people/search"):
                body["api_key"] = key
            try:
                return ctx.http.post_json(url, body, headers=headers)
            except HttpError as exc:
                last_error = exc
                if exc.status in (403, 404, 405):
                    continue
                raise
        if last_error:
            raise last_error
        return None

    def _to_record(self, ctx, person: dict):
        first = person.get("first_name") or ""
        last = person.get("last_name") or ""
        if not first and not last:
            full = person.get("name") or ""
            if full:
                first, last = full.split(" ", 1) if " " in full else (full, "")
        email = (person.get("email") or "").strip().lower()
        notes = []
        status, conf = "unverified", 50
        if email:
            e_status = (person.get("email_status") or "").lower()
            if e_status in ("verified", "valid"):
                status, conf = "valid", 90
            elif e_status in ("guessed", "unverified"):
                status, conf = "unverified", 50
        else:
            notes.append("apollo: email locked" if person.get("has_email") else "apollo: no email on file")
        return rec(
            ctx, self.name,
            first=first, last=last,
            email=email,
            title=person.get("title") or "",
            linkedin=person.get("linkedin_url") or "",
            status=status,
            confidence=conf,
            notes=notes,
        )

    def _reveal(self, ctx, headers, records, people) -> list:
        """Spend Apollo credits to unlock emails for people that had none."""
        by_index = {}
        for rec_ in records:
            if not rec_.email:
                by_index[rec_.person_key()] = rec_
        if not by_index:
            return []
        out, calls = [], 0
        for person in people:
            if calls >= 100:
                ctx.info("    apollo: reveal capped at 100 calls this run")
                break
            pid = person.get("id")
            if not pid or not person.get("has_email"):
                continue
            try:
                data = ctx.http.post_json(
                    MATCH_URL,
                    {"id": pid, "reveal_personal_emails": True, "reveal_phone_number": False},
                    headers=headers,
                )
                calls += 1
            except HttpError as exc:
                ctx.info(f"    apollo reveal failed: {api_error(exc, 'apollo')}")
                break
            person_data = (data or {}).get("person") or {}
            email = (person_data.get("email") or "").strip().lower()
            if not email:
                continue
            e_status = (person_data.get("email_status") or "").lower()
            out.append(
                rec(
                    ctx, self.name,
                    first=person_data.get("first_name") or person.get("first_name") or "",
                    last=person_data.get("last_name") or person.get("last_name") or "",
                    email=email,
                    title=person_data.get("title") or person.get("title") or "",
                    linkedin=person_data.get("linkedin_url") or person.get("linkedin_url") or "",
                    status="valid" if e_status in ("verified", "valid") else "unverified",
                    confidence=90 if e_status in ("verified", "valid") else 60,
                    notes=["apollo: revealed"],
                )
            )
        if calls:
            ctx.info(f"    apollo: reveal used {calls} credit(s)")
        return out
