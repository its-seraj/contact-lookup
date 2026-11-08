"""Company website scraper: team/about/leadership pages -> names, titles, mailto addresses.

Free, no keys, and the highest-signal source for smaller companies. It is deliberately
polite: robots.txt is honoured, one shared request delay applies, and crawling is capped.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..net import HttpError
from ..names import ROLE_WORDS, slug
from ..util import find_emails, linkedin_matches_name, linkedin_url
from .base import Source, rec

BLOCK_TAGS = {
    "p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "address", "dd", "dt", "br", "blockquote",
    "figcaption", "summary", "tr",
}

CONTACT_RE = re.compile(
    r"(team|about|people|staff|leader|management|employee|contact|company|"
    r"who-we-are|our-firm|attorneys?|lawyers?|doctors?|faculty|directory|"
    r"board|founders?|equipo|equipe|nosotros|qui-sommes|uber-uns|chi-siamo|"
    r"unser-team|notre-equipe|nuestro-equipo|mitarbeiter|medewerkers|"
    r"personal|profesionales|colaboradores|teamet|tiimi|zespol|tim)",
    re.I,
)

STANDARD_PATHS = [
    "/about", "/about-us", "/team", "/our-team", "/people", "/our-people",
    "/leadership", "/company", "/contact", "/contact-us", "/staff",
    "/who-we-are", "/management", "/employees", "/meet-the-team",
    "/about/team", "/about/people", "/company/team", "/company/about",
    "/en/about", "/about/leadership", "/careers", "/blog", "/news", "/press",
]

# Pages that are specifically about people get crawled before generic "about" pages.
STRONG_RE = re.compile(
    r"(team|staff|people|leadership|directory|faculty|employees|board|author|"
    r"founders|equipo|equipe|equip|mitarbeiter|medewerker|teamet|personal|"
    r"colaborador|zespol|tim)",
    re.I,
)

# Shared inboxes / role accounts are not employees.
GENERIC_LOCALPARTS = {
    "info", "sales", "support", "contact", "hello", "hi", "admin", "hr", "jobs",
    "careers", "marketing", "press", "media", "billing", "accounts", "accounting",
    "noreply", "no-reply", "donotreply", "webmaster", "postmaster", "office",
    "team", "enquiries", "enquiry", "inquiries", "inquiry", "mail", "email",
    "privacy", "legal", "security", "abuse", "devops", "it", "help", "helpdesk",
    "service", "services", "orders", "customerservice", "feedback", "general",
    "reception", "frontdesk", "partners", "partnerships", "investors", "ir",
    "recruiting", "recruitment", "talent", "events", "newsletter", "unsubscribe",
    "campaigns", "campaign", "licensing", "donate", "donations", "membership",
    "shop", "store", "website", "sysadmin", "systems", "grants", "awards",
    "podcast", "subscribe", "register", "login", "volunteer", "volunteers",
    "connect", "connections", "contactus", "talk", "chat", "join", "apply",
}


# A title like "CMO, Acalvio" or "Head of Sales - Rootstock" credits another company:
# that is a customer quote, not an employee, and no @target address can exist for them.
TITLE_SEPARATOR = re.compile(r"\s*(?:[,|–—\ufffd]|\s-\s|\s+at\s+|\s*@\s*)\s*", re.I)

# Trailing segments that are internal scope, not another company.
INTERNAL_TAIL = {
    "platform", "team", "teams", "global", "region", "regional", "amer", "americas",
    "emea", "apac", "latam", "us", "usa", "uk", "eu", "india", "europe", "asia",
    "africa", "anz", "mena", "product", "products", "engineering", "sales",
    "marketing", "growth", "revenue", "operations", "customer", "success",
    "data", "design", "finance", "legal", "people", "hr", "it", "security",
    "cloud", "mobile", "web", "api", "core", "infra", "infrastructure", "ai",
    "ml", "saas", "b2b", "b2c", "enterprise", "smb", "mid", "digital", "platforms",
    "services", "solutions", "technology", "technologies", "partners", "support",
    "content", "brand", "communications", "quality", "compliance", "strategy",
}


def company_tail(title: str) -> str:
    """Trailing company name in a job title, if any ('CMO, Acalvio' -> 'Acalvio')."""
    parts = [part.strip() for part in TITLE_SEPARATOR.split(title or "") if part.strip()]
    if len(parts) < 2:
        return ""
    tail = parts[-1].strip(" .;:")
    if not 3 <= len(tail) <= 40:
        return ""
    # "Ex-Uniphore" / "Former Cisco" is a previous employer, not a current one.
    if re.match(r"^(ex|former|formerly|previously|alumni|ex)\s*[-–:]\s*", tail, re.I):
        return ""
    words = [word for word in tail.split() if word]
    if not 1 <= len(words) <= 4:
        return ""
    if tail.lower() in INTERNAL_TAIL:
        return ""
    # A single trailing role word ("Head of Sales - Sales") is not a company name, but
    # a company may legitimately contain one ("Airtel Business", "Rootstock Software").
    if len(words) == 1 and words[0].lower().strip(".,") in ROLE_WORDS:
        return ""
    if not all(word[0].isupper() for word in words if word[0].isalpha()):
        return ""
    return tail


def is_external_title(title: str, domain: str) -> bool:
    """True when the title credits a company that is not the one being scanned."""
    tail = company_tail(title)
    if not tail:
        return False
    label = slug(domain.split(".")[0])
    candidate = slug(tail)
    if not label or not candidate:
        return False
    return not (candidate in label or label in candidate)


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list = []
        self.links: list = []
        self.jsonld: list = []
        # Document-ordered stream of ("block", text) and ("mailto", address) events,
        # used to attach an ambiguous mailto link to the person card before it.
        self.events: list = []
        self._buf: list = []
        self._a_href = None
        self._a_buf: list = []
        self._skip = 0
        self._script = None

    # -- tags --
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "template", "svg"):
            if tag == "script" and "ld+json" in (attrs.get("type") or "").lower():
                self._script = []
            else:
                self._skip += 1
            return
        if tag == "a":
            self._a_href = attrs.get("href")
            self._a_buf = []
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "svg"):
            if tag == "script" and self._script is not None:
                self.jsonld.append("".join(self._script))
                self._script = None
            elif self._skip:
                self._skip -= 1
            return
        if tag == "a" and self._a_href is not None:
            text = " ".join("".join(self._a_buf).split())
            self.links.append((self._a_href, text))
            if self._a_href.lower().startswith("mailto:"):
                address = self._a_href[7:].split("?", 1)[0].split(";", 1)[0].strip().lower()
                if address:
                    self.events.append(("mailto", address))
            elif "linkedin.com/in/" in self._a_href.lower():
                profile = linkedin_url(self._a_href)
                if profile:
                    self.events.append(("linkedin", profile))
            self._a_href = None
            self._a_buf = []
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
            return
        if self._skip:
            return
        # Separate text nodes with a space: inline tags split "James Hawkins" and
        # "Co-founder" into two data events that would otherwise be glued together.
        self._buf.append(" " + data)
        if self._a_href is not None:
            self._a_buf.append(" " + data)

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text:
            self.blocks.append(text)
            self.events.append(("block", text))
    def close(self):
        super().close()
        self._flush()


def _jsonld_people(chunks) -> list:
    """Walk JSON-LD blocks for schema.org Person nodes (name, jobTitle, email)."""
    people, stack = [], []
    for chunk in chunks:
        try:
            stack.append(json.loads(chunk))
        except (json.JSONDecodeError, TypeError):
            continue
    seen = set()
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(str(t).lower() in ("person", "employee") for t in types if t):
                people.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return people


def parse_html(html: str, url: str) -> dict:
    parser = _Parser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not kill the run
        pass
    text = "\n".join(parser.blocks)
    mailtos = []
    for href, anchor in parser.links:
        if href and href.lower().startswith("mailto:"):
            addr = href[7:].split("?", 1)[0].split(";", 1)[0].strip().lower()
            mailtos.append((addr, anchor))
    return {
        "url": url,
        "blocks": parser.blocks,
        "links": parser.links,
        "events": parser.events,
        "text": text,
        "emails": find_emails(text) + [a for a, _ in mailtos if a],
        "mailtos": mailtos,
        "linkedin": [
            profile
            for profile in (
                linkedin_url(href) for href, _ in parser.links
                if href and "linkedin.com/in/" in href.lower()
            )
            if profile
        ],
        "people": _jsonld_people(parser.jsonld),
    }


def is_generic(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    base = re.split(r"[._\-+]", local)[0]
    return local in GENERIC_LOCALPARTS or base in GENERIC_LOCALPARTS


HUMANS_TEAM = re.compile(r"/\*\s*TEAM\s*\*/(.*?)(?=/\*|\Z)", re.S | re.I)
HUMANS_FIELD = re.compile(r"^\s*([A-Za-z ]{2,20}):\s*(.+?)\s*$", re.M)


def parse_humans_txt(text: str) -> "list[dict]":
    """Parse the TEAM blocks of a humans.txt into {name, email, title, linkedin}."""
    people = []
    for block in HUMANS_TEAM.findall(text or ""):
        fields = {}
        for key, value in HUMANS_FIELD.findall(block):
            fields[key.strip().lower()] = value.strip()
        name = fields.get("name") or fields.get("nombre") or ""
        if not name:
            continue
        people.append({
            "name": name,
            "email": (fields.get("contact") or fields.get("email") or "").strip().lower(),
            "title": fields.get("role") or fields.get("position") or "",
            "linkedin": linkedin_url(fields.get("linkedin") or "")
                        or linkedin_url(fields.get("contact") or ""),
        })
    return people


class Web(Source):
    name = "web"
    label = "Company website"
    free = True

    # ------------------------------------------------------------------
    def _robots(self, ctx) -> "list[str]":
        cached = ctx.memo("robots")
        if cached is not None:
            return cached
        disallow: list = []
        try:
            text = ctx.http.get_text(f"https://{ctx.domain}/robots.txt", retries=1)
            active = False
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key, value = key.strip().lower(), value.strip()
                if key == "user-agent":
                    active = value == "*"
                elif key == "disallow" and active and value:
                    disallow.append(value)
        except HttpError:
            pass
        return ctx.remember("robots", disallow)

    @staticmethod
    def _blocked(path: str, rules: "list[str]") -> bool:
        for rule in rules:
            if rule == "/" or path.startswith(rule.rstrip("*")):
                return True
        return False

    def _humans(self, ctx) -> "list[dict]":
        try:
            text = ctx.http.get_text(f"https://{ctx.domain}/humans.txt", retries=1)
        except HttpError:
            return []
        if "/*" not in text:
            return []
        people = parse_humans_txt(text)
        if people:
            ctx.debug(f"humans.txt: {len(people)} team entr(ies)")
        return people

    def _fetch(self, ctx, url: str) -> "dict | None":
        try:
            html = ctx.http.get_text(
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                retries=1,
            )
        except HttpError:
            return None
        if not html or "<" not in html[:500]:
            return None
        return parse_html(html, url)

    # ------------------------------------------------------------------
    def run(self, ctx) -> list:
        domain = ctx.domain
        rules = self._robots(ctx)
        home = f"https://{domain}/"

        records: dict = {}

        def add_name(first, last, full, title, page_url, linkedin=""):
            """Get-or-create a person row. Returns the record so callers can enrich it."""
            from ..names import normalize_person, slug

            first, last, shown = normalize_person(first, last, full)
            if not (first or last or shown):
                return None
            key = ("n", slug(first), slug(last))
            record = records.get(key)
            if record is None:
                record = rec(
                    ctx, self.name, first=first, last=last, full=shown, title=title,
                    confidence=40, notes=[f"web: {page_url}"],
                )
                records[key] = record
            elif title and not record.title:
                record.title = title
            if linkedin and not record.linkedin:
                record.linkedin = linkedin
            return record


        def add_email(email, name="", title="", page_url=""):
            if not email or is_generic(email):
                return
            if not email.endswith("@" + domain):
                return
            key = ("e", email)
            existing = records.get(key)
            if existing:
                if not existing.full_name and name:
                    existing.full_name = name
                if not existing.title and title:
                    existing.title = title
                return
            first = last = ""
            if name:
                from ..names import split_name

                first, last = split_name(name)
            records[key] = rec(
                ctx, self.name, first=first, last=last, full=name, email=email,
                title=title, confidence=55, notes=[f"web: {page_url}"],
            )

        first_page = self._fetch(ctx, home)
        pages = 0
        queue: list = []
        if first_page:
            pages += 1
            self._harvest(ctx, first_page, add_name, add_email, queue)

        queue = [u for u in dict.fromkeys(queue) if u != home]
        for path in STANDARD_PATHS:
            queue.append(urljoin(home, path.lstrip("/")))
        queue = list(dict.fromkeys(queue))

        visited = {home}
        max_pages = max(1, ctx.opts.max_pages)
        while queue and pages < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            path = urlsplit(url).path or "/"
            if self._blocked(path, rules):
                ctx.debug(f"robots.txt disallows {path}, skipping")
                continue
            page = self._fetch(ctx, url)
            if not page:
                continue
            pages += 1
            self._harvest(ctx, page, add_name, add_email, queue)

        # humans.txt is a small, rarely-present but high-signal file: it often lists
        # team members with contact + social links in plain text.
        humans_url = f"https://{domain}/humans.txt"
        for person in self._humans(ctx):
            add_name("", "", person["name"], person.get("title", ""), humans_url,
                     linkedin=person.get("linkedin", ""))
            if person.get("email"):
                add_email(person["email"], person["name"], person.get("title", ""),
                          humans_url)

        with_linkedin = sum(1 for r in records.values() if r.linkedin)
        external = ctx.memo("web_external") or 0
        ctx.remember("web_pages", pages)
        ctx.info(f"    web: scanned {pages} page(s), {len(records)} candidate(s), "
                 f"{with_linkedin} with LinkedIn profile")
        if external:
            ctx.info(f"    web: skipped {external} card(s) crediting another company "
                     f"(customer testimonials, not employees)")
        return list(records.values())

    # ------------------------------------------------------------------
    def _harvest(self, ctx, page, add_name, add_email, queue) -> None:
        from ..names import (
            looks_like_name,
            looks_like_title,
            split_name_title,
        )

        url = page["url"]
        external = 0

        # 1. JSON-LD Person nodes (most structured evidence available)
        for node in page["people"]:
            name = str(node.get("name") or "").strip()
            title = str(node.get("jobTitle") or "")[:100]
            if not name or not looks_like_name(name):
                continue
            if title and is_external_title(title, ctx.domain):
                external += 1
                continue
            profile = ""
            same_as = node.get("sameAs") or node.get("url") or ""
            for candidate in ([same_as] if isinstance(same_as, str) else same_as):
                profile = profile or linkedin_url(str(candidate))
            record = add_name("", "", name, title, url, linkedin=profile)
            site = str(node.get("url") or "").strip()
            if record is not None and site.startswith(("http://", "https://")):
                if not record.website and "linkedin.com" not in site.lower():
                    record.website = site
            email = str(node.get("email") or "").replace("mailto:", "").strip().lower()
            if email:
                add_email(email, name, title, url)

        # 2. mailto links: the anchor text is usually the person's name
        for email, anchor in page["mailtos"]:
            if looks_like_name(anchor):
                add_email(email, anchor, "", url)

        # 3. person cards: "Name" then "Title", or both glued into one block.
        #    A name alone is not enough evidence - require the title next to it.
        blocks = page["blocks"]
        for index, block in enumerate(blocks):
            if "@" in block or len(block) > 120:
                continue
            merged = split_name_title(block)
            if merged:
                if is_external_title(merged[1], ctx.domain):
                    external += 1
                    continue
                add_name("", "", merged[0], merged[1], url)
                continue
            if not looks_like_name(block):
                continue
            for candidate in blocks[index + 1: index + 4]:
                if looks_like_title(candidate) and not looks_like_name(candidate):
                    if is_external_title(candidate, ctx.domain):
                        external += 1
                        break
                    add_name("", "", block, candidate[:80], url)
                    break

        # 4. mailto links whose anchor text is not a name: attach the address to the
        #    person card directly above it (name/title blocks in the same table row).
        window: list = []
        claimed: set = set()
        for kind, payload in page.get("events") or []:
            if kind == "block":
                window.append(payload)
                if len(window) > 6:
                    window.pop(0)
                continue
            if kind == "linkedin":
                # Only accept a profile whose slug contains the person's name - a
                # company page link next to a card would otherwise land on the wrong row.
                for block in reversed(window):
                    candidate = split_name_title(block)
                    name = candidate[0] if candidate else (
                        block if looks_like_name(block) else ""
                    )
                    if not name:
                        continue
                    from ..names import split_name

                    first, last = split_name(name)
                    if linkedin_matches_name(payload, first, last):
                        add_name(first, last, name, "", url, linkedin=payload)
                        break
                continue
            email = payload
            if not email or is_generic(email):
                continue
            for block in reversed(window):
                if block in claimed:
                    continue
                merged = split_name_title(block)
                if merged:
                    add_email(email, merged[0], merged[1], url)
                    claimed.add(block)
                    break
                if looks_like_name(block):
                    add_email(email, block, "", url)
                    claimed.add(block)
                    break

        # 5. bare addresses in page text
        for email in page["emails"]:
            add_email(email, "", "", url)

        if external:
            ctx.remember("web_external", (ctx.memo("web_external") or 0) + external)

        # 5. queue more on-site contact-ish pages
        for href, anchor in page["links"]:
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absolute = urljoin(url, href)
            parts = urlsplit(absolute)
            if parts.scheme not in ("http", "https"):
                continue
            host = (parts.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            # Stay on the exact host: sibling sub-sites (docs., directory., status.)
            # are not the company's people pages and would eat the page budget.
            if host != ctx.domain:
                continue
            target = absolute.split("#", 1)[0]
            if STRONG_RE.search(f"{parts.path} {anchor}"):
                if len(queue) < 400:
                    queue.insert(0, target)
            elif CONTACT_RE.search(f"{parts.path} {anchor}"):
                if len(queue) < 400:
                    queue.append(target)
