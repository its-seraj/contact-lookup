"""Name parsing, normalisation and person-name heuristics (stdlib only, unicode-safe)."""

from __future__ import annotations

import re
import unicodedata

SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "v", "vi",
    "phd", "ph.d", "md", "m.d", "mba", "msc", "bsc", "ba", "ma",
    "cpa", "cfa", "pe", "pmp", "esq", "dds", "dvm", "rn", "jd",
}

PARTICLES = {
    "van", "von", "der", "den", "de", "del", "della", "der", "di", "da",
    "du", "la", "le", "bin", "ibn", "al", "el", "dos", "das", "ter", "ten",
    "af", "av", "zu", "zum", "zur", "op", "in", "t", "y",
}

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "mx"}

# Words that are never a person's name (navigation, roles, boilerplate).
NAME_STOP = {
    "about", "about us", "our team", "team", "leadership", "management", "contact",
    "contact us", "read more", "learn more", "privacy policy", "privacy", "terms",
    "terms of service", "careers", "jobs", "home", "blog", "news", "press", "events",
    "products", "solutions", "services", "customers", "pricing", "support", "login",
    "sign up", "sign in", "get started", "company", "who we are", "board of directors",
    "resources", "partners", "menu", "close", "search", "next", "previous", "more",
    "all rights reserved", "cookie policy", "legal", "investors", "culture", "values",
    "our story", "mission", "overview", "faq", "documentation", "docs", "api",
    "case studies", "testimonials", "integrations", "security", "status", "changelog",
    "app", "apps", "docs", "documentation", "api", "sdk", "guide", "tutorial",
    "store", "product", "features", "pricing", "login", "signup", "help",
    "platform", "download", "extension", "plugin", "template", "demo", "trial",
}

ROLE_WORDS = {
    "ceo", "cto", "cfo", "coo", "cmo", "ciso", "cio", "cpo", "chro", "vp", "svp",
    "evp", "founder", "co-founder", "cofounder", "president", "director", "manager",
    "head", "lead", "engineer", "developer", "designer", "architect", "analyst",
    "consultant", "specialist", "coordinator", "assistant", "intern", "advisor",
    "partner", "principal", "scientist", "researcher", "recruiter", "accountant",
    "attorney", "counsel", "producer", "editor", "writer", "strategist",
    # job-title vocabulary that otherwise looks like a capitalised name
    "chief", "officer", "senior", "junior", "vice", "executive", "general",
    "technical", "technology", "product", "operations", "operating", "finance",
    "financial", "human", "resources", "business", "development", "research",
    "data", "software", "hardware", "marketing", "customer", "success", "growth",
    "strategy", "legal", "compliance", "quality", "project", "program", "account",
    "client", "digital", "content", "brand", "communications", "engineering",
    "design", "sales", "revenue", "security", "administrator", "associate",
    "chairman", "chairwoman", "board", "member", "department", "division",
    "office", "management", "director", "owner", "co-founder", "specialist",
    "supervisor", "technician", "representative", "executive", "coordinator",
    "deputy", "interim", "acting", "vice",
}

# Words that mark a string as an organisation, not a person.
COMPANY_WORDS = {
    "inc", "llc", "ltd", "limited", "gmbh", "ag", "sa", "bv", "nv", "plc", "corp",
    "corporation", "company", "group", "holdings", "solutions", "systems",
    "technologies", "technology", "labs", "laboratory", "consulting", "partners",
    "associates", "agency", "studio", "studios", "ventures", "capital", "bank",
    "university", "college", "school", "institute", "foundation", "clinic",
    "hospital", "press", "media", "networks", "software", "services", "global",
}

_ASCII_MAP = {
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th", "ı": "i", "å": "a", "Å": "A",
}


def strip_accents(text: str) -> str:
    """Unicode -> ASCII-ish, keeping the letter skeleton (Müller -> Muller, Łukasz -> Lukasz)."""
    if not text:
        return ""
    out = []
    for ch in unicodedata.normalize("NFKD", text):
        if ch in _ASCII_MAP:
            out.append(_ASCII_MAP[ch])
        elif unicodedata.combining(ch):
            continue
        else:
            out.append(ch)
    return "".join(out).encode("ascii", "ignore").decode("ascii")


def alpha(text: str) -> str:
    """Letters only, lowercased: 'Jane99' -> 'jane'. Used for person identity."""
    return re.sub(r"[^a-z]", "", strip_accents(text or "").lower())


def slug(text: str) -> str:
    """Lowercase alphanumeric-only key used for de-duplication and local-part building."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text or "").lower())


def _clean_tokens(text: str) -> list:
    text = (text or "").replace("\u00a0", " ")
    text = re.sub(r"[()\[\]{}\"“”‘’*]", " ", text)
    toks = [t.strip(",;:") for t in text.split()]
    return [t for t in toks if t and t not in ("-", "–", "—", "|")]


def _is_suffix(tok: str) -> bool:
    return tok.lower().replace(".", "") in SUFFIXES


def _is_honorific(tok: str) -> bool:
    return tok.lower().replace(".", "") in HONORIFICS


def split_name(full: str) -> "tuple[str, str]":
    """Split a display name into (first, last). Handles 'Last, First', suffixes and particles."""
    s = (full or "").strip().rstrip(",;")
    if not s:
        return "", ""
    if "," in s:
        left, right = s.split(",", 1)
        a, b = _clean_tokens(left), _clean_tokens(right)
        if len(a) == 1 and b:
            # "Doe, John A." -> John / Doe
            while b and (_is_suffix(b[-1]) or _is_honorific(b[0])):
                if _is_suffix(b[-1]):
                    b.pop()
                if b and _is_honorific(b[0]):
                    b.pop(0)
            if b:
                return b[0], a[0]
        s = " ".join(a + b) if (a and b) else s.replace(",", " ")

    toks = _clean_tokens(s)
    while toks and _is_honorific(toks[0]):
        toks.pop(0)
    while len(toks) > 1 and _is_suffix(toks[-1]):
        toks.pop()
    if not toks:
        return "", ""
    if len(toks) == 1:
        return toks[0].rstrip("."), ""

    first = toks[0].rstrip(".")
    # Pull lowercase particles ("van der Berg") into the surname.
    start = len(toks) - 1
    j = len(toks) - 2
    while j >= 1 and toks[j].lower() in PARTICLES and toks[j][:1].islower():
        start = j
        j -= 1
    last = " ".join(t.rstrip(".") for t in toks[start:])
    return first, last


def _fix_case(tok: str) -> str:
    if not tok:
        return tok
    if tok.isupper() or tok.islower():
        return tok[:1].upper() + tok[1:].lower()
    return tok


# Words that unambiguously signal a job title. Kept narrow on purpose: vague nouns
# ("data", "security", "product") appear in ordinary prose and would create false pairs.
TITLE_WORDS = {
    "ceo", "cto", "cfo", "coo", "cmo", "ciso", "cio", "cpo", "chro", "vp", "svp",
    "evp", "president", "founder", "cofounder", "director", "manager", "head",
    "lead", "engineer", "developer", "designer", "architect", "analyst",
    "consultant", "specialist", "coordinator", "assistant", "intern", "advisor",
    "partner", "principal", "scientist", "researcher", "recruiter", "accountant",
    "attorney", "counsel", "producer", "editor", "writer", "strategist",
    "officer", "chief", "chairman", "chairwoman", "chair", "treasurer",
    "secretary", "professor", "lecturer", "physician", "nurse", "therapist",
    "paralegal", "librarian", "curator", "coach", "trainer", "supervisor",
    "technician", "representative", "administrator", "owner", "executive",
    "senior", "junior", "associate", "apprentice", "trainee", "fellow",
    "scholar", "receptionist", "clerk", "dispatcher", "inspector", "estimator",
    "buyer", "planner", "auditor", "controller", "paralegal", "evangelist",
    "deputy", "interim", "acting", "vice", "assistant",
}


def looks_like_title(text: str) -> bool:
    """True for strings that read like a job title ('Head of Sales', 'Co-founder')."""
    s = re.sub(r"\s+", " ", (text or "").strip().strip("|,-–—•·:;"))
    if not s or len(s) > 90 or "@" in s:
        return False
    if len(s.split()) > 12:
        return False
    tokens = s.lower().replace("/", " ").replace("-", " ").split()
    return any(token.strip(".,()") in TITLE_WORDS for token in tokens)


def split_name_title(block: str) -> "tuple[str, str] | None":
    """Split 'James Hawkins Co-founder' into ('James Hawkins', 'Co-founder')."""
    if not block or len(block) > 120 or "@" in block:
        return None
    words = block.split()
    if len(words) < 2:
        return None
    # Longest name prefix first: "Ana Maria Ruiz Head of Sales" must split after "Ruiz".
    for cut in range(min(len(words) - 1, 5), 0, -1):
        name = " ".join(words[:cut])
        rest = " ".join(words[cut:])
        if looks_like_name(name) and looks_like_title(rest):
            # "Sean O'Brien, Deputy Director" -> drop the clause after the comma
            return name.split(",")[0].strip(), rest[:90]
    return None


def normalize_person(first: str, last: str, full: str = "") -> "tuple[str, str, str]":
    """Return a consistent (first, last, full) triple, filling gaps in either direction."""
    first = (first or "").strip()
    last = (last or "").strip()
    full = (full or "").strip()

    if (not first or not last) and full:
        f2, l2 = split_name(full)
        first = first or f2
        last = last or l2
    if not first and not last and full:
        first, last = split_name(full)

    first = " ".join(_fix_case(t) for t in first.split())
    last = " ".join(
        _fix_case(t) if t.lower() not in PARTICLES else t.lower() for t in last.split()
    )
    if not full:
        full = f"{first} {last}".strip()
    elif slug(full) == slug(f"{first}{last}"):
        # Same words in the same order, just badly cased upstream.
        full = f"{first} {last}".strip()
    return first, last, full.strip()


def _word_ok(word: str) -> bool:
    core = word.strip(".,()'\"’")
    if not core or len(core) < 2:
        return False
    if core.lower().rstrip(".") in ROLE_WORDS:
        return False
    if core.lower() in NAME_STOP or core.lower() in COMPANY_WORDS:
        return False
    if not core[0].isalpha() or not core[0].isupper():
        return False
    return all(c.isalpha() or c in "'’-." for c in core)


def looks_like_name(text: str) -> bool:
    """Cheap precision-first test used when lifting names out of scraped pages."""
    s = (text or "").strip().strip("|,-–—•·:;<>")
    s = re.sub(r"\s+", " ", s)
    if not s or len(s) > 70:
        return False
    low = s.lower()
    if low in NAME_STOP:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    if "@" in s or "&" in s or "http" in low or "www." in low:
        return False
    words = s.split()
    if not 2 <= len(words) <= 4:
        return False
    for index, word in enumerate(words):
        # Lowercase surname particles ("Jan van der Berg") are legitimate after word 1.
        if index > 0 and word.islower() and word.strip(".") in PARTICLES:
            continue
        if not _word_ok(word):
            return False
    # Reject role-title shaped strings ("Senior Software Engineer").
    if sum(1 for w in words if w.lower().rstrip(".,") in ROLE_WORDS) >= 1 and len(words) >= 3:
        return False
    return True


def email_localpart(email: str) -> str:
    return email.split("@", 1)[0].strip().lower()
