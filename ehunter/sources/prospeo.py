"""Prospeo domain-search: company domain -> verified employee emails."""

from __future__ import annotations

from ..config import env
from ..net import HttpError
from .base import Source, api_error, rec

ENDPOINT = "https://api.prospeo.io/domain-search"


class Prospeo(Source):
    name = "prospeo"
    label = "Prospeo"
    requires = ("PROSPEO_API_KEY",)

    def run(self, ctx) -> list:
        key = env("PROSPEO_API_KEY")
        headers = {"X-KEY": key, "Content-Type": "application/json"}
        out: list = []
        page = 1
        try:
            while len(out) < ctx.opts.max_per_source:
                data = ctx.http.post_json(
                    ENDPOINT,
                    {
                        "domain": ctx.domain,
                        "limit": 50,
                        "page": page,
                        "only_verified_email": False,
                    },
                    headers=headers,
                )
                if not data or data.get("error"):
                    ctx.info(f"    prospeo: {str(data)[:160]}")
                    break
                items = self._items(data)
                if not items:
                    break
                for item in items:
                    email = (item.get("email") or "").strip().lower()
                    if not email:
                        continue
                    verified = item.get("email_verified") or item.get("is_verified")
                    status = "valid" if verified in (True, 1, "1") else "unverified"
                    out.append(
                        rec(
                            ctx, self.name,
                            first=item.get("first_name") or item.get("firstName") or "",
                            last=item.get("last_name") or item.get("lastName") or "",
                            email=email,
                            title=(item.get("job_title") or item.get("position")
                                   or item.get("title") or ""),
                            linkedin=(item.get("linkedin_url") or item.get("linkedin")
                                      or ""),
                            status=status,
                            confidence=88 if status == "valid" else 50,
                            notes=["prospeo"],
                        )
                    )
                if len(items) < 50:
                    break
                page += 1
                if page > 20:
                    break
        except HttpError as exc:
            if not out:
                raise
            ctx.info(f"    prospeo: partial ({api_error(exc, 'prospeo')})")
        return out

    @staticmethod
    def _items(data: dict) -> list:
        """Tolerate the response-shape variations between Prospeo API revisions."""
        response = data.get("response")
        if isinstance(response, dict):
            for key in ("email_list", "emails", "results", "data"):
                if isinstance(response.get(key), list):
                    return response[key]
        for key in ("results", "data", "emails"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
