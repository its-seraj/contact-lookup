"""Keep Hugging Face Spaces awake, and wake one that has gone to sleep.

Two tools:

* ``wake(url)``     poll a Space until it answers; used by auth.py when a call fails because
                    the auth service is asleep (a cold start takes ~30-90 s).
* ``autostart()``   a daemon thread that pings the auth service and this Space on an
                    interval, so neither reaches the free tier's idle limit.

A URL counts as awake when it answers ``200`` with a JSON body. A sleeping Space answers
with an error or an HTML placeholder page, and an HTML 200 must not count as "up".

Limit: a Space that is already asleep runs no code, so it cannot ping itself awake. This
loop prevents sleep while the app runs; for a guarantee, also point an external pinger
(UptimeRobot, cron-job.org, a GitHub Actions cron) at ``/api/health``. A *paused* Space
needs a manual restart: no request wakes it.

    KEEPALIVE=1|0                      # default: on inside a Space (SPACE_ID set), else off
    KEEPALIVE_INTERVAL_MINUTES=30
    KEEPALIVE_URLS=https://a.hf.space/health,https://b.hf.space/api/health   # extra targets
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable, List, Optional

from .config import env

log = logging.getLogger("enrivo.keepalive")

FIRST_PING_DELAY_S = 30          # let our own server start listening before pinging it
DEFAULT_INTERVAL_MIN = 30


def ping(url: str, timeout: float = 20.0) -> bool:
    """True when `url` answers 200 with JSON (i.e. the app itself, not a sleep page)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "enrivo-keepalive"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            json.load(response)
            return True
    except urllib.error.HTTPError as exc:
        exc.close()
        return False
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def wake(url: str, wait: float = 90.0, interval: float = 5.0,
         _sleep: Callable[[float], None] = time.sleep,
         _clock: Callable[[], float] = time.monotonic) -> bool:
    """Poll `url` until it is awake or `wait` seconds pass. The first request is what
    starts a sleeping Space; the rest just wait for the cold start to finish."""
    if wait <= 0:
        return False
    deadline = _clock() + wait
    while True:
        if ping(url, timeout=min(20.0, max(wait, 1.0))):
            return True
        if _clock() + interval >= deadline:
            return False
        _sleep(interval)


def _targets(defaults: Iterable[str]) -> "List[str]":
    urls = list(defaults)
    host = env("SPACE_HOST")                         # set by Hugging Face inside a Space
    if host:
        urls.append(f"https://{host}/api/health")
    urls.extend(u.strip() for u in (env("KEEPALIVE_URLS") or "").split(",") if u.strip())
    seen, ordered = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _enabled() -> bool:
    flag = (env("KEEPALIVE") or "").lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool(env("SPACE_ID") or env("SPACE_HOST"))


def _run(urls: "List[str]", interval_s: float, stop: threading.Event) -> None:
    stop.wait(FIRST_PING_DELAY_S)
    state: "dict[str, bool]" = {}
    while not stop.is_set():
        for url in urls:
            ok = ping(url) or wake(url, wait=60)     # one request, then a short wake attempt
            if state.get(url) != ok:                 # log changes, not every healthy ping
                (log.info if ok else log.warning)("keepalive %s: %s", url, "up" if ok else "DOWN")
                print(f"[keepalive] {url}: {'up' if ok else 'DOWN'}", flush=True)
                state[url] = ok
        stop.wait(interval_s)


def autostart(defaults: "Iterable[str]" = ()) -> "Optional[threading.Event]":
    """Start the ping loop if enabled; returns its stop event (None when disabled)."""
    if not _enabled():
        print("[keepalive] off (not running in a Space; set KEEPALIVE=1 to force)", flush=True)
        return None
    urls = _targets(defaults)
    if not urls:
        return None
    try:
        minutes = max(1.0, float(env("KEEPALIVE_INTERVAL_MINUTES") or DEFAULT_INTERVAL_MIN))
    except ValueError:
        minutes = float(DEFAULT_INTERVAL_MIN)
    stop = threading.Event()
    threading.Thread(target=_run, args=(urls, minutes * 60, stop),
                     name="keepalive", daemon=True).start()
    print(f"[keepalive] every {minutes:g} min: {', '.join(urls)}", flush=True)
    return stop
