"""Hunter.io - domain-search returns known emails, names, titles and the domain's pattern."""

from __future__ import annotations

from ..config import env
from ..net import HttpError
from .base import Source, api_error, rec

ENDPOINT = "https://api.hunter.io/v2/domain-search"

_STATUS = {
    "valid": ("valid", 95),
    "accept_all": ("catch_all", 55),
    "webmail": ("unverified", 40),
    "disposable": ("risky", 20),
    "invalid": ("invalid", 5),
    "unknown": ("unverified", 45),
}


class Hunter(Source):
    name = "hunter"
    label = "Hunter.io"
    requires = ("HUNTER_API_KEY",)

    def run(self, ctx) -> list:
        key = env("HUNTER_API_KEY")
        out, offset, limit = [], 0, 100
        total = None
        try:
            while offset < ctx.opts.max_per_source:
                data = ctx.http.get_json(
                    ENDPOINT,
                    params={
                        "domain": ctx.domain,
                        "api_key": key,
                        "limit": limit,
                        "offset": offset,
                    },
                )
                payload = (data or {}).get("data") or {}
                if offset == 0:
                    if payload.get("pattern"):
                        ctx.remember("hunter_pattern", payload["pattern"])
                    if payload.get("organization"):
                        ctx.remember("organization", payload["organization"])
                    meta = (data or {}).get("meta") or {}
                    total = meta.get("total") or meta.get("results")
                    ctx.info(f"    hunter: total known emails {total}")

                items = payload.get("emails") or []
                if not items:
                    break
                for item in items:
                    email = (item.get("value") or "").strip().lower()
                    if not email:
                        continue
                    v_status = ((item.get("verification") or {}).get("status") or "").lower()
                    status, boost = _STATUS.get(v_status, ("unverified", 45))
                    conf = int(item.get("confidence") or 0)
                    conf = max(conf, boost) if status in ("valid", "catch_all") else conf
                    kind = (item.get("type") or "").lower()
                    notes = []
                    if kind and kind not in ("personal",):
                        notes.append(f"hunter type={kind}")
                    out.append(
                        rec(
                            ctx, self.name,
                            first=item.get("first_name") or "",
                            last=item.get("last_name") or "",
                            email=email,
                            title=item.get("position") or "",
                            linkedin=item.get("linkedin") or "",
                            status=status,
                            confidence=conf,
                            pattern=ctx.memo("hunter_pattern") or "",
                            notes=notes,
                        )
                    )
                offset += len(items)
                if total and offset >= int(total):
                    break
                if len(items) < limit:
                    break
        except HttpError as exc:
            if out:
                ctx.info(f"    hunter: partial ({api_error(exc, 'hunter')})")
            else:
                raise
        return out
