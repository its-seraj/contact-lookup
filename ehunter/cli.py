"""Command line interface.

    ehunter scan example.com --verify
    ehunter verify alice@example.com bob@example.com --json-out v.json
    ehunter patterns known.csv
    ehunter sources
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

from . import __version__, patterns, pipeline, report, smtp
from . import sources as source_registry
from .config import env, load_dotenv
from .context import Context, Options
from .names import looks_like_name, split_name
from .net import DEFAULT_UA, HttpClient
from .util import EMAIL_RE, normalize_domain, now_stamp, parse_email_input

_DOMAIN_ONLY = re.compile(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$", re.I)

BANNER = "ehunter {ver}  |  domain -> employees (names, titles, emails)"


# ---------------------------------------------------------------- helpers
def _log(quiet: bool):
    if quiet:
        return lambda *_a, **_k: None

    def log(message: str = "", **_kw) -> None:
        print(message, file=sys.stderr, flush=True)

    return log


def _pairs_from_csv(text: str) -> "list[tuple[str, str, str]]":
    """Header-aware CSV parsing: works with our own export and with Hunter/Apollo exports."""
    reader = csv.DictReader(io.StringIO(text))
    fields = {str(f).strip().lower(): f for f in (reader.fieldnames or [])}
    email_key = next((fields[k] for k in
                      ("email", "email_address", "work_email", "e-mail", "mail")
                      if k in fields), None)
    if not email_key:
        return []
    first_key = next((fields[k] for k in
                      ("first_name", "firstname", "first", "given_name") if k in fields), None)
    last_key = next((fields[k] for k in
                     ("last_name", "lastname", "last", "surname", "family_name")
                     if k in fields), None)
    name_key = next((fields[k] for k in
                     ("full_name", "name", "full name", "person", "contact")
                     if k in fields), None)
    out = []
    for row in reader:
        email = (row.get(email_key) or "").strip().lower()
        if not EMAIL_RE.fullmatch(email):
            continue
        first = (row.get(first_key) or "").strip() if first_key else ""
        last = (row.get(last_key) or "").strip() if last_key else ""
        if not (first and last) and name_key:
            first, last = split_name((row.get(name_key) or "").strip())
        if not (first or last):
            continue
        out.append((first, last, email))
    return out


def _parse_pairs(text: str) -> "list[tuple[str, str, str]]":
    """Parse name+email pairs from CSV rows, 'Name, email', 'First,Last,email' or 'Name <email>'."""
    if "," in (text or "") and "\n" in (text or "")[:2000] or "," in (text or ""):
        from_csv = _pairs_from_csv(text)
        if from_csv:
            return from_csv
    pairs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = EMAIL_RE.search(line)
        if not match:
            continue
        email = match.group(0).strip(".,;").lower()
        columns = [c.strip() for c in re.split(r"[,;\t]", line) if c.strip()]
        # keep only columns that are neither the address nor a bare domain
        columns = [
            c for c in columns
            if not EMAIL_RE.search(c) and not _DOMAIN_ONLY.match(c)
        ]
        name = ""
        for column in columns:                      # best: something that reads like a name
            if " " in column and looks_like_name(column):
                name = column
                break
        if not name:
            for column in columns:                  # next: anything multi-word
                if " " in column and not any(ch.isdigit() for ch in column):
                    name = column
                    break
        if not name and columns:
            name = " ".join(columns)
        if not name:
            name = re.sub(r"\s+", " ", EMAIL_RE.sub("", line)).strip(' <>,-|"\'')
        first, last = split_name(name)
        pairs.append((first, last, email))
    return pairs


def _read_names_file(path: str) -> "list[tuple[str, str, str]]":
    """Read first/last/title seeds from a CSV or plain text name list."""
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"names file not found: {path}")
    text = file_path.read_text(encoding="utf-8-sig", errors="replace")
    seeds: "list[tuple[str, str, str]]" = []

    if file_path.suffix.lower() == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        fields = {str(f).strip().lower(): f for f in (reader.fieldnames or [])}
        fkey = fields.get("first_name") or fields.get("first") or fields.get("firstname")
        lkey = fields.get("last_name") or fields.get("last") or fields.get("lastname")
        nkey = (fields.get("name") or fields.get("full_name")
                or fields.get("full name") or fields.get("fullname"))
        tkey = (fields.get("title") or fields.get("role") or fields.get("position")
                or fields.get("job_title"))
        for row in reader:
            first = (row.get(fkey) or "").strip() if fkey else ""
            last = (row.get(lkey) or "").strip() if lkey else ""
            if not fkey and not lkey and nkey:
                first, last = split_name((row.get(nkey) or "").strip())
            title = (row.get(tkey) or "").strip() if tkey else ""
            if first or last:
                seeds.append((first, last, title))
        if seeds:
            return seeds

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if EMAIL_RE.search(line):
            for first, last, _email in _parse_pairs(line):
                seeds.append((first, last, ""))
            continue
        columns = [c.strip() for c in re.split(r"[,;\t]", line) if c.strip()]
        if len(columns) >= 2 and " " not in columns[0]:
            seeds.append((columns[0], columns[1], columns[2] if len(columns) > 2 else ""))
        else:
            first, last = split_name(columns[0])
            if first or last:
                seeds.append((first, last, ""))
    return seeds


def _table(rows, headers) -> str:
    widths = []
    for index, header in enumerate(headers):
        widths.append(max(len(header), max((len(str(r[index])) for r in rows), default=0)))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ehunter",
        description="Map a company domain to employee names, titles and email addresses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ehunter scan acme.com\n"
            "  ehunter scan acme.com --verify --min-confidence 40\n"
            "  ehunter scan acme.com --sources web,pgp,github --github-org acme-inc\n"
            "  ehunter scan acme.com --names-file team.csv --patterns 2\n"
            "  ehunter verify alice@acme.com bob@acme.com\n"
            "  ehunter patterns known_emails.csv\n"
            "  ehunter sources\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"ehunter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="run discovery for a domain")
    scan.add_argument("domain", help="company domain, e.g. acme.com")
    scan.add_argument("--sources", default="",
                      help=f"comma list (default: {source_registry.DEFAULT})")
    scan.add_argument("--max-per-source", type=int, default=1000, dest="max_per_source")
    scan.add_argument("--patterns", type=int, default=2,
                      help="email patterns to consider for name-only people (default 2)")
    scan.add_argument("--names-file", default="", dest="names_file",
                      help="CSV/txt of known names to expand into addresses")
    scan.add_argument("--verify", action="store_true",
                      help="probe mailboxes over SMTP (needs outbound port 25)")
    scan.add_argument("--permute", action="store_true",
                      help="for people without a working address, walk the email "
                           "permutation ladder until one is accepted (implies --verify)")
    scan.add_argument("--permute-all", action="store_true", dest="permute_all",
                      help="try every candidate for every spelling instead of stopping "
                           "after 8 rejections (slow, and more likely to be throttled)")
    scan.add_argument("--permute-max", type=int, nargs="?", const=19, default=19,
                      dest="permute_max", metavar="N",
                      help="candidate addresses to try per person (default 19; the flag "
                           "alone means 19)")
    scan.add_argument("--from-addr", default="", dest="from_addr",
                      help="MAIL FROM for verification (or set VERIFY_FROM)")
    scan.add_argument("--verify-delay", type=float, default=0.2, dest="verify_delay")
    scan.add_argument("--verify-timeout", type=float, default=10.0, dest="verify_timeout")
    scan.add_argument("--max-verify", type=int, default=500, dest="max_verify")
    scan.add_argument("--control", default="", dest="control_address", metavar="EMAIL",
                      help="an address you KNOW exists on the domain (or set VERIFY_CONTROL). "
                           "It is probed during verification; if the server refuses it, the "
                           "'no such user' answers are treated as throttling, not truth")
    scan.add_argument("--greylist-wait", type=float, default=0.0, dest="greylist_wait",
                      metavar="SECONDS",
                      help="wait before re-probing addresses that answered 4xx (default 60)")
    scan.add_argument("--throttled-as-valid", action="store_true", dest="throttled_as_valid",
                      help="when the server answers 4xx for a candidate, record it as "
                           "valid at confidence 70 instead of greylisted")
    scan.add_argument("--greylist-retries", type=int, default=0, dest="greylist_retries",
                      metavar="N",
                      help="re-probe greylisted addresses N times after a wait "
                           "(default 0 = off: the ladder's answer is final)")
    scan.add_argument("--threads", type=int, default=8)
    scan.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout seconds")
    scan.add_argument("--delay", type=float, default=0.4, help="per-host request delay seconds")
    scan.add_argument("--github-org", default="", dest="github_org")
    scan.add_argument("--max-pages", type=int, default=12, dest="max_pages",
                      help="website pages to crawl (default 12)")
    scan.add_argument("--search-pages", type=int, default=3, dest="search_pages",
                      help="result pages per dork (default 3)")
    scan.add_argument("--linkedin-file", default="", dest="linkedin_file",
                      help="reuse a saved sweep (linkedin_latest.csv/json) as contacts "
                           "for email resolution - costs no search credits")
    scan.add_argument("--linkedin", "--linkedin-sweep", action="store_true",
                      dest="linkedin_sweep",
                      help="find LinkedIn profiles for the domain and add them to the same "
                           "list, so email resolution covers them too")
    scan.add_argument("--sweep-depth", type=int, nargs="?", const=3, default=3,
                      dest="sweep_depth", metavar="N",
                      help="result pages per sweep query (default 3)")
    scan.add_argument("--sweep-variants", type=int, nargs="?", const=14, default=14,
                      dest="sweep_variants", metavar="N",
                      help="query variants to run (default 14)")
    scan.add_argument("--linkedin-lookups", type=int, nargs="?", const=10, default=10,
                      dest="linkedin_lookups", metavar="N",
                      help="per-person LinkedIn searches for people missing a URL "
                           "(default 10; the flag alone means 10; 0 disables)")
    scan.add_argument("--apollo-reveal", action="store_true", dest="apollo_reveal",
                      help="spend Apollo credits to unlock emails")
    scan.add_argument("--only-verified", action="store_true", dest="only_verified")
    scan.add_argument("--min-confidence", type=int, default=0, dest="min_confidence")
    scan.add_argument("--keep-invalid", action="store_true", dest="keep_invalid")
    scan.add_argument("--user-agent", default="", dest="user_agent")
    scan.add_argument("--out", default="out", help="output directory (default ./out)")
    scan.add_argument("--format", default="csv,json", help="csv,json (default both)")
    scan.add_argument("--json", action="store_true", dest="json_stdout",
                      help="print the full result as JSON on stdout")
    scan.add_argument("--quiet", action="store_true")
    scan.add_argument("--verbose", action="store_true")

    verify = sub.add_parser("verify", help="SMTP-verify a list of addresses")
    verify.add_argument("emails", nargs="+", help="addresses, a file path, or - for stdin")
    verify.add_argument("--from-addr", default="", dest="from_addr")
    verify.add_argument("--threads", type=int, default=4)
    verify.add_argument("--timeout", type=float, default=15.0)
    verify.add_argument("--verify-timeout", type=float, default=10.0, dest="verify_timeout")
    verify.add_argument("--verify-delay", type=float, default=0.2, dest="verify_delay")
    verify.add_argument("--json-out", default="", dest="json_out")
    verify.add_argument("--quiet", action="store_true")

    pat = sub.add_parser("patterns", help="infer the email convention from known addresses")
    pat.add_argument("input", help="CSV/txt of name+email pairs, or '-' for stdin")
    pat.add_argument("--top", type=int, default=5)

    perm = sub.add_parser("permute",
                          help="preview the candidate addresses for a name (no network)")
    perm.add_argument("names", nargs="+", help='"Jane Doe", several names, or a names file')
    perm.add_argument("--domain", required=True)
    perm.add_argument("--max", type=int, default=12, dest="max_items")

    ln = sub.add_parser("linkedin",
                        help="sweep every public LinkedIn profile that names the domain")
    ln.add_argument("domain")
    ln.add_argument("--timeout", type=float, default=25.0)
    ln.add_argument("--delay", type=float, default=0.3)
    ln.add_argument("--depth", type=int, nargs="?", const=3, default=3,
                    help="result pages per query (default 3)")
    ln.add_argument("--max-variants", type=int, nargs="?", const=14, default=14,
                    dest="max_variants", help="query variants to run (default 14)")
    ln.add_argument("--current-only", action="store_true", dest="current_only",
                    help="keep only profiles whose headline shows current employment")
    ln.add_argument("--out", default="out", help="output directory (default ./out)")
    ln.add_argument("--json", action="store_true", dest="json_stdout")
    ln.add_argument("--quiet", action="store_true")

    db = sub.add_parser("dbcheck", help="check the MongoDB result cache")
    db.add_argument("--uri", default="", help="override the URL from the environment")

    sub.add_parser("sources", help="show which sources are configured")
    return parser


# ---------------------------------------------------------------- commands
def _cmd_scan(args) -> int:
    domain = normalize_domain(args.domain)
    log = _log(args.quiet)
    if not args.quiet:
        log(BANNER.format(ver=__version__))

    source_spec = args.sources or source_registry.DEFAULT
    source_registry.build_sources(source_spec)  # fail fast on a typo, before any network call

    opts = Options(
        sources=source_spec,
        max_per_source=max(1, args.max_per_source),
        threads=max(1, args.threads),
        timeout=args.timeout,
        delay=max(0.0, args.delay),
        max_patterns=max(1, args.patterns),
        github_org=args.github_org,
        max_pages=max(1, args.max_pages),
        search_pages=max(1, args.search_pages),
        linkedin_lookups=max(0, args.linkedin_lookups),
        linkedin_sweep=args.linkedin_sweep,
        linkedin_file=args.linkedin_file,
        sweep_depth=max(1, args.sweep_depth),
        sweep_variants=max(1, args.sweep_variants),
        apollo_reveal=args.apollo_reveal,
        verify=args.verify or args.permute,
        permute=args.permute,
        permute_max=max(1, args.permute_max),
        permute_all=args.permute_all,
        from_addr=args.from_addr or env("VERIFY_FROM") or "noreply@example.com",
        verify_delay=max(0.0, args.verify_delay),
        verify_timeout=args.verify_timeout,
        max_verify=max(0, args.max_verify),
        control_address=(args.control_address or env("VERIFY_CONTROL") or "").strip(),
        greylist_wait=max(0.0, args.greylist_wait),
        greylist_retries=max(0, args.greylist_retries),
        throttled_as_valid=args.throttled_as_valid,
        min_confidence=max(0, args.min_confidence),
        only_verified=args.only_verified,
        keep_invalid=args.keep_invalid,
        verbose=args.verbose,
        user_agent=args.user_agent or DEFAULT_UA,
    )
    if opts.verify and opts.from_addr.endswith("@example.com"):
        log("  [!] verifying with MAIL FROM noreply@example.com - most servers reject that. "
            "Set VERIFY_FROM or --from-addr to an address you control.")

    seeds = None
    if args.names_file:
        seeds = _read_names_file(args.names_file)
        log(f"  [i] {len(seeds)} name(s) loaded from {args.names_file}")

    result = pipeline.scan(domain, opts, log=log, name_seeds=seeds)

    formats = {f.strip().lower() for f in (args.format or "csv,json").split(",") if f.strip()}
    outdir = Path(args.out) / domain
    stamp = now_stamp()
    written: "list[Path]" = []
    if "csv" in formats:
        written.append(report.write_csv(outdir / f"{domain}_{stamp}.csv", result.records))
        written.append(report.write_csv(outdir / f"{domain}_latest.csv", result.records))
    if "json" in formats:
        written.append(report.write_json(outdir / f"{domain}_{stamp}.json", result))
        written.append(report.write_json(outdir / f"{domain}_latest.json", result))

    if args.json_stdout:
        print(json.dumps(
            {"domain": result.domain, "meta": result.meta,
             "employees": [r.to_row() for r in result.records]},
            indent=2, ensure_ascii=False,
        ))
    else:
        report.summary(result)
        if written:
            print("")
            print(f"  files       : {written[0]}")
            for extra in written[1:]:
                print(f"                {extra}")
        if not result.records:
            print("", file=sys.stderr)
            print("  no people found. Most likely causes: every keyed source is unconfigured, "
                  "the site blocks scraping (403), or the domain has no public footprint. "
                  "Run `ehunter sources` to see what is active.", file=sys.stderr)
    return 0


def _cmd_verify(args) -> int:
    log = _log(args.quiet)
    raw = " ".join(args.emails)
    addresses = parse_email_input(raw) if len(args.emails) == 1 else parse_email_input(",".join(args.emails))
    if not addresses:
        print("error: no valid email addresses in input", file=sys.stderr)
        return 2
    from_addr = args.from_addr or env("VERIFY_FROM") or "noreply@example.com"
    if from_addr.endswith("@example.com"):
        log("  [!] MAIL FROM is noreply@example.com - set VERIFY_FROM to a real address you control")

    http = HttpClient(timeout=args.timeout, delay=0.2, user_agent=DEFAULT_UA, log=log)
    results = smtp.verify_many(
        addresses, http, from_addr=from_addr, timeout=args.verify_timeout,
        threads=max(1, args.threads), delay=max(0.0, args.verify_delay), log=log,
    )
    rows = []
    for address in addresses:
        res = results.get(address.lower())
        if res is None:
            rows.append([address, "unknown", "-", "no result", "-"])
            continue
        rows.append([
            address,
            res.status,
            res.code or "-",
            (res.message or "")[:46] or "-",
            res.mx or "-",
        ])
    print(_table(rows, ["email", "status", "code", "server reply", "via"]))

    if args.json_out:
        payload = {
            address: {
                "status": (results.get(address.lower()).status if results.get(address.lower()) else "unknown"),
                "code": (results.get(address.lower()).code if results.get(address.lower()) else 0),
                "message": (results.get(address.lower()).message if results.get(address.lower()) else ""),
                "mx": (results.get(address.lower()).mx if results.get(address.lower()) else ""),
            }
            for address in addresses
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


def _cmd_patterns(args) -> int:
    if args.input == "-":
        text = sys.stdin.read()
    else:
        path = Path(args.input)
        text = path.read_text(encoding="utf-8-sig", errors="replace") if path.is_file() else args.input
    pairs = _parse_pairs(text)
    if not pairs:
        print("error: no 'name,email' pairs recognised in input", file=sys.stderr)
        return 2
    hits = patterns.infer(pairs)
    if not hits:
        print(f"{len(pairs)} pair(s) parsed, but none matched a known convention. "
              "Locals may include digits or a different separator.")
        return 0
    rows = []
    for hit in hits[: max(1, args.top)]:
        example = patterns.build(hit.pattern, pairs[0][0], pairs[0][1], "domain.com") or "-"
        rows.append([hit.pattern, f"{hit.matched}/{hit.total}", hit.confidence, example])
    print(_table(rows, ["pattern", "matched", "confidence", "example"]))
    return 0


def _cmd_permute(args) -> int:
    from . import permute as permute_mod

    domain = normalize_domain(args.domain)
    joined = " ".join(args.names)
    seeds: "list[tuple[str, str, str]]" = []
    if len(args.names) == 1 and Path(joined).is_file():
        seeds = _read_names_file(joined)
    else:
        for raw in args.names:
            first, last = split_name(raw)
            if first or last:
                seeds.append((first, last, ""))
    if not seeds:
        print("error: no names recognised", file=sys.stderr)
        return 2

    for first, last, _title in seeds:
        plan = permute_mod.candidates(first, last, domain)
        if not plan:
            print(f"\n{first or '-'}  -> skipped (no usable name)")
            continue
        if len(seeds) > 1:
            print(f"\n{first} {last}".rstrip())
        rows = [
            [step, label, email]
            for step, (label, email) in enumerate(plan, start=1)
            if step <= args.max_items
        ]
        print(_table(rows, ["#", "convention", "candidate"]))
    return 0


def _cmd_linkedin(args) -> int:
    from . import sweep as sweep_mod
    from .sources.search import Search

    domain = normalize_domain(args.domain)
    log = _log(args.quiet)
    if not (env("SERPER_API_KEY") or env("BRAVE_API_KEY")
            or (env("GOOGLE_CSE_KEY") and env("GOOGLE_CSE_CX")) or env("SEARXNG_URL")):
        print("error: the LinkedIn sweep needs a search provider "
              "(SERPER_API_KEY, BRAVE_API_KEY, GOOGLE_CSE_KEY+_CX or SEARXNG_URL)",
              file=sys.stderr)
        return 2

    http = HttpClient(timeout=args.timeout, delay=args.delay, user_agent=DEFAULT_UA, log=log)
    opts = Options(search_pages=args.depth)
    ctx = Context(domain=domain, http=http, opts=opts, log=log)
    ctx.remember("company_names",
                 sweep_mod.company_names(domain, sweep_mod.site_title(ctx)))

    profiles = Search().sweep_linkedin(
        ctx, depth=max(1, args.depth), max_variants=max(1, args.max_variants)
    )
    if args.current_only:
        profiles = [p for p in profiles if p.get("employment") == "current"]
    profiles = sweep_mod.sort_profiles(profiles)

    if args.json_stdout:
        print(json.dumps({"domain": domain, "profiles": profiles}, indent=2,
                         ensure_ascii=False))
        return 0

    counts: dict = {}
    for profile in profiles:
        key = profile.get("employment") or "unclear"
        counts[key] = counts.get(key, 0) + 1
    rows = [
        [p.get("employment", ""), (p.get("full_name") or "-")[:26],
         (p.get("title") or "-")[:34], (p.get("slug") or "")[:30]]
        for p in profiles
    ]
    print(_table(rows, ["employment", "name", "headline", "profile"]) if rows
          else "no profiles found")

    stamp = now_stamp()
    outdir = Path(args.out) / domain
    csv_path = sweep_mod.write_profiles(outdir / f"linkedin_{stamp}.csv", profiles)
    sweep_mod.write_profiles(outdir / "linkedin_latest.csv", profiles)
    json_path = sweep_mod.write_profiles_json(outdir / f"linkedin_{stamp}.json",
                                              domain, profiles)
    sweep_mod.write_profiles_json(outdir / "linkedin_latest.json", domain, profiles)
    print("")
    print("  profiles    : " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"  files       : {csv_path}")
    print(f"                {json_path}")
    return 0


def _cmd_dbcheck(args) -> int:
    from .store import MONGO_ENV_NAMES, DomainStore, find_uri

    uri, source = (args.uri, "argument") if args.uri else find_uri()
    store = DomainStore(uri=uri or None)
    info = store.health()

    print(f"  mongo url   : {'set via ' + source if store.uri else 'not found'}")
    if store.uri:
        safe = store.uri.split("@")[-1] if "@" in store.uri else store.uri
        print(f"  target      : {safe[:80]}")
    print(f"  pymongo     : ", end="")
    try:
        import pymongo
        print(pymongo.version)
    except ImportError:
        print("MISSING  ->  python -m pip install \"pymongo[srv]\"")
    print(f"  cache       : {'ready' if info['enabled'] else 'unavailable'}")
    if info.get("error"):
        print(f"  reason      : {info['error']}")
    if not info["enabled"]:
        if not store.uri:
            print(f"\n  Put the URL in .env as one of: {', '.join(MONGO_ENV_NAMES)}")
        return 1

    started = __import__("time").time()
    docs = store.list_domains(limit=5)
    elapsed = int((__import__("time").time() - started) * 1000)
    print(f"  db          : {info['db']}.{info['collection']}")
    print(f"  round trip  : {elapsed} ms")
    print(f"  cached      : {len(docs)} domain(s)")
    for doc in docs[:5]:
        counts = doc.get("counts") or {}
        print(f"    - {doc.get('domain', '?')}  {counts.get('people', '?')} people  "
              f"{doc.get('scanned_at', '')}")
    return 0


def _cmd_sources(_args) -> int:
    rows = []
    for item in source_registry.describe():
        state = "ready" if item["available"] else "needs " + (", ".join(item["requires"]) or "config")
        rows.append([item["name"], item["label"], state, item["reason"] or ""])
    print(_table(rows, ["name", "source", "state", "note"]))
    print("\nfree sources need no key: " + ", ".join(source_registry.FREE))
    return 0


# ---------------------------------------------------------------- entry
def main(argv=None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _cmd_scan(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "patterns":
            return _cmd_patterns(args)
        if args.command == "sources":
            return _cmd_sources(args)
        if args.command == "dbcheck":
            return _cmd_dbcheck(args)
        if args.command == "permute":
            return _cmd_permute(args)
        if args.command == "linkedin":
            return _cmd_linkedin(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
