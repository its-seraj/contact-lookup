"""Single-process server: the /api/* JSON API (no UI; the front end is a separate repo).

    python -m server                     # http://localhost:8000
    uvicorn server:app --host 0.0.0.0 --port 8000

Environment: MONGODB_URI, MONGODB_DB, CORS_ORIGINS, SERPER_API_KEY, GITHUB_TOKEN.
"""

from __future__ import annotations

import logging
import os

from ehunter.api import app as app  # module-level app: created once, plugins mount onto it
from ehunter.config import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("pymongo").setLevel(logging.WARNING)

from ehunter.config import env as _env  # noqa: E402  (after load_dotenv)

for _name in ("SERPER_API_KEY", "BRAVE_API_KEY", "GOOGLE_CSE_KEY", "GITHUB_TOKEN",
              "SEARXNG_URL", "SMTP_PROXY", "VERIFY_FROM"):
    print(f"[server] {_name}: {'set' if _env(_name) else '-'}", flush=True)

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT") or 8000)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
