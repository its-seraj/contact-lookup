"""Snov.io - domain-emails-with-info v2 (OAuth2 client_credentials)."""

from __future__ import annotations

from ..config import env
from ..net import HttpError
from .base import Source, api_error, rec

TOKEN_URL = "https://api.snov.io/v1/oauth/access_token"
DOMAIN_URL = "https://api.snov.io/v2/domain-emails-with-info"


class Snov(Source):
    name = "snov"
    label = "Snov.io"
    requires = ("SNOV_CLIENT_ID", "SNOV_CLIENT_SECRET")

    def _token(self, ctx) -> str:
        cached = ctx.memo("snov_token")
        if cached:
            return cached
        data = ctx.http.post_json(
            TOKEN_URL,
            {
                "grant_type": "client_credentials",
                "client_id": env("SNOV_CLIENT_ID"),
                "client_secret": env("SNOV_CLIENT_SECRET"),
            },
        )
        token = (data or {}).get("access_token")
        if not token:
            raise HttpError(401, TOKEN_URL, f"no access_token in response: {data}")
        return ctx.remember("snov_token", token)

    def run(self, ctx) -> list:
        token = self._token(ctx)
        out: list = []
        last_id = None
        seen_ids = set()
        try:
            while len(out) < ctx.opts.max_per_source:
                body = {
                    "access_token": token,
                    "domain": ctx.domain,
                    "type": "all",
                    "limit": 100,
                }
                if last_id:
                    body["lastId"] = last_id
                data = ctx.http.post_json(DOMAIN_URL, body)
                if not data or data.get("success") is False:
                    ctx.info(f"    snov: {str(data)[:160]}")
                    break
                items = data.get("data") or []
                if not items:
                    break
                for item in items:
                    email = (item.get("email") or "").strip().lower()
                    if not email:
                        continue
                    verified = item.get("isVerified")
                    status = "valid" if verified in (1, True, "1") else "unverified"
                    conf = 85 if status == "valid" else 55
                    if item.get("confidence") is not None:
                        try:
                            conf = max(conf, int(item["confidence"]))
                        except (TypeError, ValueError):
                            pass
                    notes = []
                    if item.get("sourcePage"):
                        notes.append(f"snov source: {item['sourcePage'][:80]}")
                    out.append(
                        rec(
                            ctx, self.name,
                            first=item.get("firstName") or "",
                            last=item.get("lastName") or "",
                            email=email,
                            title=item.get("position") or "",
                            linkedin=item.get("linkedIn") or item.get("linkedin") or "",
                            status=status,
                            confidence=min(conf, 99),
                            notes=notes,
                        )
                    )
                last_id = ((data.get("links") or {}) or {}).get("lastId")
                if not last_id or last_id in seen_ids:
                    break
                seen_ids.add(last_id)
        except HttpError as exc:
            if not out:
                raise
            ctx.info(f"    snov: partial ({api_error(exc, 'snov')})")
        return out
