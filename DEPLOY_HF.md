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
   * `API_KEY` - optional; clients must send `X-API-Key`
4. Smoke test: `GET https://<user>-<space>.hf.space/api/health`, then
   `POST /api/scan` with `{"domains":["buffer.com"],"isLinkedin":true}`.

Base URL: `https://<user>-<space>.hf.space` (lowercase; `.` and `_` become `-`).
Interactive docs: `/docs`.

## Private Space

Every request needs `Authorization: Bearer <hf_token>`. Do not put that token in browser
code: proxy through your own backend, or make the Space public and protect it with `API_KEY`
and `CORS_ORIGINS` (note `API_KEY` in a browser bundle is also visible).

## What will not work on HF

| Feature | Status | Reason |
|---|---|---|
| SMTP verification / `isVerify` | unavailable | port 25 blocked; `webapp.py` disables it with a log line |
| Keyless search | mostly blocked | shared egress IPs get captcha'd; use `SERPER_API_KEY` |
| Persisted files in `out/` | none | filesystem is ephemeral; use the MongoDB cache |
| Long scans | not advisable | ~2 vCPU on the free tier |

Free Spaces sleep after ~48h idle; the first request afterwards is slow.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/api/health` 404 | the Space is still on the old Gradio SDK: check README front matter says `sdk: docker` and rebuild |
| 401 from HF | private Space: send the HF bearer token |
| 401 `missing or invalid X-API-Key` | `API_KEY` is set: send the header |
| Browser CORS error | set `CORS_ORIGINS` to the UI origin |
| `mongo cache: disabled` in logs | `MONGODB_URI` missing or unreachable (`/api/health` says why) |
