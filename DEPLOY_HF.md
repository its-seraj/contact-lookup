# Deploying ehunter on Hugging Face Spaces

The repository is ready to be pushed as a **Gradio Space** as-is: `app.py`,
`requirements.txt` and the `README.md` front matter are already in place. This page lists
what changed versus the local CLI, what will not work on HF, and the exact steps.

---

## The one hard constraint: no port 25

Hugging Face blocks outbound SMTP, and so do most cloud hosts (AWS, GCP, Azure all block 25
by default). That means:

* **`--permute` / `--verify` cannot work on a Space.** The ladder needs `RCPT TO` against
  the target's MX.
* Discovery still works fully: website, GitHub, PGP, LinkedIn sweep, email pattern
  inference. Addresses are produced as **guesses** and marked `unverified`.
* The UI therefore ships with `Verify emails` unchecked and disabled-by-advice, and
  `ehunter/webapp.py` re-checks reachability per run, disabling verification with a clear
  log line instead of hanging.

If you need confirmed addresses, run the verification half where port 25 is open (your
laptop, or a VPS with port 25 unblocked) against the CSV the Space gives you:

```bash
# on HF: discovery only -> download bamboobox_ai_<ts>.csv
# locally, with the downloaded CSV:
python -m ehunter scan bamboobox.ai --linkedin-file out/bamboobox.ai/linkedin_latest.csv --permute
```

---

## Files added or changed

| File | Change | Why |
|---|---|---|
| `app.py` | **new** | Gradio UI: domain box, LinkedIn sweep toggle, limits, results table, CSV download, log pane |
| `ehunter/webapp.py` | **new** | `run_scan()` wraps `pipeline.scan()` with Space-safe caps and port-25 detection; no Gradio import, so it stays testable |
| `requirements.txt` | now `gradio>=5.0,<6` | the Space installs it; the CLI itself is still stdlib-only |
| `README.md` | YAML front matter added | HF needs `sdk: gradio`, `app_file: app.py` |
| `.gitignore` | `.deps/`, `tests/_tmp_sweep/` | local only |
| `ehunter/config.py` | unchanged | `load_dotenv()` already falls back to real env vars, so HF *secrets* work with no code change |

Nothing else in the pipeline changed: sources, sweep, permutation and reporting are the
same code the CLI uses.

---

## Deploy in five steps

1. **Create the Space** — huggingface.co/new-space, SDK **Gradio**, hardware **CPU basic
   (free)**, visibility **Private** (see privacy below).
2. **Push this repository** into it:
   ```bash
   git clone https://huggingface.co/spaces/<user>/ehunter
   cp -r contact-lookup/* .            # app.py, ehunter/, requirements.txt, README.md
   git add -A && git commit -m "ehunter space" && git push
   ```
   (or drag the files into the Space's *Files* tab).
3. **Add secrets** — Settings → *Variables and secrets*:
   * `SERPER_API_KEY` — required for the LinkedIn sweep and dorks (free tier: 2,500 credits)
   * `GITHUB_TOKEN` — otherwise GitHub org membership is invisible (the API returns 0
     members unauthenticated) and you get 60 requests/hour on a shared IP
   * `VERIFY_FROM` — harmless to set, unused while port 25 is blocked
4. **Restart** the Space and wait for the build (Gradio install takes ~1–2 min).
5. **Smoke test** with `buffer.com` — you should get ~15 rows, several LinkedIn URLs, and
   `email_status=unverified` everywhere.

---

## What will not work on HF, and why

| Feature | Status on a Space | Reason / workaround |
|---|---|---|
| SMTP verification, `--permute` | **unavailable** | port 25 blocked; run locally or on a VPS |
| Keyless search (DuckDuckGo/Bing fallback) | mostly blocked | HF egress IPs are shared and captcha'd; use `SERPER_API_KEY` |
| GitHub org members | empty without a token | private membership is invisible to anonymous calls |
| PGP keyservers | works | plain HTTPS |
| Website scraping | works | plain HTTPS; some sites 403 shared IPs |
| Persisted results | **none** — filesystem is ephemeral | download the CSV; a rebuild wipes `out/` |
| Long scans (20+ pages) | not advisable | free CPU is ~2 vCPU; keep the sliders low |

Also note: free Spaces **sleep after ~48h of inactivity** and cold-start on the next visit
(the first click after sleep takes a few seconds; `port25_available()` is cached for 5
minutes to avoid a repeated 3-second probe).

---

## Troubleshooting the Space log

| Log line | Meaning | Fix |
|---|---|---|
| `UserWarning: The parameters have been moved from the Blocks constructor to the launch() method in Gradio 6.0: theme` | Gradio 6 moved `theme` | handled in `app.py` (version-aware) |
| `Running on local URL: http://0.0.0.0:7860, with SSR` then `Stopping Node.js server...` | Gradio 6 starts an **SSR Node proxy** on 7860 and moves Python to 7861. `Stopping Node.js server...` is a SIGTERM handler, i.e. the container was asked to stop after that proxy failed a health check | `ssr_mode=False` in `launch()` (already set) so Python serves 7860 directly |
| `Invalid allowed path` | `allowed_paths` pointed at a directory that does not exist — `out/` is gitignored, so a fresh Space has none | `app.py` creates `out/` before launching |
| Space builds, then restarts in a loop | usually a launch exception; `app.py` now prints the traceback and retries with plain `launch()` | read the first traceback in the log |

`GRADIO_SERVER_PORT` (set by Spaces) is preferred over `PORT`, falling back to 7860.

## Tuning the shown limits

`ehunter/webapp.py` hard-caps what the UI can request, so a stranger cannot make the Space
hammer a third party:

| Setting | UI range | Default | Effect |
|---|---|---|---|
| Website pages | 2–25 | 8 | crawl budget |
| LinkedIn query variants | 2–20 | 10 | search credits = variants × 3 pages |
| Email candidates per person | 1–19 | 6 | probes per person (irrelevant without port 25) |
| `max_verify` | fixed 400 | — | total probe budget when verification is available |

A representative scan costs ~30–40 Serper credits and 30–120 seconds of CPU.

---

## Privacy and policy

* A **public** Space means anyone with the URL can read every result and could type in any
  domain. This tool collects personal data (names, work emails, profile URLs) — use a
  **private** Space, and keep the CSV out of public repos/datasets.
* Do not republish harvested contact lists; GDPR/CCPA obligations apply to the data even
  when the sources were public.
* Hugging Face's content policy applies to the Space itself; the LinkedIn data comes from
  search snippets, never from scraping LinkedIn directly.

---

## If you need confirmation at scale

The honest architecture is two-stage: discovery on the Space (or anywhere), verification on
a machine with port 25 open. A small VM with port 25 unblocked (many providers require a
support ticket) running `ehunter scan <domain> --names-file <csv> --permute --verify-delay 2`
gives you `valid` / `invalid` per address, which no cloud Space can do.
