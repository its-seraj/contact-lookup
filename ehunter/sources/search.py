"""Search-engine dorking: find public pages/snippets that expose addresses and LinkedIn profiles.

Providers are tried in this order, and the first configured one wins for per-person
lookups:

  keyed   : Serper.dev, Brave Search, Google Programmable Search (any of these)
  keyless : a self-hosted or public SearXNG instance (SEARXNG_URL), DuckDuckGo HTML, Bing HTML

The keyless engines block datacenter IPs aggressively and show captchas. Every provider
is therefore best-effort: a blocked engine yields no results and the scan continues.
"""

from __future__ import annotations

import base64
import html
import re
import urllib.parse

from ..config import env
from ..names import looks_like_name, slug
from ..net import HttpError
from ..util import find_emails, linkedin_matches_name, linkedin_url
from .base import Source, api_error, rec

LINKEDIN_SUFFIX = re.compile(r"\s*[|\-–]\s*LinkedIn\s*$", re.I)
SPLIT = re.compile(r"\s+[-–|·]\s+")
TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return " ".join(html.unescape(TAG_RE.sub(" ", text or "")).split())


def _linkedin_parts(title: str) -> "tuple[str, str]":
    """'Jane Doe - VP Sales - Acme | LinkedIn' -> ('Jane Doe', 'VP Sales')."""
    cleaned = LINKEDIN_SUFFIX.sub("", _clean(title)).strip()
    segments = [s.strip() for s in SPLIT.split(cleaned) if s.strip()]
    if segments and looks_like_name(segments[0]):
        role = segments[1] if len(segments) > 1 else ""
        if role.lower() in ("linkedin",):
            role = ""
        return segments[0], role[:100]
    return "", ""


# ---------------------------------------------------------------- keyless parsers
DDG_RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I
)
DDG_SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I
)
BING_BLOCK = re.compile(r'<li class="b_algo".*?</li>', re.S | re.I)
BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
SEARX_RESULT = re.compile(
    r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I
)
SEARX_SNIPPET = re.compile(r'<p class="content">(.*?)</p>', re.S | re.I)


def _b64_url(value: str) -> str:
    """Bing wraps results as /ck/a?...&u=a1<base64url of the target>."""
    if not value:
        return ""
    raw = value[2:] if value.startswith("a1") else value
    raw = raw.replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def parse_ddg(body: str) -> "list[dict]":
    snippets = [s for s in DDG_SNIPPET.findall(body or "")]
    out = []
    for index, (href, title) in enumerate(DDG_RESULT.findall(body or "")):
        link = href
        if "uddg=" in href:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            link = urllib.parse.unquote((query.get("uddg") or [""])[0])
        if not link.startswith("http"):
            continue
        out.append({
            "title": _clean(title),
            "link": link,
            "snippet": _clean(snippets[index]) if index < len(snippets) else "",
        })
    return out


def parse_bing(body: str) -> "list[dict]":
    out = []
    for block in BING_BLOCK.findall(body or ""):
        match = BING_LINK.search(block)
        if not match:
            continue
        href, title = match.groups()
        link = href
        if "bing.com/ck/a" in href:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            link = _b64_url((query.get("u") or [""])[0])
        if not link.startswith("http"):
            continue
        snippet = ""
        para = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        if para:
            snippet = _clean(para.group(1))
        out.append({"title": _clean(title), "link": link, "snippet": snippet})
    return out


def parse_searx(body: str) -> "list[dict]":
    snippets = [_clean(s) for s in SEARX_SNIPPET.findall(body or "")]
    out = []
    for index, (href, title) in enumerate(SEARX_RESULT.findall(body or "")):
        if href.startswith("/"):
            continue
        out.append({
            "title": _clean(title),
            "link": href,
            "snippet": snippets[index] if index < len(snippets) else "",
        })
    return out


class Search(Source):
    name = "search"
    label = "Search dorks"
    requires = ()      # works keyless; keys only improve reliability
    free = True

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------------
    def _providers(self, ctx) -> "list[tuple[str, object]]":
        providers: "list[tuple[str, object]]" = []
        if env("SERPER_API_KEY"):
            providers.append(("serper", self._serper))
        if env("BRAVE_API_KEY"):
            providers.append(("brave", self._brave))
        if env("GOOGLE_CSE_KEY") and env("GOOGLE_CSE_CX"):
            providers.append(("google", self._google_cse))
        if env("SEARXNG_URL"):
            providers.append(("searxng", self._searxng))
        if not providers:
            providers = [("duckduckgo", self._duckduckgo), ("bing", self._bing)]
        return providers

    @staticmethod
    def _relevant(item: dict, domain: str) -> bool:
        """Drop engines that answer with unrelated cached pages."""
        link = (item.get("link") or "").lower()
        text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
        return domain in text or domain in link or "linkedin.com/in/" in link

    def _note_miss(self, ctx, provider_name: str, ok: bool) -> None:
        """Two empty/failed answers in a row mean the engine is blocked - stop asking."""
        misses = dict(ctx.memo("provider_misses") or {})
        misses[provider_name] = 0 if ok else misses.get(provider_name, 0) + 1
        ctx.remember("provider_misses", misses)

    def _dead_providers(self, ctx) -> set:
        misses = ctx.memo("provider_misses") or {}
        return {name for name, count in misses.items() if count >= 2}

    def _collect(self, ctx, query: str, pages: int = 1, relevant: bool = True):
        """Yield results across providers, walking `pages` result pages per provider."""
        dead = self._dead_providers(ctx)
        for provider_name, provider in self._providers(ctx):
            if provider_name in dead:
                continue
            seen: set = set()
            for page in range(1, max(1, pages) + 1):
                try:
                    items = provider(ctx, query, page) or []
                except HttpError as exc:
                    if exc.status == 400:
                        ctx.debug(f"search[{provider_name}]: query rejected: {exc.body[:120]}")
                        break
                    self._note_miss(ctx, provider_name, False)
                    ctx.debug(f"search[{provider_name}]: {api_error(exc, provider_name)}")
                    break
                except Exception as exc:  # noqa: BLE001 - never let an engine kill the scan
                    self._note_miss(ctx, provider_name, False)
                    ctx.debug(f"search[{provider_name}]: {type(exc).__name__}: {exc}")
                    break
                fresh = [item for item in items if (item.get("link") or "") not in seen]
                self._note_miss(ctx, provider_name, bool(fresh))
                if not fresh:
                    break
                for item in fresh:
                    seen.add(item.get("link") or "")
                for item in fresh:
                    if relevant and not self._relevant(item, ctx.domain):
                        continue
                    item["provider"] = provider_name
                    item["query"] = query
                    yield item

    # ------------------------------------------------------------------
    def run(self, ctx) -> list:
        domain = ctx.domain
        queries = [
            f'site:linkedin.com/in "{domain}"',
            f'"@{domain}"',
            f'"@{domain}" -site:{domain}',
            f'"@{domain}" filetype:pdf',
        ]
        keyed = any(
            env(k) for k in
            ("SERPER_API_KEY", "BRAVE_API_KEY", "GOOGLE_CSE_KEY", "SEARXNG_URL")
        )
        if not keyed:
            # Keyless engines are slow and rate-limited; spend the requests on the
            # two dorks that actually produce people.
            queries = [f'site:linkedin.com/in "{domain}"', f'"@{domain}"']
        results: dict = {}
        for query in queries:
            for item in self._collect(ctx, query, pages=max(1, int(ctx.opts.search_pages))):
                link = (item.get("link") or "").strip()
                if link:
                    results.setdefault(link, item)
        dead = self._dead_providers(ctx)
        if dead and not results:
            ctx.info("    search: no result - "
                     + ", ".join(sorted(dead)) + " blocked/unavailable "
                     "(keyless engines captcha datacenter IPs; set SEARXNG_URL or an API key)")
        else:
            ctx.info(f"    search: {len(results)} relevant result(s)")

        out: list = []
        seen = set()
        for link, item in results.items():
            title = item.get("title") or ""
            snippet = item.get("snippet") or ""
            name, role = _linkedin_parts(title)
            profile = linkedin_url(link) or linkedin_url(snippet)

            if profile:
                first = last = ""
                if name:
                    from ..names import split_name

                    first, last = split_name(name)
                key = ("li", linkedin_slug_or_url(profile))
                if key in seen:
                    continue
                seen.add(key)
                from .web import is_external_title

                label = ctx.domain.split(".")[0].lower()
                mentioned = label in f"{title} {snippet}".lower()
                # A headline like "Software Engineer at Cisco" belongs to someone
                # else; keep the row but do not present it as an employee.
                elsewhere = is_external_title(title, ctx.domain)
                confidence = 55 if (mentioned and not elsewhere) else 30
                notes = [f"search[{item.get('provider')}]"]
                if not mentioned:
                    notes.append("linkedin: company not named in snippet")
                if elsewhere:
                    notes.append("linkedin: headline names another company")
                out.append(
                    rec(
                        ctx, self.name, first=first, last=last, full=name,
                        title=role, linkedin=profile, status="name_only",
                        confidence=confidence, notes=notes,
                    )
                )
                continue

            for email in find_emails(f"{title} {snippet}", domain):
                key = ("e", email)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    rec(
                        ctx, self.name, full=name, email=email, title=role,
                        confidence=50, notes=[f"search[{item.get('provider')}]"],
                    )
                )

            if not profile and not name:
                continue
            if not profile and name and "linkedin.com/in/" in link:
                key = ("n", name.lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    rec(ctx, self.name, full=name, title=role, confidence=35,
                        notes=[f"search: {link}"])
                )
        return out

    # ------------------------------------------------------------------
    def lookup_linkedin(self, ctx, record) -> str:
        """One targeted query: 'First Last' + domain + site:linkedin.com/in."""
        query = (
            f'site:linkedin.com/in "{record.first_name} {record.last_name}" '
            f'"{ctx.domain}"'
        )
        wanted = slug(f"{record.first_name}{record.last_name}")
        try:
            for item in self._collect(ctx, query):
                profile = linkedin_url(item.get("link") or "") or linkedin_url(
                    item.get("snippet") or ""
                )
                if not profile:
                    continue
                if linkedin_matches_name(profile, record.first_name, record.last_name):
                    return profile
                name, _role = _linkedin_parts(item.get("title") or "")
                if name and slug(name) == wanted:
                    return profile
        except HttpError:
            return ""
        return ""

    # ------------------------------------------------------------------
    # providers
    # ------------------------------------------------------------------
    def _serper(self, ctx, query: str, page: int = 1) -> list:
        # Free Serper accounts reject num > 10 ("Query pattern not allowed"), so ask for
        # 10 per page and let _collect walk the pages.
        data = ctx.http.post_json(
            "https://google.serper.dev/search",
            {"q": query, "num": 10, "page": page},
            headers={"X-API-KEY": env("SERPER_API_KEY")},
        )
        return [
            {"title": r.get("title") or "", "link": r.get("link") or "",
             "snippet": r.get("snippet") or ""}
            for r in (data or {}).get("organic") or []
        ]

    def _brave(self, ctx, query: str, page: int = 1) -> list:
        data = ctx.http.get_json(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 20, "offset": (page - 1) * 20},
            headers={"X-Subscription-Token": env("BRAVE_API_KEY"),
                     "Accept": "application/json"},
        )
        return [
            {"title": r.get("title") or "", "link": r.get("url") or "",
             "snippet": r.get("description") or ""}
            for r in (((data or {}).get("web") or {}).get("results") or [])
        ]

    def _google_cse(self, ctx, query: str, page: int = 1) -> list:
        data = ctx.http.get_json(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": env("GOOGLE_CSE_KEY"), "cx": env("GOOGLE_CSE_CX"),
                    "q": query, "num": 10, "start": 1 + 10 * (page - 1)},
        )
        return [
            {"title": r.get("title") or "", "link": r.get("link") or "",
             "snippet": r.get("snippet") or ""}
            for r in (data or {}).get("items") or []
        ]

    def _searxng(self, ctx, query: str, page: int = 1) -> list:
        base = (env("SEARXNG_URL") or "").rstrip("/")
        if not base:
            return []
        body = ctx.http.get_text(
            f"{base}/search",
            params={"q": query, "language": "en", "safesearch": 0, "pageno": page},
            headers={"Accept": "text/html"},
            retries=1,
        )
        return parse_searx(body)

    def _duckduckgo(self, ctx, query: str, page: int = 1) -> list:
        body = ctx.http.get_text(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "kl": "us-en"},
            headers={"Accept": "text/html", "Referer": "https://duckduckgo.com/"},
            retries=1,
        )
        if "challenge" in body.lower() and "result__a" not in body:
            raise HttpError(429, "duckduckgo", "captcha challenge served")
        return parse_ddg(body)

    def _bing(self, ctx, query: str, page: int = 1) -> list:
        body = ctx.http.get_text(
            "https://www.bing.com/search",
            params={"q": query, "count": 30, "setlang": "en", "cc": "us"},
            headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
            retries=1,
        )
        return parse_bing(body)


    # ------------------------------------------------------------------
    def sweep_linkedin(self, ctx, depth: int = 3, max_variants: int = 14) -> "list[dict]":
        """Fan out many query variants and return every unique profile found."""
        from ..sweep import classify, slug_from_link, title_parts, variants

        names = ctx.memo("company_names") or [ctx.domain.split(".")[0]]
        queries = variants(ctx.domain, names, max_variants)
        ctx.info(f"    linkedin: {len(queries)} query variant(s) x {depth} page(s)")

        found: dict = {}
        for query in queries:
            for item in self._collect(ctx, query, pages=depth, relevant=False):
                slug = slug_from_link(item.get("link") or "")
                if not slug:
                    continue
                if slug in found:
                    if query not in found[slug]["queries"]:
                        found[slug]["queries"].append(query)
                    continue
                title = item.get("title") or ""
                snippet = item.get("snippet") or ""
                employment, reason = classify(title, snippet, names)
                name, role = title_parts(title)
                found[slug] = {
                    "slug": slug,
                    "url": linkedin_url(item.get("link") or ""),
                    "full_name": name,
                    "title": role,
                    "employment": employment,
                    "evidence": reason,
                    "snippet": snippet[:300],
                    "queries": [query],
                }
        ctx.info(f"    linkedin: {len(found)} unique profile(s)")
        return list(found.values())


def linkedin_slug_or_url(url: str) -> str:
    from ..util import linkedin_slug

    return linkedin_slug(url) or url
