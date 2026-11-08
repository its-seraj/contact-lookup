"""MongoDB cache for scan results, one document per domain.

The cache is optional: if pymongo is not installed, or the server is unreachable, every
method degrades to "no cached data" instead of failing the request.

    MONGODB_URI=mongodb://localhost:27017
    MONGODB_DB=ehunter          # database name
    MONGODB_COLLECTION=domains  # collection name
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import env

log = logging.getLogger("ehunter.store")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DomainStore:
    """Thread-safe (pymongo clients are); used from worker threads via run_in_executor."""

    def __init__(self, uri: "str | None" = None, db_name: "str | None" = None,
                 collection: "str | None" = None):
        self.uri = uri if uri is not None else (env("MONGODB_URI") or "")
        self.db_name = db_name or env("MONGODB_DB") or "ehunter"
        self.collection_name = collection or env("MONGODB_COLLECTION") or "domains"
        self._client = None
        self._col = None
        self._error = ""
        if not self.uri:
            self._error = "MONGODB_URI not set"

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._collection() is not None

    def _collection(self):
        if self._col is not None:
            return self._col
        if not self.uri:
            return None
        try:
            from pymongo import MongoClient
        except ImportError:
            self._error = "pymongo not installed"
            return None
        try:
            self._client = MongoClient(self.uri, serverSelectionTimeoutMS=4000,
                                       connectTimeoutMS=4000)
            self._client.admin.command("ping")
            self._col = self._client[self.db_name][self.collection_name]
            self._col.create_index("domain", unique=True, sparse=True)
            log.info("mongo cache ready: db=%s collection=%s", self.db_name, self.collection_name)
        except Exception as exc:  # noqa: BLE001 - cache must never break the API
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("mongo cache unavailable: %s", self._error)
            self._col = None
        return self._col

    # ------------------------------------------------------------------
    def get(self, domain: str) -> "dict | None":
        collection = self._collection()
        if collection is None:
            return None
        try:
            return collection.find_one({"_id": domain})
        except Exception as exc:  # noqa: BLE001
            log.warning("mongo get failed for %s: %s", domain, exc)
            return None

    def upsert(self, domain: str, doc: dict) -> bool:
        collection = self._collection()
        if collection is None:
            return False
        payload = dict(doc)
        payload["_id"] = domain
        payload["domain"] = domain
        payload["updated_at"] = _now()
        try:
            collection.update_one({"_id": domain}, {"$set": payload}, upsert=True)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("mongo upsert failed for %s: %s", domain, exc)
            return False

    def delete(self, domain: str) -> bool:
        collection = self._collection()
        if collection is None:
            return False
        try:
            collection.delete_one({"_id": domain})
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_domains(self, limit: int = 200) -> "list[dict]":
        collection = self._collection()
        if collection is None:
            return []
        try:
            cursor = collection.find({}, {"rows": 0}).sort("updated_at", -1).limit(limit)
            out = []
            for doc in cursor:
                doc.pop("_id", None)
                out.append(doc)
            return out
        except Exception:  # noqa: BLE001
            return []

    def health(self) -> dict:
        collection = self._collection()
        return {
            "enabled": collection is not None,
            "db": self.db_name,
            "collection": self.collection_name,
            "error": self._error,
        }
