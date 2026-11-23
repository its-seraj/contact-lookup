"""User verification (via the separate auth service) and per-user API tokens.

Flow:

    1. The front end signs the user in against the auth service and gets a short-lived
       bearer JWT.
    2. It calls  POST /api/token  with that JWT.  We do NOT decode the JWT ourselves: we
       forward it to  {AUTH_SERVICE_URL}/api/auth/me  and trust the answer.  A bad or
       expired JWT is rejected there, so nothing is issued.
    3. Only then do we generate (or return) the user's long-lived API token, stored in
       MongoDB (collection ``api_tokens``, one document per user id).

    AUTH_SERVICE_URL=https://its-seraj-auth-service.hf.space      # default
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import env
from .store import DomainStore

log = logging.getLogger("ehunter.auth")

DEFAULT_AUTH_URL = "https://its-seraj-auth-service.hf.space"
TOKEN_PREFIX = "eh_"
_BEARER = {"WWW-Authenticate": "Bearer"}


class AuthError(Exception):
    """Carries the HTTP status we should answer with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class TokenStoreError(Exception):
    """MongoDB is not configured or the operation failed."""


# ---------------------------------------------------------------------------
# step 1: who is calling?  (asks the auth service, never decodes the JWT here)
def fetch_user(authorization: str, timeout: float = 10.0) -> dict:
    base = (env("AUTH_SERVICE_URL") or DEFAULT_AUTH_URL).rstrip("/")
    request = urllib.request.Request(
        f"{base}/api/auth/me",
        headers={"Authorization": authorization, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise AuthError(401, "invalid or expired token") from None
        log.warning("auth service answered %s", exc.code)
        raise AuthError(502, f"auth service error ({exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("auth service unreachable: %s", exc)
        raise AuthError(502, "auth service unreachable") from None
    except ValueError:
        raise AuthError(502, "auth service returned invalid JSON") from None

    data = payload.get("data") if isinstance(payload, dict) else None
    user = data.get("user") if isinstance(data, dict) else None
    if not (isinstance(payload, dict) and payload.get("success") is True
            and isinstance(user, dict) and user.get("id")):
        raise AuthError(401, "invalid or expired token")
    return {
        "id": str(user["id"]),
        "email": user.get("email") or "",
        "name": user.get("name") or "",
        "picture": user.get("picture") or "",
        "roles": list(user.get("roles") or []),
    }


# auto_error=False: we answer a missing/odd header ourselves; this also puts the
# "Authorize" button on /docs.
bearer_scheme = HTTPBearer(auto_error=False, description="Sign-in JWT from the auth service "
                           "(or, on the scan routes, an eh_... API token)")


def bearer_token(credentials: "Optional[HTTPAuthorizationCredentials]") -> str:
    """The raw bearer value, or a 401 when the header is missing/empty/not Bearer."""
    token = credentials.credentials.strip() if credentials else ""
    if not token:
        raise HTTPException(status_code=401, detail="missing Bearer token", headers=_BEARER)
    return token


async def verify_jwt(token: str) -> dict:
    """Ask the auth service who `token` belongs to; HTTPException on any failure."""
    try:
        return await asyncio.to_thread(fetch_user, f"Bearer {token}")
    except AuthError as exc:
        headers = _BEARER if exc.status == 401 else None
        raise HTTPException(status_code=exc.status, detail=exc.detail, headers=headers) from None


async def current_user(
    credentials: "Optional[HTTPAuthorizationCredentials]" = Depends(bearer_scheme),
) -> dict:
    """FastAPI dependency: the user behind a sign-in JWT (JWT only, no API tokens)."""
    return await verify_jwt(bearer_token(credentials))


# ---------------------------------------------------------------------------
# step 2: the user's API token
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "token": doc["token"],
        "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
    }


class TokenStore:
    """One document per user id in ``api_tokens``; shares the cache's MongoDB connection."""

    def __init__(self, base: "DomainStore", collection: str = "api_tokens"):
        self._base = base
        self._name = collection
        self._col = None

    def _collection(self):
        if self._col is not None:
            return self._col
        col = self._base.collection_named(self._name)
        if col is None:
            raise TokenStoreError("token storage unavailable: MongoDB is not connected")
        try:
            col.create_index("token", unique=True)
        except Exception as exc:  # noqa: BLE001
            raise TokenStoreError(f"token storage unavailable: {exc}") from None
        self._col = col
        return col

    def get(self, user_id: str) -> "Optional[dict]":
        col = self._collection()
        try:
            doc = col.find_one({"_id": user_id})
        except Exception as exc:  # noqa: BLE001
            raise TokenStoreError(f"token lookup failed: {exc}") from None
        return _public(doc) if doc else None

    def find_user(self, token: str) -> "Optional[dict]":
        """The owner of an API token, shaped like fetch_user()'s result, or None."""
        col = self._collection()
        try:
            doc = col.find_one({"token": token})
        except Exception as exc:  # noqa: BLE001
            raise TokenStoreError(f"token lookup failed: {exc}") from None
        if not doc:
            return None
        return {"id": str(doc["_id"]), "email": doc.get("email", ""),
                "name": doc.get("name", ""), "picture": "", "roles": []}

    def issue(self, user: dict, rotate: bool = False) -> "tuple[dict, bool]":
        """(token document, created). Returns the existing token unless `rotate`.

        Atomic: two concurrent first calls both end up with the same token, because only
        the first insert wins ($setOnInsert) and both read back the stored value.
        """
        from pymongo import ReturnDocument

        col = self._collection()
        candidate = TOKEN_PREFIX + secrets.token_urlsafe(32)
        fields = {"token": candidate, "email": user.get("email", ""),
                  "name": user.get("name", ""), "created_at": _now()}
        operator = "$set" if rotate else "$setOnInsert"
        try:
            doc = col.find_one_and_update(
                {"_id": user["id"]}, {operator: fields},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        except Exception as exc:  # noqa: BLE001
            raise TokenStoreError(f"token write failed: {exc}") from None
        return _public(doc), doc["token"] == candidate
