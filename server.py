"""Single-process server: /api/* JSON API plus /ui (Gradio, when installed).

    python -m server                     # http://localhost:8000  (API)  + /ui
    uvicorn server:app --host 0.0.0.0 --port 8000

Environment: MONGODB_URI, MONGODB_DB, CORS_ORIGINS, SERPER_API_KEY, GITHUB_TOKEN.
"""

from __future__ import annotations

import os

from ehunter.api import create_app
from ehunter.config import load_dotenv

load_dotenv()

app = create_app()

try:  # the Gradio UI is optional; the API must run even without it
    import gradio as gr

    from app import build_ui

    app = gr.mount_gradio_app(app, build_ui(), path="/ui")
    print("[server] UI mounted at /ui")
except Exception as exc:  # noqa: BLE001
    print(f"[server] UI disabled ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT") or 8000)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
