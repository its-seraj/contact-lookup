# Deploying the ehunter API on Hugging Face Spaces

The Space is a **Docker Space** that serves only the JSON API (`server.py` -> `ehunter.api`).
There is no UI here; the front end is a separate repo that calls this API.

## Files that matter

| File | Role |
|---|---|
| `README.md` front matter | `sdk: docker`, `app_port: 7860` |
| `Dockerfile` | installs `requirements-api.txt`, runs `uvicorn server:app --port 7860` as uid 1000 |
| `server.py` | exposes `ehunter.api.app` |
| `ehunter/webapp.py` | `run_scan()` caps (pages, variants, probe budget) and cached port-25 check |

## Deploy

1. Space settings: SDK **Docker**, hardware **CPU basic**.
2. Push the repo (`git push` to the Space remote). The build installs fastapi/uvicorn/pymongo.
3. *Settings -> Variables and secrets*:
   * `SERPER_API_KEY` - LinkedIn sweep and dorks
   * `GITHUB_TOKEN` - otherwise GitHub org members are invisible
   * `MONGODB_URI` (+ optional `MONGODB_DB`) - per-domain cache; without it every scan reruns
   * `CORS_ORIGINS` - your UI's origin(s), comma separated (default `*`)
   * `API_KEY` - optional service-to-service key, sent as `X-API-Key`. Scan routes otherwise need a sign-in JWT or an `eh_` token (README -> Authentication)
   * `AUTH_SERVICE_URL` - optional; defaults to `https://its-seraj-auth-service.hf.space` (used by `/api/token`; needs `MONGODB_URI`)
4. Smoke test: `GET https://<user>-<space>.hf.space/api/health`, then
   `POST /api/scan` with `{"domains":["buffer.com"],"isLinkedin":true}`.

Base URL: `https://<user>-<space>.hf.space` (lowercase; `.` and `_` become `-`).
Interactive docs: `/docs`.

## Keeping the Spaces awake

Free Spaces sleep after ~48h without traffic and take 30-90 s to cold-start. Two things are built in:

* **Keep-alive loop** (`ehunter/keepalive.py`): inside a Space (HF sets `SPACE_ID`) a background
  thread pings the auth service's `/health` and this Space's own `/api/health` every 30 min
  (`KEEPALIVE_INTERVAL_MINUTES`). Off locally unless `KEEPALIVE=1`; `KEEPALIVE=0` turns it off;
  `KEEPALIVE_URLS` adds more targets. Look for `[keepalive] ... up|DOWN` in the Space log.
* **Wake on demand** (`ehunter/auth.py`): if verifying a JWT fails because the auth service looks
  asleep (5xx, timeout, refused), the API polls its `/health` for up to `AUTH_WAKE_SECONDS`
  (default 90, `0` = off) and retries once, instead of returning an error. A `401` is a real
  answer and never triggers this. If the wake times out the caller gets `502` and should retry.

**Limit:** a Space that is already asleep runs no code, so this Space cannot keep *itself* awake
from inside. For a guarantee, add an external pinger on `https://<user>-<space>.hf.space/api/health`
(UptimeRobot, cron-job.org, or a GitHub Actions `schedule:` cron) every 5-30 min, and do the same
for the auth service's `/health`. A **paused** Space (not just sleeping) is not woken by requests:
restart it in Settings.

## Private Space

Every request needs `Authorization: Bearer <hf_token>`. Do not put that token in browser
code: proxy through your own backend, or make the Space public and rely on the
bearer auth above plus `CORS_ORIGINS`.

## What will not work on HF

| Feature | Status | Reason |
|---|---|---|
| SMTP verification / `isVerify` | unavailable | port 25 blocked; `webapp.py` disables it with a log line |
| Keyless search | mostly blocked | shared egress IPs get captcha'd; use `SERPER_API_KEY` |
| Persisted files in `out/` | none | filesystem is ephemeral; use the MongoDB cache |
| Long scans | not advisable | ~2 vCPU on the free tier |

Free Spaces sleep after ~48h idle; the first request afterwards is slow (see *Keeping the Spaces awake*).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/api/health` 404 | the Space is still on the old Gradio SDK: check README front matter says `sdk: docker` and rebuild |
| 401 from HF | private Space: send the HF bearer token |
| 401 `missing Bearer token` / `invalid API token` | send `Authorization: Bearer <sign-in jwt or eh_ token>` |
| 503 `token storage unavailable` | `MONGODB_URI` missing: `eh_` tokens are looked up in MongoDB |
| Browser CORS error | set `CORS_ORIGINS` to the UI origin |
| `mongo cache: disabled` in logs | `MONGODB_URI` missing or unreachable (`/api/health` says why) |
