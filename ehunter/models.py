"""Core data types shared by every source."""

from __future__ import annotations

from dataclasses import dataclass, field

# Higher = more trustworthy. Used for confidence blending and output ordering.
STATUS_RANK = {
    "valid": 6,
    "unverified": 4,
    "unknown": 3,
    "catch_all": 2,
    "greylisted": 2,
    "risky": 2,
    "pattern": 1,
    "name_only": 1,
    "invalid": 0,
}

CSV_COLUMNS = [
    "domain",
    "full_name",
    "first_name",
    "last_name",
    "title",
    "email",
    "email_status",
    "confidence",
    "pattern",
    "linkedin",
    "linkedin_status",
    "github",
    "website",
    "sources",
    "notes",
]


@dataclass
class Record:
    """One row: a person, a person+email, or sometimes only an email."""

    domain: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    title: str = ""
    email: str = ""
    status: str = "unverified"
    confidence: int = 0
    pattern: str = ""
    linkedin: str = ""
    linkedin_status: str = ""
    github: str = ""
    website: str = ""
    sources: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    # ---- identity helpers -------------------------------------------------
    def person_key(self) -> str:
        from .names import alpha

        return f"{alpha(self.first_name)}|{alpha(self.last_name)}"

    def email_key(self) -> str:
        return self.email.strip().lower()

    def finalize(self) -> "Record":
        from .names import normalize_person

        self.email = self.email_key()
        self.first_name, self.last_name, self.full_name = normalize_person(
            self.first_name, self.last_name, self.full_name
        )
        self.title = " ".join(self.title.split())[:160]
        self.sources = sorted({s for s in self.sources if s})
        self.confidence = max(0, min(100, int(self.confidence)))
        return self

    # ---- merging ---------------------------------------------------------
    def merge(self, other: "Record") -> None:
        """Fold `other` into self. Never overwrite good data with empty data."""
        if not self.email and other.email:
            self.email = other.email
        if not self.first_name and other.first_name:
            self.first_name = other.first_name
        if not self.last_name and other.last_name:
            self.last_name = other.last_name
        if not self.full_name and other.full_name:
            self.full_name = other.full_name
        if not self.title and other.title:
            self.title = other.title
        if not self.linkedin and other.linkedin:
            self.linkedin = other.linkedin
        if not self.linkedin_status and other.linkedin_status:
            self.linkedin_status = other.linkedin_status
        if not self.github and other.github:
            self.github = other.github
        if not self.website and other.website:
            self.website = other.website
        if not self.pattern and other.pattern:
            self.pattern = other.pattern
        self.sources = sorted(set(self.sources) | set(other.sources))
        for note in other.notes:
            if note and note not in self.notes:
                self.notes.append(note)
        # Status/confidence: keep the strongest signal seen.
        if STATUS_RANK.get(other.status, 0) > STATUS_RANK.get(self.status, 0):
            self.status = other.status
        self.confidence = max(self.confidence, other.confidence)

    def to_row(self) -> dict:
        return {
            "domain": self.domain,
            "full_name": self.full_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "title": self.title,
            "email": self.email,
            "email_status": self.status,
            "confidence": self.confidence,
            "pattern": self.pattern,
            "linkedin": self.linkedin,
            "linkedin_status": self.linkedin_status,
            "github": self.github,
            "website": self.website,
            "sources": ";".join(self.sources),
            "notes": "; ".join(self.notes),
        }


@dataclass
class SourceResult:
    source: str
    ok: bool = True
    count: int = 0
    error: str = ""
    skipped: str = ""
    records: list = field(default_factory=list)
