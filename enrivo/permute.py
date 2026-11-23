"""Email permutation ladder.

Given a person's name, produce the conventional work addresses in the order a human
would try them. The resolver walks the ladder one round at a time and stops at the first
address the mail server accepts, so a hit at step 2 costs two probes, not eighteen.

Names harvested from GitHub (and company pages) are not always clean: `jane99`,
`john_dev`, `s3raj`, `jane.doe`, or a login like `jane_doe` with no surname at all.
`name_variants()` derives the plausible spellings for such a name, and `candidates()`
runs the whole ladder for the most plausible one first.
"""

from __future__ import annotations

import re

from .names import alpha as _alpha
from .names import slug, strip_accents
from .patterns import render

# (template, short label). Templates match patterns.py syntax.
# The first eight are the classic ladder; the rest are the conventions that show up
# often enough in the wild to be worth a probe.
LADDER = [
    ("{first}.{last}", "first.last"),
    ("{first}", "first"),
    ("{first}{last}", "firstlast"),
    ("{f}.{last}", "f.last"),
    ("{f}{last}", "f+last"),
    ("{first}{l}", "firstl"),
    ("{first}_{last}", "first_last"),
    ("{last}.{first}", "last.first"),
    ("{f}{l}", "fl"),
    ("{last}", "last"),
    ("{last}{first}", "lastfirst"),
    ("{last}_{first}", "last_first"),
    ("{first}.{l}", "first.l"),
    ("{first}-{last}", "first-last"),
    ("{f}-{last}", "f-last"),
    ("{last}.{f}", "last.f"),
    ("{last}{f}", "lastf"),
    ("{f}_{last}", "f_last"),
    ("{first}_{l}", "first_l"),
]

MIN_LOCAL_LENGTH = 2

_NON_LETTERS = re.compile(r"[^A-Za-z]+")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


def fold(text: str) -> str:
    """Lowercase + accent-folded, separators preserved: 'Zoë_99' -> 'zoe_99'."""
    return strip_accents(text or "").lower()


def alpha(text: str) -> str:
    """Letters only: 'Jane99' -> 'jane', 'Zoë' -> 'zoe', "O'Brien" -> 'obrien'."""
    return _alpha(text)


def truncate_at_special(text: str) -> str:
    """Leading letters before the first digit or separator: 'jane_doe99' -> 'jane'."""
    match = re.match(r"[a-z]+", fold(text))
    return match.group(0) if match else ""


def split_parts(text: str) -> "list[str]":
    """'jane_doe99' -> ['jane', 'doe99']; 'jane.doe' -> ['jane', 'doe']."""
    return [part for part in _SEPARATORS.split(fold(text)) if part]


def name_variants(first: str, last: str) -> "list[tuple[str, str, str]]":
    """Plausible (first, last) spellings, most likely first.

    Labels are for the report notes: clean / raw / truncated / clean-surname /
    surname-split / split.
    """
    raw_first, raw_last = slug(first), slug(last)
    clean_first, clean_last = alpha(first), alpha(last)
    variants: "list[tuple[str, str, str]]" = []
    seen: set = set()

    def add(label: str, sub_first: str, sub_last: str,
            allow_missing_last: bool = False) -> None:
        if not sub_first:
            return
        if not sub_last and not allow_missing_last:
            return
        key = (sub_first, sub_last)
        if key in seen:
            return
        seen.add(key)
        variants.append((label, sub_first, sub_last))

    # 1. letters only. "jane99" and "john_dev" lose the GitHub decoration.
    if clean_first and clean_last and (clean_first, clean_last) != (raw_first, raw_last):
        add("clean", clean_first, clean_last)

    # 2. surname baked onto the given name: "janesmith" + "smith" -> jane / smith
    if raw_last and raw_first.endswith(raw_last) and len(raw_first) > len(raw_last):
        add("surname-split", raw_first[: -len(raw_last)], raw_last)

    # 3. cut at the first digit/separator: "jane_doe99" -> "jane"
    truncated = truncate_at_special(first)
    if truncated and truncated != clean_first and (clean_last or raw_last):
        add("truncated", truncated, clean_last or raw_last)

    # 4. the digits are in the surname: "Doe99"
    if clean_last and clean_last != raw_last:
        add("clean-surname", raw_first, clean_last)

    # 5. exactly as stored (also the only variant for an already-clean name)
    add("raw", raw_first, raw_last)

    # 6. no surname at all: a login like "jane_doe" already contains both parts
    if not raw_last:
        pieces = split_parts(first)
        if len(pieces) >= 2:
            add("split", pieces[0], pieces[-1])

    # 7. a single, already-clean token: "cher@domain" is the only convention left.
    #    Deliberately skipped for dirty tokens ("s3raj"), where the guess would be
    #    a different word than the person's name.
    if raw_first and not raw_last and raw_first == clean_first:
        add("first-only", raw_first, "", allow_missing_last=True)

    return variants


def local_candidates(first: str, last: str) -> "list[tuple[str, str]]":
    """Ordered, de-duplicated [(label, local_part)] for one spelling of a name."""
    out: "list[tuple[str, str]]" = []
    seen: set = set()
    for template, label in LADDER:
        local = render(template, first, last)
        if not local or len(local) < MIN_LOCAL_LENGTH or local in seen:
            continue
        seen.add(local)
        out.append((label, local))
    return out


def candidates(
    first: str,
    last: str,
    domain: str,
    preferred=(),
) -> "list[tuple[str, str]]":
    """Candidate addresses for a person, best guess first.

    `preferred` holds patterns already confirmed for the domain (from observed emails);
    they are tried before the generic ladder. The ladder runs to completion for the most
    plausible name spelling before moving on to the next one.
    """
    variants = name_variants(first, last)
    if not variants:
        return []

    multi = len(variants) > 1
    steps: "list[tuple[str, str]]" = []
    seen: set = set()

    def add(label: str, template: str, variant: str, sub_first: str, sub_last: str) -> None:
        local = render(template, sub_first, sub_last)
        if not local or len(local) < MIN_LOCAL_LENGTH or local in seen:
            return
        seen.add(local)
        suffix = f" ({variant})" if multi and variant != "raw" else ""
        steps.append((label + suffix, f"{local}@{domain}".lower()))

    for variant_label, variant_first, variant_last in variants:
        for template in preferred or ():
            add(str(template), str(template), variant_label, variant_first, variant_last)
        for template, label in LADDER:
            add(label, template, variant_label, variant_first, variant_last)
    return steps


def explain_order(max_items: int = 12) -> "list[tuple[int, str, str]]":
    """Human-readable ladder for docs/CLI: [(step, label, pattern)]."""
    return [
        (index + 1, label, template)
        for index, (template, label) in enumerate(LADDER[:max_items])
    ]
