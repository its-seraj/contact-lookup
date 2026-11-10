---
title: Contact Lookup
emoji: 😻
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
pinned: false
license: mit
short_description: contact enrichment tool
---

Check out the configuration reference at https://huggingface.co/docs/hub/spaces-config-reference


# ehunter

Give it a domain (or a list of names) and get back a table of people: **name, title, email, how confident we are, and which source produced it.**

```
$ python -m ehunter scan fsf.org --permute --linkedin

  domain      : fsf.org
  people      : 237  (236 with email, 11 SMTP-verified)
  pattern     : {first}
  elapsed     : 272.7s

status      name                      title                       email             conf  linkedin
----------  ------------------------  --------------------------  ----------------  ----  --------------------
valid       -                         -                           ekokao@fsf.org    95    -
valid       Craig Topham              & Licensing Associate       craigt@fsf.org    95    -
valid       Dawn Bryanton Peterson    Business Operations Manag…  dawnbp@fsf.org    95    -
valid       Greg Farough              Manager                     gregf@fsf.org     95    -
valid       Heshan de Silva-Weeramu…  Program Manager             heshan@fsf.org    95    -
valid       Ian Kelling               Senior Systems Administra…  iank@fsf.org      95    -
valid       Jeanne Rasata             Coordinator                 jeanne@fsf.org    95    -
valid       Krzysztof Siewicz         and Compliance Manager      ksiewicz@fsf.org  95    -
valid       Michael McMahon           GNU/Linux Systems Adminis…  michael@fsf.org   95    -
valid       Sean O'Brien              Deputy Director             seano@fsf.org     95    -
valid       Zoë Kooyman               Executive Director          zoe@fsf.org       95    -
unverified  Benjamin Harrison         Owner at Front Page Marke…  benjamin@fsf.org  55    benjamin-harrison-0…
unverified  Daniel Ryan               Business Owner at Front P…  daniel@fsf.org    55    daniel-ryan-b277351…
unverified  Emmanuel Daniel           Project Manager at Front …  emmanuel@fsf.org  55    emmanuel-daniel-3ab…
unverified  Michael Gayi              CEO at Front Page Publish…  michaelg@fsf.org  55    michael-gayi-a7a913…
unverified  Allison Knaus             Editorial Intern at 5280 …  allison@fsf.org   35    allison-knaus-07776…
unverified  Dylan Fox                 Founder/CEO                 dylan@fsf.org     35    dylanbfox
unverified  Ethan Yeo                 Intern news writer at the…  ethan@fsf.org     35    ethan-yeo-aa5198286
unverified  India Das-Brown           Freelance Journalist        india@fsf.org     35    india-das-brown
unverified  Jim Lindsay               Sr. Principal Mechanical …  jim@fsf.org       35    jim-lindsay-83733817
unverified  Mark Page                 Founder JetZero             mark@fsf.org      35    mark-page-8412004
unverified  Stacey May                Student Intern at Front R…  stacey@fsf.org    35    stacey-may-830218306
unverified  Terry Carter              Editor & Front-Page Sport…  terry@fsf.org     35    terrycarter
unverified  Doug Jennings             Owner at Freedom Realty F…  doug@fsf.org      30    doug-jennings-63997…
unverified  Dr Peter Murray           CEO @ Hold The Front Page…  peter@fsf.org     30    dr-peter-murray-661…
  ... 212 more row(s) in the exported files

  sources     : github=5; pgp=0; web=25; search=29; skipped: hunter, apollo, snov, prospeo

  files       : out\fsf.org\fsf.org_20261108T031939Z.csv
                out\fsf.org\fsf.org_latest.csv
                out\fsf.org\fsf.org_20261108T031939Z.json
                out\fsf.org\fsf.org_latest.json
```

It is a pipeline of independent collectors (paid APIs, the company's own website, PGP
keyservers, GitHub, search-engine dorks), a merge/dedupe layer, an **email-pattern
inference engine**, and an optional **SMTP verifier**.

---

## Set expectations first

No tool can return *all* employees of a company. What you get is what is **published or
discoverable**. Realistic yields:

| Company type | Typical result | Why |
|---|---|---|
| Startup / SMB with a `/team` page | 5–40 names, often 0 emails | names are public, addresses usually are not |
| SMB with a Hunter/Snov key | dozens of addresses + a domain pattern | the APIs already solved the pattern |
| Enterprise on Google Workspace / M365 | more, but verification is capped by catch-all and policy blocks | big mail systems answer honestly per-mailbox |
| Anything with a `/careers` page and PDFs | a handful | emails leak into decks, papers, press releases |

Two different things are being measured, keep them apart:

* **discovery** – "does an address for this person exist anywhere I can see?"
* **verification** – "would this mailbox accept mail?" (only SMTP can answer, imperfectly)

Rows are labelled with both, so downstream code never has to guess.

---

## Install

Pure standard library, Python 3.9+. No dependencies.

```bash
cd employee-hunter
python -m pip install -e .     # gives you the `ehunter` command
# or, without installing:
python -m ehunter scan acme.com
```

Configure keys (all optional):

```bash
cp .env.example .env    # then edit
ehunter sources         # shows what is active and what is missing
```

---

## Commands

### `scan` – the main entry point

```bash
# free sources only (website + PGP keyservers), no keys needed
ehunter scan acme.com

# everything configured, plus mailbox verification
ehunter scan acme.com --verify --from-addr you@yourdomain.com

# tight scope and a deeper crawl
ehunter scan acme.com --sources web,github --github-org acme-inc --max-pages 25

# ONE command: discover + verify + fall back through the permutation ladder
ehunter scan acme.com --permute

# expand a name list you already have into addresses, using the domain's pattern
ehunter scan acme.com --names-file team.csv --patterns 2

# machine-readable on stdout
ehunter scan acme.com --json --quiet | jq '.employees | length'
```

### `linkedin` – every public profile that names the domain

```bash
ehunter linkedin acme.com --depth 3                 # 14 query variants, 3 pages each
ehunter linkedin acme.com --current-only            # employees only
ehunter linkedin acme.com --json --quiet            # machine-readable
```

### `verify` – SMTP check without discovery

```bash
ehunter verify alice@acme.com bob@acme.com
ehunter verify emails.txt --json-out verified.json --threads 4
```

### `patterns` – what convention does this domain use?

```bash
ehunter patterns known_emails.csv      # our CSV export, Hunter export, or "Name <email>" lines
```

```
pattern         matched  confidence  example
--------------  -------  ----------  -------------------
{first}.{last}  3/3      84          jane.doe@domain.com
```

### `sources` – what is configured

```bash
ehunter sources
```

---

## Sources

Everything is pluggable (`ehunter/sources/`). Order shown is report order.

| Source | Key(s) | Cost | What it gives you | Notes |
|---|---|---|---|---|
| `hunter` | `HUNTER_API_KEY` | paid | emails + names + titles + **the domain pattern** | `domain-search`, paginated to 1000 |
| `apollo` | `APOLLO_API_KEY` | freemium | people with titles/links; emails locked or revealed | `--apollo-reveal` spends credits (~1/person, capped at 100/run) |
| `snov` | `SNOV_CLIENT_ID`, `SNOV_CLIENT_SECRET` | paid | emails with names, positions, verification flags | OAuth2 + `domain-emails-with-info` v2 |
| `prospeo` | `PROSPEO_API_KEY` | paid | verified work emails per domain | response parser tolerates API revisions |
| `github` | `GITHUB_TOKEN` (optional) | free | org members + public profile emails | guesses the org from the domain; capped at 30 profiles without a token. Members with no display name are kept as handle-only rows (`github` column, no address guessed) - nothing is dropped |
| `pgp` | none | free | real name + work email from key UIDs | fails on huge domains (keyserver 500s on thousands of keys) |
| `web` | none | free | names, titles, `mailto:` addresses from `/team`, `/about`, `/leadership`… | honours `robots.txt`, 24 standard paths + link discovery |
| `search` | **none** (keyless), or `SERPER_API_KEY` / `BRAVE_API_KEY` / `GOOGLE_CSE_KEY`+`_CX` / `SEARXNG_URL` | free | LinkedIn profile URLs, addresses in snippets, PDF leaks | dorks: `site:linkedin.com/in "domain"`, `"@domain"`, `-site:domain`, `filetype:pdf`. Keyless engines (`html.duckduckgo.com`, `bing.com`) captcha most datacenter IPs; a self-hosted SearXNG via `SEARXNG_URL` is the reliable keyless path |

The `web` source is the only one that works with zero configuration, and it is also the
one with the most heuristics:

* name candidates come from JSON-LD `Person` nodes, `mailto:` anchor text, and
  "`Name`" followed by a job-title block (`split_name_title` also handles the glued
  case `<h3>James HawkinsCo-founder</h3>`);
* a `mailto:` with no usable anchor text is attached to the closest person card above it
  in document order;
* shared inboxes (`info@`, `sales@`, `licensing@`, `campaigns@`, 60+ more) are dropped:
  they are not employees;
* **customer testimonials are dropped**: a card reading `CMO, Acalvio` or
  `Head of Sales - Rootstock Software` credits another company, so no `@target`
  address can exist and permutation would only burn probes. Internal scope such as
  `Head of Sales - EMEA` or `Director of Engineering - Platform` is kept. The run log
  reports how many cards were skipped;
* pages are decoded with the charset they declare (`Content-Type` header, then
  `<meta charset>`), because `Sales – Rootstock` read as UTF-8 turns into `Sales �
  Rootstock` and defeats the testimonial check.

---

## The pattern engine (`ehunter/patterns.py`)

Given observed `(first, last, email)` triples it scores 16 conventions and picks the one
that explains the most of them:

```
{first}.{last}   john.smith      {f}{last}   jsmith      {first}{last}  johnsmith
{first}_{last}   john_smith      {last}.{first}  smith.john   {first}{l}  johns
{first}-{last}   john-smith      {last}{first}   smithjohn    {f}-{last}  j-smith
...
```

Guards that keep it from inventing garbage:

1. **Two confirmations minimum.** One matching pair is a coincidence, not a convention.
   (`{first}` matching once at `fsf.org` proves nothing; it needs a second.)
   Hunter's own domain-level conclusion is allowed to stand alone.
2. **Collision check.** If the generated address already belongs to a *different* person,
   the convention is not trusted for that pair — no row is emitted.
3. **Free-mail is ignored.** `gmail.com` pairs say nothing about `acme.com`.
4. **One row per person.** The runner-up pattern is stored in `notes` as `alt: ...`
   instead of producing a second guess row.
5. **`status = inferred`** and confidence ≈ half the pattern confidence, so a guessed
   address is never mistaken for a found one.

---

## Resolving addresses: the permutation ladder

`--permute` (implies `--verify`) walks a per-domain ladder for every person whose
discovered address was rejected or never existed, and stops at the first address the
mail server accepts. Preview the ladder without touching the network:

```bash
ehunter permute "Jane Doe" --domain acme.com
```

```
#   convention  candidate
1   first.last  jane.doe@acme.com
2   first       jane@acme.com
3   firstlast   janedoe@acme.com
4   f.last      j.doe@acme.com
5   f+last      jdoe@acme.com
6   firstl      janed@acme.com
7   first_last  jane_doe@acme.com
8   last.first  doe.jane@acme.com
9   fl          jd@acme.com
10  last        doe@acme.com
11  lastfirst   doejane@acme.com
12  last_first  doe_jane@acme.com
13  first.l     jane.d@acme.com
14  first-last  jane-doe@acme.com
15  f-last      j-doe@acme.com
16  last.f      doe.j@acme.com
17  lastf       doej@acme.com
18  f_last      j_doe@acme.com
19  first_l     jane_d@acme.com
```

Rules the resolver follows:

* **Round-based, not per-person.** Round 1 probes `first.last` for everybody, round 2 the
  survivors get `first`, and so on. A domain that uses `first.last` costs one probe per
  person instead of eighteen.
* **Preferred pattern first.** If known emails already revealed the convention
  (`{f}{last}` at 4/4), that address is step 0, ahead of the generic ladder.
* **Collision guard.** An address already verified for someone else is never reused for a
  different person, even when they share `jane.doe`.
* **Stop conditions.** `valid` → done, `catch_all` → stop and keep the best guess,
  `unknown`/`greylisted` → stop (the receiver refused us, not the mailbox), `invalid` → next.
* **Never lose a person.** If the ladder exhausts and their discovered address was
  rejected, the row becomes `name_only` instead of being dropped.
* **The ladder's answer is final.** A 4xx answer keeps the guess as `greylisted` and stops
  that person; there is no waiting retry pass unless you ask for one with
  `--greylist-retries N --greylist-wait SECONDS` (both default to off: 0 and 0).
* Tune with `--permute-max N` (default 19).

### Names that are not names

GitHub profiles, logins and scraped cards carry decoration: `jane99`, `john_dev`,
`jane.doe`, `s3raj`, or a login like `jane_doe` with no surname at all. Rendering
`jane99.doe@` is wasted probes, so the resolver derives the plausible spellings first
and runs the **whole ladder for the best spelling before moving to the next**:

| label | from `Jane99 Doe` | when |
|---|---|---|
| `clean` | jane / doe | letters only — digits and punctuation dropped |
| `surname-split` | jane / smith | given name ends with the surname (`Janesmith Smith`) |
| `truncated` | jane / doe | cut at the first digit or separator (`jane_doe99` → `jane`) |
| `clean-surname` | jane / doe | decoration only in the surname (`Doe99`) |
| `raw` | jane99 / doe | exactly as stored (the only variant for a clean name) |
| `split` | jane / doe | no surname, but the login contains both (`jane_doe`) |
| `first-only` | cher | a single clean token: only `cher@domain` can be built |

```bash
$ ehunter permute "Jane99 Doe" --domain bamboobox.ai
1  first.last (clean)  jane.doe@bamboobox.ai
2  first (clean)       jane@bamboobox.ai
...
20 first.last          jane99.doe@bamboobox.ai     # raw spelling, only after clean
```

A dirty single token (`s3raj`) is skipped on purpose: the cleaned guess would be a
different word, not the person's name.

## SMTP verification (`ehunter/smtp.py`)

Needs **outbound port 25**; many office/ISP networks block it (the tool says so inline).

What it does per domain, once:

1. resolve MX via DoH (`cloudflare-dns.com` → `dns.google` → `nslookup`), honouring null MX;
2. probe a random non-existent address to detect **catch-all** (accepts anything);
3. reuse one session, `MAIL FROM` yours, `RCPT TO` each candidate, with delay and
   reconnect every 40 probes.

Result mapping:

| status | meaning |
|---|---|
| `valid` | 250/251/252 (or 552 mailbox-full) and the domain is not catch-all |
| `invalid` | hard 5xx that is about the *recipient* |
| `greylisted` | 4xx / 421 → "try later"; the guessed address is kept but unconfirmed |
| `catch_all` | domain accepts every address, so acceptance proves nothing |
| `unknown` | no route, timeout, or the server rejected **us** |

That last row matters. `mail.fsf.org` answers a probe for a *real* employee with:

```
550 Sender verification failed
```

That is a verdict about your `MAIL FROM` (policy/SPF/reputation), not about the mailbox.
The classifier detects policy/SPF/reputation/`5.7.x` wording and returns `unknown` rather
than `invalid`, because reporting a false negative is worse than reporting nothing.
Set `VERIFY_FROM` to an address on a domain that actually authorises it.

### Why `code == 250` is not enough

The naive verifier (open SMTP, `RCPT TO`, `return code == 250`) produces wrong answers in
three situations this tool handles explicitly:

| Situation | Naive result | What ehunter does |
|---|---|---|
| **Catch-all domain** — the server accepts every address | "valid" for a made-up address | probes a random address first; if it is accepted, the domain is `catch_all` and no acceptance means anything |
| **Receiver rejects the *sender*** — SPF/DKIM/reputation, e.g. `550 Sender verification failed` | "invalid" for a real employee | classifies it as `unknown` (policy) instead of `invalid` |
| **Code semantics** — `251`, `252` accept; `552` = mailbox full but real; `4xx` = retry later | false negatives / false positives | mapped to `valid` / `valid (full)` / `greylisted` |

Measured on this machine with `MAIL FROM <seraj@gmail.com>`: `posthog.com` is a catch-all
domain, so `demand.gen@posthog.com` and `james.hawkins@posthog.com` both return 250 —
one is a real convention, the other is nonsense. A naive checker reports both as valid.
`fsf.org`, by contrast, answered `valid` for 11 of 16 published addresses and
`550 Sender verification failed` when the envelope sender was `noreply@example.com`:
the same addresses, two different verdicts, decided entirely by the sender domain.
Send from a domain you control with SPF set up, or accept `unknown` as the answer.

Large mail systems rate-limit *per recipient domain*: after ~60 probes to a Google
Workspace domain every further `RCPT` returns
`450 4.2.1 The user you are trying to contact is receiving mail at a rate`, which is a
`greylisted` verdict, not a rejection. Keep `--permute-max` modest and `--verify-delay`
around 1s on Google-hosted domains, or re-check those addresses later with
`ehunter verify`.

**Port 25 blocked by your ISP/hotspot?** Test it: `python -c "import socket; socket.create_connection(('aspmx.l.google.com', 25), 5)"`. If that times out, route
the probes through a host that allows port 25 (check it there with
`nc -vz aspmx.l.google.com 25`; AWS, GCP, Azure and Oracle block it by default):

```
ssh -N -D 1080 user@your-vps        # leave running
SMTP_PROXY=socks5://127.0.0.1:1080  # in .env
```

Only SMTP connections use the tunnel; web scraping, DNS and APIs stay direct. The MX
hostname is resolved on the VPS. `GET /api/health` and the scan warning say whether the
tunnel can reach port 25 (`/api/health` shows the result of the last check, so it reads
`null` until the first verify scan).

Other honest limits: providers that accept-then-bounce, tarpits that always answer 250,
and per-connection rate limits all make a single probe unreliable. Treat `valid` as
"accepted", not "deliverable".

---

## Output

Written to `out/<domain>/<domain>_<utc-timestamp>.{csv,json}` plus `_latest` copies.

```csv
domain,full_name,first_name,last_name,title,email,email_status,confidence,pattern,linkedin,linkedin_status,github,website,sources,notes
bamboobox.ai,Ankur Saigal,Ankur,Saigal,Cofounder & CEO at BambooBox,ankur@bamboobox.ai,valid,95,,
  https://www.linkedin.com/in/ankur-saigal-b35313,current,,,search;linkedin,linkedin: current
bamboobox.ai,,,,,,name_only,30,,,,harsh-b2,,github,"github: @harsh-b2; no display name (login is not a name)"
```

```json
{
  "domain": "fsf.org",
  "meta": {
    "total_records": 16, "with_email": 16, "verified": 0,
    "pattern": "{first}",
    "mail": { "mx": ["mail.fsf.org"], "providers": [], "spf": true },
    "smtp_results": { "unknown": 16 },
    "sources": [{ "name": "web", "ok": true, "count": 29 }]
  },
  "employees": [ { "...": "one row per person" } ]
}
```

---

## Flags worth knowing

| Flag | Default | Why |
|---|---|---|
| `--sources` | all 8 | subset by name, e.g. `web,pgp,github`, or `free` for the keyless set |
| `--max-pages` | 12 | website crawl budget |
| `--patterns` | 2 | how many conventions to consider (2nd goes to `notes` only) |
| `--verify` | off | SMTP probing; off by default because it is intrusive and needs port 25 |
| `--from-addr` / `VERIFY_FROM` | `noreply@example.com` | MAIL FROM; set it or expect policy rejections |
| `--max-verify` | 500 | cap the number of probes |
| `--only-verified` | off | keep only `status=valid` |
| `--min-confidence` | 0 | e.g. `40` drops most inferred rows |
| `--keep-invalid` | off | keep rows SMTP rejected |
| `--github-org` | guess (org name + GitHub org search) | when the org name is not the domain label |
| `--linkedin-lookups` | 10 | per-person LinkedIn searches; `0` disables |
| `--linkedin` | off | sweep LinkedIn dorks and add every profile to the same list (`--linkedin-sweep` also works) |
| `--linkedin-file` | – | reuse a saved sweep (`linkedin_latest.csv/json`) as contacts; no credits |
| `MONGODB_URI` | – | enables the API's per-domain cache |
| `CORS_ORIGINS` | `*` | allowed origins for the HTTP API |
| `--sweep-depth` | 3 | result pages per sweep query |
| `--sweep-variants` | 14 | LinkedIn query variants to run |
| `--permute` | off | walk the candidate ladder for people without a verified address (implies `--verify`) |
| `--permute-max` | 19 | candidate addresses per person |
| `SMTP_PROXY` (env) | – | `socks5://host:port` - send SMTP probes through a SOCKS5 tunnel when your own network blocks port 25 (see below) |
| `--control` | – | an address you know exists on the domain (or `VERIFY_CONTROL`); if the server refuses it, its "no such user" answers are treated as throttling (`greylisted`) instead of trusted |
| `--greylist-wait` / `--greylist-retries` | 60s / 1 | re-probe `greylisted` addresses after a wait; `0` retries turns it off |
| `--names-file` | – | CSV (`first_name,last_name[,title]`) or plain `First Last` lines |
| `--delay` | 0.4s | per-host request spacing, shared by all sources |
| `--threads` | 8 | source concurrency and SMTP workers |

Exit codes: `0` success, `2` bad input/domain, `1` unexpected error, `130` interrupt.

---

## Adding a source

```python
# ehunter/sources/mysource.py
from .base import Source, rec

class MySource(Source):
    name = "mysource"
    label = "My Source"
    requires = ("MY_API_KEY",)     # omit for a keyless source; set free = True

    def run(self, ctx) -> list:
        data = ctx.http.get_json(
            "https://api.example.com/people",
            params={"domain": ctx.domain},
            headers={"Authorization": f"Bearer {ctx.env('MY_API_KEY')}"},
        )
        return [
            rec(ctx, self.name, full=row["name"], email=row.get("email"),
                title=row.get("title"), confidence=70)
            for row in data.get("results", [])
        ]
```

Register it in `ehunter/sources/__init__.py` (one line in `ALL`). Errors are contained:
a source that raises produces a `source: failed` note, the scan continues.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

112 tests over name parsing (particles, suffixes, accents, `Last, First`), pattern
inference, merging/generation guards, PGP UID parsing, the HTML extractor, LinkedIn URL
canonicalisation/matching, `humans.txt`, the keyless SERP parsers, and the permutation
resolver (name variants, stop conditions, catch-all, collisions, exhaustion),
charset decoding, testimonial detection, the Gravatar channel, and the LinkedIn sweep
(variant building, employment classification, record construction).

---

## HTTP API: multi-domain, streamed, MongoDB-cached

The same engine is exposed as a JSON API for a web front end.

```bash
pip install -r requirements-api.txt          # fastapi, uvicorn, pymongo
set MONGODB_URI=mongodb://localhost:27017    # optional: cache is skipped without it
python -m server                             # API on :8000, docs on /docs
```

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/scan` | scan 1–10 domains, streamed as newline-delimited JSON (or `?format=sse`) |
| `GET` | `/api/domains` | cached domains, newest first (no rows) |
| `GET` | `/api/domains/{domain}` | full cached document including rows |
| `DELETE` | `/api/domains/{domain}` | drop one cached domain |
| `GET` | `/api/health` | cache status, port-25 state, domain limit |

### Request

```json
{ "domains": ["acme.com", "beta.io"], "isVerify": false, "isLinkedin": true, "force": false }
```

`domains` also accepts a single string (`"acme.com, beta.io"`). `isVerify` maps to
`--permute` + SMTP verification, `isLinkedin` maps to `--linkedin`. `force` ignores the
cached document.

### Events on the stream

| type | when | payload |
|---|---|---|
| `start` | before a domain is scanned | `domain` |
| `log` | every pipeline log line, as it happens | `domain`, `message` |
| `cached` | domain served from MongoDB | `domain`, `flags`, `counts`, `scanned_at` |
| `partial` | **rows found so far**, emitted per stage | `domain`, `stage`, `rows` (only new/changed), `removed`, `total`, `rows_total` |
| `result` | domain finished (or cache hit) | `domain`, `rows`, `meta`, `counts`, `cached` |
| `error` | invalid input, or a domain that failed | `domain` or `input`, `message` |
| `done` | all domains handled | `domains`, `elapsed_ms` |

```bash
curl -N -X POST http://localhost:8000/api/scan \
  -H "Content-Type: application/json" \
  -d '{"domains":["buffer.com","posthog.com"],"isLinkedin":true}'
```

```
{"type":"start","domain":"buffer.com"}
{"type":"log","domain":"buffer.com","message":"  [*] discovering..."}
{"type":"result","domain":"buffer.com","rows":[...],"counts":{"people":17,...},"cached":false}
{"type":"start","domain":"posthog.com"}
{"type":"result","domain":"posthog.com","rows":[...]}
{"type":"done","domains":2,"elapsed_ms":72200}
```

`partial.stage` is one of `discovered`, `generated`, `linkedin`, `verified`,
`permuting:N` (one per ladder round), `resolved`. Rows are deltas: apply `rows` (they carry
the same columns as the final result), delete the keys in `removed`, and you have the
current table. A row's key is its person, not its address, so an email appearing in round 3
updates that person's row instead of adding a duplicate:

```python
from ehunter.progress import row_key

table = {}
for event in stream:
    if event["type"] != "partial":
        continue
    for row in event["rows"]:
        table[row_key(row)] = row
    for key in event["removed"]:
        table.pop(key, None)
    render(list(table.values()))        # <- the UI renders here, mid-scan
```

That is what keeps the page useful while verification runs: on a real domain the first 15
rows appeared at 19 s, LinkedIn URLs at 43 s, and addresses arrived round by round after
that, while the final result landed at 337 s.

Browser side, read it as a stream:

```js
const res = await fetch("/api/scan", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ domains, isVerify, isLinkedin }),
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
for (;;) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  let i;
  while ((i = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, i).trim();
    buffer = buffer.slice(i + 1);
    if (line) handleEvent(JSON.parse(line));   // start / log / partial / result / error / done
  }
}
```

`?format=sse` emits `data: {...}\n\n` frames instead (note `EventSource` cannot POST; use
`fetch` or an SSE library).

### Cache semantics

One MongoDB document per domain (`_id` = domain), collection `domains`:

```json
{ "_id": "buffer.com", "domain": "buffer.com", "rows": [...], "meta": {...},
  "counts": {"people": 17, "with_email": 9}, "flags": {"isVerify": false, "isLinkedin": true},
  "scanned_at": "2026-11-08T15:41:00+00:00", "scan_ms": 22517 }
```

Measured on a real domain: the first request took **51.9 s**, the second identical one
**0.04 s** (`{"type":"cached"}` then `{"type":"result"}`), a 1,400x difference.

A cached document is reused when the flags that produced it **cover** the request: a
`isLinkedin=false` document cannot answer an `isLinkedin=true` request, so that domain is
rescanned instead of returning a thinner list; `force: true` always rescans. Set
`DOMAINS_PER_REQUEST` (default 10) and `CORS_ORIGINS` (default `*`, comma separated) as
needed. Without `pymongo` or a reachable server the cache silently degrades to "no cache" —
`/api/health` reports why.

## Deploy on Hugging Face

The repo deploys as a **Docker Space** that serves only the HTTP API (no UI; the front end
lives in its own repo). `Dockerfile` runs `uvicorn server:app` on port 7860 and the README
front matter declares `sdk: docker` / `app_port: 7860`.

```bash
git clone https://huggingface.co/spaces/<user>/<space>
cp -r ./* .                      # Dockerfile, server.py, ehunter/, requirements-api.txt, README.md
git add -A && git commit -m "api space" && git push
```

The API is then at `https://<user>-<space>.hf.space/api/...` (`/api/health`, `/api/scan`,
`/docs`). Add `SERPER_API_KEY`, `GITHUB_TOKEN`, `MONGODB_URI`, `CORS_ORIGINS` (your UI's
origin) and optionally `API_KEY` (clients then send `X-API-Key`) in *Variables and secrets*.
A private Space also needs `Authorization: Bearer <hf token>` on every call, so call it
from your backend, not from browser code. **Port 25 is blocked on Hugging Face**, so SMTP
verification cannot run there - addresses come back as unverified guesses. Details:
[DEPLOY_HF.md](DEPLOY_HF.md).

## Use it on data you are allowed to touch

The collectors are public sources; the web source honours `robots.txt` and rate limits;
SMTP probing touches the target's mail infrastructure and is intrusive. Harvested
addresses are personal data under GDPR/CCPA — bulk unsolicited mail to them is illegal
in most jurisdictions, and LinkedIn/Hunter/Prospeo terms bind how you may store or resell
what their APIs return. Use it for recruiting, security research, or due diligence on
domains where you have that basis.
