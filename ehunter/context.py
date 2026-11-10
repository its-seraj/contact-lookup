"""Run context handed to every source: options, http client, logger, shared cache."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .net import HttpClient


@dataclass
class Options:
    # discovery
    sources: list = field(default_factory=list)
    max_per_source: int = 1000
    threads: int = 8
    timeout: float = 20.0
    delay: float = 0.4
    # pattern engine
    max_patterns: int = 2
    linkedin_lookups: int = 10
    permute: bool = False
    permute_max: int = 19
    permute_all: bool = False
    linkedin_sweep: bool = False
    linkedin_file: str = ""
    sweep_depth: int = 3
    sweep_variants: int = 14
    # source specifics
    github_org: str = ""
    max_pages: int = 12
    search_pages: int = 3
    apollo_reveal: bool = False
    # verification
    verify: bool = False
    from_addr: str = "noreply@example.com"
    verify_delay: float = 0.2
    verify_timeout: float = 10.0
    max_verify: int = 500
    # an address you know exists on the domain; probed to detect throttled servers
    control_address: str = ""
    # greylisted answers are retried after this many seconds, this many times
    greylist_wait: float = 0.0
    greylist_retries: int = 0
    throttled_as_valid: bool = False
    # output
    min_confidence: int = 0
    only_verified: bool = False
    keep_invalid: bool = False
    # misc
    verbose: bool = False
    user_agent: str = ""
    extra: dict = field(default_factory=dict)

    def get(self, key, default=None):
        return getattr(self, key, default)


@dataclass
class Context:
    domain: str
    http: HttpClient
    opts: Options
    log: object = None
    app_url: str = ""
    cache: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def info(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    def debug(self, msg: str) -> None:
        if self.log and self.opts.verbose:
            self.log(f"    {msg}")

    def memo(self, key):
        with self.lock:
            return self.cache.get(key)

    def remember(self, key, value):
        with self.lock:
            self.cache[key] = value
        return value
