"""HTTP layer: retries, per-host politeness, gzip, JSON helpers. Standard library only."""

from __future__ import annotations

import gzip
import io
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; ehunter/1.0; domain-research-tool)"
)


_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"\x27]?([A-Za-z0-9_\-]+)", re.I)
_HEADER_CHARSET = re.compile(r"charset=([A-Za-z0-9_\-]+)", re.I)


def sniff_charset(data: bytes, headers=None) -> str:
    """Content-Type charset first, then <meta charset> in the first 2 KB."""
    if headers:
        match = _HEADER_CHARSET.search(headers.get("Content-Type") or "")
        if match:
            return match.group(1)
    match = _META_CHARSET.search(data[:2048])
    if match:
        try:
            return match.group(1).decode("ascii")
        except UnicodeDecodeError:
            return ""
    return ""


def decode_body(data: bytes, headers=None) -> str:
    """Decode HTML/JSON with the declared charset, then sensible fallbacks."""
    if not data:
        return ""
    tried = []
    for encoding in (sniff_charset(data, headers), "utf-8", "cp1252", "latin-1"):
        if not encoding or encoding.lower() in tried:
            continue
        tried.append(encoding.lower())
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = (body or "")[:500]
        super().__init__(f"HTTP {status} for {url}: {self.body}")


class HttpClient:
    """Thread-safe client. All sources share one instance so throttling is global."""

    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = 3,
        delay: float = 0.4,
        user_agent: str = DEFAULT_UA,
        log=None,
    ):
        self.timeout = timeout
        self.retries = max(0, retries)
        self.delay = max(0.0, delay)
        self.user_agent = user_agent
        self._log = log or (lambda *a, **k: None)
        self._next_at: dict = {}
        self._lock = threading.Lock()
        # One opener shared across threads; it is stateless for our use.
        self._opener = urllib.request.build_opener()

    # ---- politeness ------------------------------------------------------
    def _throttle(self, host: str) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at.get(host, 0.0) - now)
            self._next_at[host] = max(now, self._next_at.get(host, 0.0)) + self.delay
        if wait:
            time.sleep(min(wait, 15.0))

    # ---- raw request -----------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        params: "dict | None" = None,
        headers: "dict | None" = None,
        json_body=None,
        form_body: "dict | None" = None,
        raw_body: "bytes | None" = None,
        retries: "int | None" = None,
    ) -> "tuple[int, dict, bytes]":
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True
            )
            url = f"{url}{'&' if '?' in url else '?'}{query}"

        hdrs = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        }
        if headers:
            hdrs.update({k: v for k, v in headers.items() if v is not None})
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form_body is not None:
            body = urllib.parse.urlencode(form_body).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif method.upper() in ("POST", "PUT", "PATCH") and body is None:
            body = b""

        host = urllib.parse.urlsplit(url).hostname or ""
        attempts = (self.retries if retries is None else retries) + 1
        last_err: "Exception | None" = None

        for attempt in range(attempts):
            self._throttle(host)
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.status, dict(resp.headers), self._decode(resp)
            except urllib.error.HTTPError as exc:
                payload = b""
                try:
                    payload = self._decode(exc)
                except Exception:
                    payload = b""
                if exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    wait = self._retry_after(exc.headers) or (2 ** attempt) + random.random()
                    self._log(f"    http {exc.code} {urllib.parse.urlsplit(url).hostname}, retry in {wait:.1f}s")
                    time.sleep(min(wait, 30.0))
                    last_err = HttpError(exc.code, url, payload.decode("utf-8", "ignore"))
                    continue
                raise HttpError(exc.code, url, payload.decode("utf-8", "ignore")) from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_err = exc
                if attempt < attempts - 1:
                    time.sleep(min((1.5 ** attempt) + random.random(), 20.0))
                    continue
                raise HttpError(0, url, str(exc)) from None
        raise HttpError(0, url, str(last_err or "request failed"))

    @staticmethod
    def _retry_after(headers) -> float:
        try:
            return float(headers.get("Retry-After", "") or 0) or 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _decode(resp) -> bytes:
        data = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            try:
                return gzip.decompress(data)
            except OSError:
                return data
        if "deflate" in enc:
            try:
                return zlib.decompress(data, -zlib.MAX_WBITS)
            except zlib.error:
                try:
                    return zlib.decompress(data)
                except zlib.error:
                    return data
        return data

    # ---- typed helpers ---------------------------------------------------
    def get_text(self, url: str, **kw) -> str:
        _, headers, data = self.request("GET", url, **kw)
        return decode_body(data, headers)

    def get_json(self, url: str, **kw):
        _, headers, data = self.request("GET", url, **kw)
        text = decode_body(data, headers).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Some endpoints prepend anti-XSSI junk.
            start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
            if start > 0:
                try:
                    return json.loads(text[start:])
                except json.JSONDecodeError:
                    pass
            raise HttpError(0, url, f"invalid JSON: {text[:200]}")

    def post_json(self, url: str, payload, **kw):
        _, headers, data = self.request("POST", url, json_body=payload, **kw)
        text = decode_body(data, headers).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise HttpError(0, url, f"invalid JSON: {text[:200]}")

    def post_form(self, url: str, payload, **kw):
        _, headers, data = self.request("POST", url, form_body=payload, **kw)
        text = decode_body(data, headers).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise HttpError(0, url, f"invalid JSON: {text[:200]}")
