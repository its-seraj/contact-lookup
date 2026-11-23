"""Orchestration: run sources, merge, infer the email pattern, generate, verify, rank."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import patterns, smtp
from .context import Context, Options
from .dnsutil import mail_posture
from .models import STATUS_RANK, Record, SourceResult
from .net import DEFAULT_UA, HttpClient, HttpError
from .sources import build_sources
from .sweep import build_records
from .util import LINKEDIN_RE, linkedin_matches_name, linkedin_url
from .sources.web import is_generic


@dataclass
class ScanResult:
    domain: str
    records: "list[Record]" = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    sources: "list[SourceResult]" = field(default_factory=list)
    pattern_hits: list = field(default_factory=list)


def _guarded(src, ctx) -> SourceResult:
    try:
        found = src.run(ctx)
        found = [r for r in found if isinstance(r, Record)]
        return SourceResult(source=src.name, ok=True, count=len(found), records=found)
    except Exception as exc:  # noqa: BLE001 - a failing source must not abort the scan
        return SourceResult(
            source=src.name, ok=False,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )


def run_sources(ctx, log) -> "list[SourceResult]":
    spec = ctx.opts.sources
    if not isinstance(spec, str):
        spec = ",".join(spec or [])
    sources = build_sources(spec)
    results: "list[SourceResult]" = []
    runnable = []
    for src in sources:
        if src.available():
            runnable.append(src)
        else:
            results.append(SourceResult(source=src.name, ok=False, skipped=src.skip_reason()))
            log(f"  [-] {src.label}: skipped ({src.skip_reason()})")

    if runnable:
        with ThreadPoolExecutor(max_workers=min(8, len(runnable))) as pool:
            futures = {pool.submit(_guarded, src, ctx): src for src in runnable}
            for future in as_completed(futures):
                src = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = SourceResult(
                        source=src.name, ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                results.append(result)
                if result.ok:
                    log(f"  [+] {src.label}: {result.count} record(s)")
                else:
                    log(f"  [x] {src.label}: {result.error}")

    order = {s.name: i for i, s in enumerate(sources)}
    results.sort(key=lambda r: order.get(r.source, 99))
    return results


# ---------------------------------------------------------------- linkedin input
def _linkedin_records(ctx, opts, log) -> "list[Record]":
    """Every LinkedIn contact we know about becomes a pipeline row, so the ladder can
    try to turn their name into an address."""
    profiles: "list[dict]" = []
    if opts.linkedin_sweep:
        from .config import env
        from .sources.search import Search
        from .sweep import company_names, site_title

        if not (env("SERPER_API_KEY") or env("BRAVE_API_KEY")
                or (env("GOOGLE_CSE_KEY") and env("GOOGLE_CSE_CX")) or env("SEARXNG_URL")):
            log("  [!] linkedin: no search provider configured, skipping the LinkedIn sweep")
            return []
        ctx.remember("company_names", company_names(ctx.domain, site_title(ctx)))
        profiles.extend(Search().sweep_linkedin(
            ctx, depth=max(1, opts.sweep_depth), max_variants=max(1, opts.sweep_variants)
        ))
    if opts.linkedin_file:
        from .sweep import read_profiles

        loaded = read_profiles(opts.linkedin_file)
        log(f"  [i] linkedin: {len(loaded)} profile(s) loaded from {opts.linkedin_file}")
        profiles.extend(loaded)

    if not profiles:
        return []

    # One row per profile, even when several queries matched it.
    unique: dict = {}
    for profile in profiles:
        key = (profile.get("slug") or "").lower() or (profile.get("url") or "").lower()
        if not key:
            continue
        existing = unique.get(key)
        if existing is None:
            unique[key] = dict(profile)
        else:
            for query in profile.get("queries") or []:
                if query not in existing["queries"]:
                    existing["queries"].append(query)
            if not existing.get("full_name") and profile.get("full_name"):
                existing["full_name"] = profile["full_name"]

    # Former employees no longer receive mail there; probing them only burns throttles.
    former = [p for p in unique.values() if (p.get("employment") or "") == "former"]
    kept = [p for p in unique.values() if (p.get("employment") or "") != "former"]

    records = build_records(ctx, kept)
    if former:
        log(f"  [i] linkedin: skipped {len(former)} former-employee profile(s)")
    log(f"  [i] linkedin: {len(records)} named contact(s) queued for email resolution")
    return records


# ---------------------------------------------------------------- assembly
def dedupe_records(records) -> "list[Record]":
    """Drop the same person+address appearing twice (guards against double-append bugs)."""
    seen, out = set(), []
    for record in records:
        identity = (record.person_key(), record.email_key(),
                    (record.github or "").lower())
        if identity in seen:
            continue
        seen.add(identity)
        out.append(record)
    return out


# ---------------------------------------------------------------- merging
def merge_records(records) -> "tuple[dict, dict]":
    """Collapse to {email: Record} plus {person_key: Record} for name-only people."""
    by_email: dict = {}
    name_only: dict = {}

    for record in records:
        if record.email:
            key = record.email_key()
            if key in by_email:
                by_email[key].merge(record)
            else:
                record.finalize()
                by_email[key] = record
        else:
            key = record.person_key()
            if key in ("|", ""):
                # No name and no email: keep it when it carries a handle (GitHub member
                # without a display name), so nothing from a source is silently lost.
                if record.github:
                    key = f"github:{record.github.lower()}"
                else:
                    continue
            if key in name_only:
                name_only[key].merge(record)
            else:
                record.finalize()
                name_only[key] = record

    # Fold name-only rows into the matching email row (same person).
    for key in list(name_only):
        matches = [e for e, r in by_email.items() if r.person_key() == key]
        if matches:
            for email in matches:
                by_email[email].merge(name_only[key])
            name_only.pop(key)
    return by_email, name_only


# ---------------------------------------------------------------- patterns
def infer_pattern(ctx, by_email) -> list:
    pairs = [
        (r.first_name, r.last_name, r.email)
        for r in by_email.values()
        if r.first_name and r.last_name and r.email
    ]
    hits = patterns.infer(pairs)
    hunter_pattern = ctx.memo("hunter_pattern") or ""
    if hunter_pattern and hunter_pattern not in [h.pattern for h in hits]:
        # Hunter's own conclusion for the domain is authoritative for position 0.
        hits.insert(0, patterns.PatternHit(
            pattern=hunter_pattern,
            matched=max(1, len(pairs) // 4),
            total=max(len(pairs), 1),
        ))
    return hits[: max(1, ctx.opts.max_patterns)]


def generate_emails(ctx, name_only, by_email, hits) -> "list[Record]":
    if not hits:
        return []
    # One confirmed example is not a convention, it is a coincidence. Hunter's own
    # domain-level conclusion is allowed to stand alone; our own inference is not.
    if hits[0].matched < 2 and not ctx.memo("hunter_pattern"):
        ctx.info("  [!] pattern: only one confirming example, refusing to generate addresses")
        return []
    primary = hits[0].pattern
    alts = [h.pattern for h in hits[1:]]
    created: "list[Record]" = []

    for key in list(name_only.keys()):
        record = name_only[key]
        if not (record.first_name and record.last_name):
            continue
        email = patterns.build(primary, record.first_name, record.last_name, ctx.domain)
        if not email:
            continue
        existing = by_email.get(email)
        if existing is not None:
            if existing.person_key() == record.person_key():
                existing.merge(record)
            else:
                # Local-part collision: this convention cannot be trusted for the pair.
                record.notes.append(f"pattern collision on {email}")
            continue

        confidence = int(hits[0].confidence * 0.5)
        new = Record(
            domain=ctx.domain,
            first_name=record.first_name,
            last_name=record.last_name,
            full_name=record.full_name,
            title=record.title,
            email=email,
            status="pattern",
            confidence=confidence,
            pattern=primary,
            linkedin=record.linkedin,
            sources=list(record.sources),
            notes=[f"inferred {primary}"] + list(record.notes),
        )
        for alt in alts:
            alt_email = patterns.build(alt, record.first_name, record.last_name, ctx.domain)
            if alt_email and alt_email != email:
                new.notes.append(f"alt: {alt_email}")
        new.finalize()
        created.append(new)
        by_email[email] = new
        name_only.pop(key)
    return created


# ---------------------------------------------------------------- linkedin
def _website_linkedin(ctx, record) -> str:
    """The person's own site (GitHub profile link, JSON-LD url) often links their profile."""
    site = record.website
    if not site:
        return ""
    key = ("website_li", site)
    cached = ctx.memo(key)
    if cached is not None:
        return cached
    try:
        page = ctx.http.get_text(site, headers={"Accept": "text/html"}, retries=1)
    except HttpError:
        return ctx.remember(key, "")
    found = ""
    for match in LINKEDIN_RE.finditer(page or ""):
        profile = linkedin_url(match.group(0))
        if profile and linkedin_matches_name(profile, record.first_name, record.last_name):
            found = profile
            break
    return ctx.remember(key, found)


def enrich_linkedin(ctx, records, log) -> dict:
    """Three keyless channels, cheapest first: Gravatar, their own website, then search."""
    limit = int(ctx.opts.linkedin_lookups or 0)
    counts = {"gravatar": 0, "website": 0, "search": 0, "none": 0}
    if limit <= 0:
        return counts
    targets = [r for r in records if r.full_name and not r.linkedin][:limit]
    if not targets:
        log("  [i] linkedin: no named person left to look up")
        return counts

    from . import gravatar
    from .sources.search import Search

    searcher = Search()
    search_alive = True
    log(f"  [*] linkedin: {len(targets)} person(s) without a profile URL")

    for record in targets:
        profile, how = "", ""

        if record.email:
            profile = gravatar.lookup(ctx, record.email)
            if profile:
                how = "gravatar"

        if not profile and record.website:
            profile = _website_linkedin(ctx, record)
            if profile:
                how = "their own site"

        if not profile and search_alive and record.first_name and record.last_name:
            try:
                profile = searcher.lookup_linkedin(ctx, record)
            except Exception as exc:  # noqa: BLE001
                ctx.debug(f"linkedin search failed for {record.full_name}: {exc}")
                profile = ""
            if profile:
                how = "search"
            else:
                dead = searcher._dead_providers(ctx)
                if dead and len(dead) >= len(searcher._providers(ctx)):
                    search_alive = False
                    log("  [!] linkedin: no search engine is answering - only published "
                        "links and Gravatar are being used (set SEARXNG_URL or a search key "
                        "to resolve the rest)")
            time.sleep(0.6)

        if profile:
            record.linkedin = profile
            record.notes.append(f"linkedin: {how}")
            counts["website" if how == "their own site" else how] += 1
        else:
            counts["none"] += 1

    summary = ", ".join(f"{key}={value}" for key, value in counts.items() if value)
    log(f"  [*] linkedin: {summary}")
    return counts

def _needs_resolution(record) -> bool:
    """People we still have to find an address for: nothing yet, or discovery failed.

    A surname is no longer mandatory: a login like "jane_doe" carries both parts and
    permute.name_variants() can split it.
    """
    if not record.first_name:
        return False
    if record.status == "invalid":
        return True
    return not record.email


def _drop(pending: list, record) -> None:
    """Remove by identity - dataclass __eq__ would match an equal-looking twin."""
    for index, item in enumerate(pending):
        if item is record:
            pending.pop(index)
            return


SAMPLE_SIZE = 6
_VERIFIED_NOTE = "verified by permutation: "


def _learned_templates(sample) -> list:
    """Conventions that verified for the sample, most common first, as pattern templates."""
    from . import permute

    by_label = {label: template for template, label in permute.LADDER}
    tally: dict = {}
    for record in sample:
        for note in record.notes:
            if note.startswith(_VERIFIED_NOTE):
                label = note[len(_VERIFIED_NOTE):].split(" (", 1)[0]
                if label in by_label:
                    tally[by_label[label]] = tally.get(by_label[label], 0) + 1
    return sorted(tally, key=lambda template: -tally[template])


def _control_address(ctx, records) -> str:
    """A known-good address on the scanned domain: the user's, else one we verified."""
    suffix = "@" + ctx.domain.lower()
    control = (ctx.opts.control_address or "").strip().lower()
    if control.endswith(suffix):
        return control
    verified = sorted(r.email.lower() for r in records
                      if r.email and r.status == "valid" and r.email.lower().endswith(suffix))
    return verified[0] if verified else ""


def _retry_greylisted(ctx, deferred: dict, claimed: set, counts: dict, log,
                      verify_fn, sleep_fn, control: str) -> None:
    """Re-probe addresses that answered 4xx, after the wait real greylisting asks for.

    `deferred` maps address -> (record, label). Outcomes: accepted -> promoted to
    valid/catch_all; still 4xx -> stays greylisted; rejected -> the guess was wrong,
    the person goes back to a name-only row.
    """
    attempts = max(0, int(ctx.opts.greylist_retries))
    wait = max(0.0, float(ctx.opts.greylist_wait))
    for attempt in range(1, attempts + 1):
        open_items = {e: v for e, v in deferred.items()
                      if v[0].status == "greylisted" and e not in claimed}
        if not open_items:
            return
        log(f"  [*] permutation: {len(open_items)} address(es) answered 4xx (\"try "
            f"again later\") - waiting {wait:.0f}s, then re-checking them "
            f"(attempt {attempt}/{attempts}). No other probes run meanwhile.")
        sleep_fn(wait)
        try:
            results = verify_fn(
                list(open_items), ctx.http, from_addr=ctx.opts.from_addr,
                timeout=ctx.opts.verify_timeout,
                threads=max(1, min(4, ctx.opts.threads)),
                delay=ctx.opts.verify_delay, log=None, control=control,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  [!] permutation: greylist retry error ({type(exc).__name__}: {exc})")
            return
        promoted = still_deferred = rejected = 0
        for email, (record, label) in open_items.items():
            result = results.get(email)
            if result is None or result.status in ("unknown", "greylisted"):
                still_deferred += 1
                reply = smtp.reply_text(result)
                if reply:
                    record.notes.append(f"retry {attempt}: still no answer ({reply})")
                continue
            if result.status == "valid":
                promoted += 1
                record.status = "valid"
                record.confidence = max(record.confidence, 95)
                record.notes.append(f"verified on greylist retry: {label}")
                claimed.add(email)
                counts["inconclusive"] -= 1
                counts["valid"] += 1
            elif result.status == "catch_all":
                promoted += 1
                record.status = "catch_all"
                record.confidence = min(max(record.confidence, 40), 60)
                record.notes.append(f"catch-all domain, best guess: {label}")
                counts["inconclusive"] -= 1
                counts["catch_all"] += 1
            else:
                rejected += 1
                reply = smtp.reply_text(result)
                record.notes.append(
                    f"rejected on retry: {email} ({reply or 'no reply text'}); "
                    "ladder not completed")
                record.email = ""
                record.status = "name_only"
                counts["inconclusive"] -= 1
                counts["exhausted"] += 1
        log(f"    retry {attempt}: {promoted} accepted, {still_deferred} still "
            f"deferred, {rejected} rejected")


def resolve_permutations(ctx, records, log, verify_fn=None, sleep_fn=time.sleep,
                         on_round=None) -> dict:
    """Find addresses for people who have none, via the permutation ladder + SMTP.

    * With many people and no known convention, a small sample runs first; whatever
      convention verifies for them is tried first for everyone else, which saves a pile
      of doomed probes (and probes that look like address harvesting to the receiver).
    * Addresses that answered 4xx stop there: the ladder's answer is final unless a
      retry pass is explicitly enabled (`--greylist-retries N --greylist-wait SECONDS`).
    """
    import itertools

    round_seq = itertools.count(1)
    round_hook = (lambda _n: on_round(next(round_seq))) if on_round else None
    owns_verifier = verify_fn is None
    verify_fn = verify_fn or smtp.verify_many
    if owns_verifier:
        ready, reason = smtp.route_status()
        if not ready:
            log(f"  [!] smtp: {reason}")
            log("  [!] permutation: skipped - no SMTP route, addresses stay unverified")
            return {"valid": 0, "catch_all": 0, "inconclusive": 0, "exhausted": 0}
    deferred: dict = {}
    pending = [r for r in records if _needs_resolution(r)]
    known = ctx.memo("pattern_hits")

    if len(pending) > SAMPLE_SIZE * 2 and not known:
        sample = pending[:SAMPLE_SIZE]
        log(f"  [*] permutation: sampling {len(sample)} person(s) to learn the "
            "domain's convention")
        counts = _resolve_rounds(ctx, sample, log, verify_fn, deferred, hint=(),
                                 on_round=round_hook)
        learned = _learned_templates(sample)
        if learned:
            log(f"  [i] permutation: convention(s) seen: {', '.join(learned)}")
        rest = [r for r in records if not any(r is s for s in sample)]
        more = _resolve_rounds(ctx, rest, log, verify_fn, deferred, hint=learned,
                               on_round=round_hook)
        counts = {key: counts[key] + more[key] for key in counts}
    else:
        counts = _resolve_rounds(ctx, records, log, verify_fn, deferred, hint=(),
                                 on_round=round_hook)

    if deferred and max(0, int(ctx.opts.greylist_retries)) == 0:
        log(f"  [i] permutation: {len(deferred)} address(es) were throttled (4xx); kept as "
            "greylisted guesses, no retry pass (--greylist-retries 0)")
    if deferred:
        claimed = {r.email for r in records if r.email and r.status == "valid"}
        _retry_greylisted(ctx, deferred, claimed, counts, log, verify_fn, sleep_fn,
                          _control_address(ctx, records))
    if counts["exhausted"]:
        log(f"  [i] permutation: {counts['exhausted']} person(s) unverified after the ladder")
    return counts


def _resolve_rounds(ctx, records, log, verify_fn, deferred: dict, hint=(),
                   on_round=None) -> dict:
    """Walk the ladder one round at a time, stopping each person at their first hit.

    Round-based (candidate #1 for everyone, then #2 for whoever is left) so a domain whose
    convention is `first.last` costs one probe per person instead of twelve.
    """
    from . import permute

    pending = [r for r in records if _needs_resolution(r)]
    counts = {"valid": 0, "catch_all": 0, "inconclusive": 0, "exhausted": 0}
    if not pending:
        log("  [i] permutation: nothing to resolve (no missing or rejected addresses)")
        return counts

    preferred = list(hint) + [h.pattern for h in (ctx.memo("pattern_hits") or [])]
    max_rounds = max(1, int(ctx.opts.permute_max))
    claimed = {r.email for r in records if r.email and r.status == "valid"}
    # A known-good address on this domain lets the verifier notice a server that answers
    # "no such user" to everything (throttling) instead of believing it.
    control = _control_address(ctx, records)
    log(f"  [*] permutation: {len(pending)} person(s), up to {max_rounds} candidate(s) each")
    if not control:
        log("  [i] permutation: no control address (--control EMAIL) - a server that "
            "rejects everything cannot be told apart from one that rejects only strangers")

    plans = {
        id(record): permute.candidates(record.first_name, record.last_name,
                                       ctx.domain, preferred)
        for record in pending
    }
    # Names with nothing usable ("Cher", an all-symbol handle) produce no candidates.
    pending = [record for record in pending if plans[id(record)]]
    if not pending:
        log("  [i] permutation: no usable names to build candidates from")
        return counts

    exhaustive = bool(getattr(ctx.opts, "permute_all", False))
    if exhaustive:
        # Every spelling x every convention: no early drop, and the ladder is as long as
        # the longest per-person plan (dirty names have more than one spelling).
        max_rounds = max(max_rounds, max(len(plan) for plan in plans.values()))
        log(f"  [i] permutation: exhaustive - up to {max_rounds} candidate(s) per person, "
            f"~{max_rounds * len(pending)} probe(s) if nothing hits")

    # One budget for the whole ladder, scaled to the number of people so a
    # single-command run can actually finish instead of stopping mid-way.
    budget = max(int(ctx.opts.max_verify or 0),
                 min(2000, 8 * len(pending)) if not exhaustive
                 else min(20000, max_rounds * len(pending)))
    probes = 0
    # A person whose first N conventions are all rejected is not going to be saved by
    # convention N+1 - the domain's scheme is simply not in the ladder. In exhaustive
    # mode nothing is skipped.
    per_person_limit = max_rounds if exhaustive else min(max_rounds, 8)
    misses: dict = {}
    replies: dict = {}      # id(record) -> distinct server replies to their rejected guesses
    gave_up: "list[Record]" = []
    for round_index in range(max_rounds):
        if budget and probes >= budget:
            log(f"  [i] permutation: probe budget of {budget} reached, stopping")
            break
        batch = {}
        for record in pending:
            plan = plans[id(record)]
            if round_index >= len(plan):
                continue
            label, email = plan[round_index]
            if email in claimed:
                continue
            batch[email] = (record, label)
        if not batch:
            # Every candidate in this round collided with an address already verified
            # for someone else - move on to the next convention instead of giving up.
            if any(round_index < len(plans[id(record)]) for record in pending):
                continue
            break

        probes += len(batch)
        try:
            results = verify_fn(
                list(batch),
                ctx.http,
                from_addr=ctx.opts.from_addr,
                timeout=ctx.opts.verify_timeout,
                threads=max(1, min(4, ctx.opts.threads)),
                delay=ctx.opts.verify_delay,
                log=None,
                control=control,
            )
        except Exception as exc:  # noqa: BLE001 - SMTP must never kill the scan
            log(f"  [!] permutation: verification error ({type(exc).__name__}: {exc})")
            break

        resolved = 0
        for email, (record, label) in batch.items():
            result = results.get(email)
            if result is None:
                continue
            if result.status == "valid":
                record.email = email
                record.status = "valid"
                record.confidence = max(record.confidence, 95)
                record.notes.append(f"verified by permutation: {label}")
                claimed.add(email)
                control = control or email
                counts["valid"] += 1
                resolved += 1
            elif result.status == "catch_all":
                # The domain accepts anything, so the ladder cannot discriminate.
                # Keep the best-supported guess but label it honestly.
                record.email = email
                record.status = "catch_all"
                record.confidence = min(max(record.confidence, 40), 60)
                record.notes.append(f"catch-all domain, best guess: {label}")
                counts["catch_all"] += 1
                resolved += 1
            elif result.status in ("unknown", "greylisted"):
                # Inconclusive: the receiver refused *our probe*, not the mailbox.
                # Keep the ladder's best guess and say so, rather than dropping the
                # person because their previously discovered address was rejected.
                if record.email and record.email != email:
                    record.notes.append(f"previous address rejected: {record.email}")
                record.email = email
                record.status = "greylisted" if result.status == "greylisted" else "unverified"
                record.confidence = min(record.confidence, 55)
                reply = smtp.reply_text(result)
                record.notes.append(
                    f"smtp inconclusive; kept best guess: {label}"
                    + (f" [{reply}]" if reply else ""))
                if result.status == "greylisted":
                    deferred[email] = (record, label)
                counts["inconclusive"] += 1
                resolved += 1
            else:
                misses[id(record)] = misses.get(id(record), 0) + 1
                reply = smtp.reply_text(result)
                if reply:
                    seen = replies.setdefault(id(record), [])
                    if reply not in seen:
                        seen.append(reply)
                if misses[id(record)] >= per_person_limit:
                    record.notes.append(
                        f"ladder stopped after {per_person_limit} rejected candidates")
                    _drop(pending, record)
                    gave_up.append(record)
                continue
            _drop(pending, record)

        log(f"    round {round_index + 1}: {resolved} resolved, {len(pending)} still open")
        if on_round:
            on_round(round_index + 1)
        if not pending:
            break

    counts["exhausted"] += len(pending) + len(gave_up)
    for record in list(pending) + gave_up:
        if "no permutation address was accepted" not in " ".join(record.notes):
            record.notes.append("no permutation address was accepted")
            seen = replies.get(id(record))
            if seen:
                record.notes.append("server replies: " + " | ".join(seen[:2]))
        if record.status == "invalid":
            # We know their discovered address bounces; do not ship it, but do not
            # lose the person either - keep them as a name-only row.
            if record.email:
                record.notes.append(f"dropped rejected address: {record.email}")
            record.email = ""
            record.status = "name_only"
    return counts


# ---------------------------------------------------------------- verification
def verify_records(ctx, records, log) -> dict:
    targets = [r.email for r in records if r.email][: max(0, ctx.opts.max_verify)]
    if not targets:
        return {}
    log(f"  [*] SMTP verification for {len(targets)} address(es)...")
    results = smtp.verify_many(
        targets,
        ctx.http,
        from_addr=ctx.opts.from_addr,
        timeout=ctx.opts.verify_timeout,
        threads=max(1, min(4, ctx.opts.threads)),
        delay=ctx.opts.verify_delay,
        log=log,
        control=ctx.opts.control_address,
    )
    counts: dict = {}
    for record in records:
        result = results.get(record.email_key()) if record.email else None
        if result is None:
            continue
        counts[result.status] = counts.get(result.status, 0) + 1
        record.notes.append(result.note)
        if result.status == "valid":
            record.status = "valid"
            record.confidence = max(record.confidence, 95)
        elif result.status == "invalid":
            record.status = "invalid"
            record.confidence = min(record.confidence, 5)
        elif result.status == "catch_all":
            record.status = "catch_all"
            record.confidence = min(record.confidence, 45)
        elif result.status == "greylisted":
            record.status = "unknown"
            record.confidence = min(record.confidence, 40)
        else:
            if record.status != "valid":
                record.status = "unknown"
            record.confidence = min(record.confidence, 55)
    if counts:
        log("  [*] smtp: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        log("  [*] smtp: no answers (port 25 blocked, or all probes failed)")
    return counts


# ---------------------------------------------------------------- main entry
def scan(domain: str, opts: Options, log=None, name_seeds=None,
         progress=None) -> ScanResult:
    started = time.time()
    log = log or (lambda *_: None)

    http = HttpClient(
        timeout=opts.timeout,
        delay=opts.delay,
        user_agent=opts.user_agent or DEFAULT_UA,
        log=log,
    )
    ctx = Context(domain=domain, http=http, opts=opts, log=log)

    posture = mail_posture(domain, http)
    if posture["mx"]:
        log(f"  [i] mail: {len(posture['mx'])} MX host(s) via {', '.join(posture['providers']) or 'unknown provider'}")
    else:
        log("  [!] mail: no MX record - the domain may not receive mail at all")

    timings: dict = {}
    _t = time.time()
    log("  [*] discovering...")
    source_results = run_sources(ctx, log)
    timings["discovery"] = round(time.time() - _t, 1)

    raw: "list[Record]" = []
    for result in source_results:
        raw.extend(result.records)

    _t = time.time()
    linkedin_records = _linkedin_records(ctx, opts, log)
    timings["linkedin_profiles"] = round(time.time() - _t, 1)
    raw.extend(linkedin_records)

    by_email, name_only = merge_records(raw)

    def current_records() -> "list[Record]":
        seen, out = set(), []
        for record in list(by_email.values()) + list(name_only.values()):
            if id(record) in seen:
                continue
            seen.add(id(record))
            out.append(record)
        return out

    from .progress import RowEmitter

    emitter = RowEmitter(progress)

    def snap(stage: str) -> None:
        """Emit only the rows that changed since the previous stage."""
        emitter.snapshot([record.to_row() for record in current_records()], stage)

    dropped_generic = 0
    for key in list(by_email):
        if is_generic(key):
            by_email.pop(key)
            dropped_generic += 1

    snap("discovered")

    hits = infer_pattern(ctx, by_email)
    ctx.remember("pattern_hits", hits)
    if hits:
        log("  [i] pattern: " + ", ".join(
            f"{h.pattern} ({h.matched}/{h.total})" for h in hits
        ))
    else:
        log("  [!] pattern: not enough known emails to infer a convention")

    if name_seeds:
        known_keys = {record.person_key() for record in by_email.values()}
        known_keys |= set(name_only)
        for first, last, title in name_seeds:
            key = Record(domain=domain, first_name=first, last_name=last, title=title).person_key()
            if key in ("|", "") or key in known_keys:
                continue
            name_only[key] = Record(
                domain=domain, first_name=first, last_name=last, title=title or "",
                sources=["names-file"], confidence=30,
            ).finalize()

    generated = generate_emails(ctx, name_only, by_email, hits)
    if generated:
        log(f"  [i] generated {len(generated)} address(es) using {hits[0].pattern}")
        snap("generated")

    # People identified by name only still belong in the output - they are the people
    # whose address could not be derived (no pattern, or no email published).
    for leftover in name_only.values():
        if not leftover.email:
            leftover.status = "name_only"
            if not leftover.confidence:
                leftover.confidence = 35

    # current_records() already contains the name-only rows; do not append them again.
    records = current_records()
    _t = time.time()
    linkedin_counts = enrich_linkedin(ctx, records, log)
    timings["linkedin_lookups"] = round(time.time() - _t, 1)
    snap("linkedin")

    smtp_counts = {}
    _t = time.time()
    if opts.verify and records:
        smtp_counts = verify_records(ctx, records, log)
    timings["verify"] = round(time.time() - _t, 1)
    snap("verified")
    permute_counts = {}
    _t = time.time()
    if opts.permute and records:
        permute_counts = resolve_permutations(
            ctx, records, log, on_round=lambda number: snap(f"permuting:{number}")
        )
    timings["permute"] = round(time.time() - _t, 1)
    log("  [i] timings: " + "  ".join(f"{k}={v}s" for k, v in timings.items()))

    final = []
    for record in records:
        record.finalize()
        if record.status == "invalid" and not opts.keep_invalid:
            continue
        if opts.only_verified and record.status != "valid":
            continue
        if record.confidence < opts.min_confidence:
            continue
        final.append(record)

    final = dedupe_records(final)

    final.sort(key=lambda r: (
        -STATUS_RANK.get(r.status, 0),
        -r.confidence,
        0 if r.email else 1,
        r.full_name.lower(),
        r.email,
    ))

    elapsed = round(time.time() - started, 1)
    meta = {
        "domain": domain,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": elapsed,
        "total_records": len(final),
        "with_email": sum(1 for r in final if r.email),
        "with_name": sum(1 for r in final if r.full_name),
        "verified": sum(1 for r in final if r.status == "valid"),
        "pattern": hits[0].pattern if hits else "",
        "patterns_considered": [h.pattern for h in hits],
        "mail": {
            "mx": [h for _, h in posture["mx"]],
            "providers": posture["providers"],
            "spf": posture["spf"],
        },
        "smtp_results": smtp_counts,
        "timings": timings,
        "permutation_results": permute_counts,
        "linkedin_results": linkedin_counts,
        "github_nameless_logins": ctx.memo("github_nameless_logins") or [],
        "dropped_generic_inboxes": dropped_generic,
        "pages_scanned": ctx.memo("web_pages") or 0,
        "sources": [
            {
                "name": s.source,
                "ok": s.ok,
                "count": s.count,
                "error": s.error,
                "skipped": s.skipped,
            }
            for s in source_results
        ],
    }
    return ScanResult(domain=domain, records=final, meta=meta,
                      sources=source_results, pattern_hits=hits)
