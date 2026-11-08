"""HTTP API (FastAPI): multi-domain, streaming, MongoDB-cached scans.

    POST /api/scan                -> newline-delimited JSON (or SSE) stream
    GET  /api/domains             -> cached domains (no rows)
    GET  /api/domains/{domain}    -> cached document with rows
    DEL  /api/domains/{domain}    -> drop one cached domain
    GET  /api/health              -> cache availability + port-25 state

Body:

    {"domains": ["acme.com", "beta.io"], "isVerify": false, "isLinkedin": true, "force": false}

Events on the stream: `start`, `log`, `cached`, `result`, `error`, `done`.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import env
from .service import (
    MAX_DOMAINS,
    ScanFlags,
    build_doc,
    cache_covers,
    normalize_domains,
    run_domain,
)
from .store import DomainStore
from .webapp import port25_cached

router = APIRouter()
_STORE: "Optional[DomainStore]" = None


def require_key(x_api_key: "Optional[str]" = Header(default=None)) -> None:
    """Optional shared-secret guard: set API_KEY to require the header on every route."""
    expected = env("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


def store() -> DomainStore:
    global _STORE
    if _STORE is None:
        _STORE = DomainStore()
    return _STORE


class ScanBody(BaseModel):
    domains: Optional[Union[List[str], str]] = Field(
        default=None, description="One domain, a list, or a comma/space separated string"
    )
    isVerify: bool = Field(default=False, description="run SMTP verification + permutation")
    isLinkedin: bool = Field(default=True, description="run the LinkedIn profile sweep")
    force: bool = Field(default=False, description="ignore the cached result and rescan")


def _encoder(fmt: str):
    if fmt == "sse":
        def encode(event: dict) -> str:
            return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        return encode, "text/event-stream"

    def encode(event: dict) -> str:
        return json.dumps(event, ensure_ascii=False, default=str) + "\n"
    return encode, "application/x-ndjson"


async def _stream(domains: "List[str]", flags: ScanFlags, force: bool, encode) -> "object":
    started = time.time()
    for domain in domains:
        cached = await asyncio.to_thread(store().get, domain)
        if cached and not force and cache_covers(cached, flags):
            yield encode({
                "type": "cached",
                "domain": domain,
                "flags": cached.get("flags") or {},
                "counts": cached.get("counts") or {},
                "scanned_at": cached.get("scanned_at") or "",
            })
            yield encode({
                "type": "result",
                "domain": domain,
                "rows": cached.get("rows") or [],
                "meta": cached.get("meta") or {},
                "counts": cached.get("counts") or {},
                "cached": True,
            })
            continue

        yield encode({"type": "start", "domain": domain})
        queue: "asyncio.Queue[str]" = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def log(message: str = "", **_kwargs) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, str(message))

        task = asyncio.create_task(asyncio.to_thread(run_domain, domain, flags, log))
        while True:
            getter = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait(
                {getter, task}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter in done:
                yield encode({"type": "log", "domain": domain, "message": getter.result()})
                continue
            getter.cancel()
            break

        while not queue.empty():
            yield encode({"type": "log", "domain": domain, "message": queue.get_nowait()})

        try:
            result = task.result()
        except Exception as exc:  # noqa: BLE001 - one bad domain must not kill the stream
            yield encode({
                "type": "error",
                "domain": domain,
                "message": f"{type(exc).__name__}: {exc}",
            })
            continue

        doc = build_doc(domain, result, flags)
        stored = await asyncio.to_thread(store().upsert, domain, doc)
        yield encode({
            "type": "result",
            "domain": domain,
            "rows": doc["rows"],
            "meta": doc["meta"],
            "counts": doc["counts"],
            "scanned_at": doc["scanned_at"],
            "cached": stored,
        })

    yield encode({
        "type": "done",
        "domains": len(domains),
        "elapsed_ms": int((time.time() - started) * 1000),
    })


@router.post("/scan", summary="Scan one or more domains, streaming results as they finish",
             dependencies=[Depends(require_key)])
async def scan(body: ScanBody, format: str = Query("ndjson", pattern="^(ndjson|sse)$")):
    limit = int(env("DOMAINS_PER_REQUEST") or MAX_DOMAINS)
    domains, errors = normalize_domains(body.domains, limit=limit)
    if not domains and not errors:
        raise HTTPException(status_code=400, detail="no domains provided")

    flags = ScanFlags(is_verify=bool(body.isVerify), is_linkedin=bool(body.isLinkedin))
    encode, media_type = _encoder(format)

    async def generator():
        for problem in errors:
            yield encode({"type": "error", **problem})
        async for chunk in _stream(domains, flags, bool(body.force), encode):
            yield chunk

    return StreamingResponse(
        generator(),
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # nginx: do not buffer the stream
        },
    )


@router.get("/domains", summary="Cached domains", dependencies=[Depends(require_key)])
async def list_domains(limit: int = Query(200, ge=1, le=1000)):
    docs = await asyncio.to_thread(store().list_domains, limit)
    return {"domains": docs, "count": len(docs)}


@router.get("/domains/{domain}", summary="Cached result for one domain",
            dependencies=[Depends(require_key)])
async def get_domain(domain: str):
    from .util import normalize_domain

    try:
        key = normalize_domain(domain)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    doc = await asyncio.to_thread(store().get, key)
    if not doc:
        raise HTTPException(status_code=404, detail=f"{key} is not cached")
    doc.pop("_id", None)
    return doc


@router.delete("/domains/{domain}", summary="Drop a cached domain",
               dependencies=[Depends(require_key)])
async def delete_domain(domain: str):
    from .util import normalize_domain

    try:
        key = normalize_domain(domain)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    removed = await asyncio.to_thread(store().delete, key)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{key} is not cached")
    return {"deleted": key}


@router.get("/health", summary="Service health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "cache": store().health(),
        "port25": port25_cached(),
        "max_domains_per_request": int(env("DOMAINS_PER_REQUEST") or MAX_DOMAINS),
    }


def create_app() -> FastAPI:
    app = FastAPI(
        title="ehunter API",
        version=__version__,
        description="Domain -> people, work emails and LinkedIn profiles, streamed per domain.",
    )
    origins = env("CORS_ORIGINS") or "*"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    return app


app = create_app()
