"""Hugging Face Space entry point (Gradio UI).

Deploy as a Space with SDK = gradio, app_file = app.py.
Secrets to add in Space settings: SERPER_API_KEY (or BRAVE_API_KEY / GOOGLE_CSE_KEY +
GOOGLE_CSE_CX / SEARXNG_URL), GITHUB_TOKEN, VERIFY_FROM.
"""

from __future__ import annotations

import os
import traceback

import gradio as gr

from ehunter.models import CSV_COLUMNS
from ehunter.webapp import DISPLAY_COLUMNS, run_scan

TITLE = "ehunter - domain to employees"
DESCRIPTION = """
Give it a company **domain** and get back every person the public web exposes for it:
name, headline, work email candidate, LinkedIn profile URL, GitHub handle, plus where each
row came from.

**What runs here:** website scraping, GitHub org members, PGP keyserver UIDs, and - when a
search key is configured - LinkedIn profile discovery and email dorks.

**What cannot run here:** SMTP mailbox verification. Hugging Face blocks outbound port 25,
so addresses are shown as *unverified guesses*. Run the same scan on a host with port 25
open (`ehunter scan domain --permute`) to confirm them.

Only scan domains you are allowed to research. Use a **private** Space: results contain
personal data.
"""


def _summary_markdown(result: dict) -> str:
    counts = result["counts"]
    meta = result["meta"]
    lines = [
        f"**{result['domain']}** - {counts['people']} people, "
        f"{counts['with_email']} with an email candidate, "
        f"{counts['verified']} verified, {counts['with_linkedin']} with a LinkedIn profile",
        f"pattern: `{meta.get('pattern') or 'not inferred'}` · "
        f"elapsed: {meta.get('elapsed_seconds', 0)}s",
    ]
    failed = [s for s in meta.get("sources", []) if not s.get("ok") and not s.get("skipped")]
    for source in failed:
        lines.append(f"- source `{source['name']}` failed: {source['error'][:160]}")
    return "\n\n".join(lines)


def _run(domain, do_linkedin, do_verify, max_pages, sweep_variants, permute_max):
    """Generator handler: rows appear as each pipeline stage finds them."""
    import threading
    import time

    from ehunter.progress import row_key

    state = {"rows": {}, "log": [], "done": False, "result": None, "error": ""}
    lock = threading.Lock()

    def log(message: str = "", **_kwargs) -> None:
        text = str(message)
        with lock:
            state["log"].append(text)
            if len(state["log"]) > 500:
                state["log"] = state["log"][-500:]

    def on_progress(payload: dict) -> None:
        with lock:
            for row in payload.get("rows", []):
                state["rows"][row_key(row)] = row
            for key in payload.get("removed", []):
                state["rows"].pop(key, None)

    def worker() -> None:
        try:
            state["result"] = run_scan(
                domain,
                linkedin=bool(do_linkedin),
                verify=bool(do_verify),
                max_pages=int(max_pages),
                sweep_variants=int(sweep_variants),
                permute_max=int(permute_max),
                log=log,
                progress=on_progress,
            )
        except ValueError as exc:
            state["error"] = f"error: {exc}"
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            state["done"] = True

    threading.Thread(target=worker, daemon=True).start()

    def render(status: str):
        with lock:
            rows = [[row.get(column, "") for column in DISPLAY_COLUMNS]
                    for row in state["rows"].values()]
            logs = "\n".join(state["log"][-200:])
        return rows, None, logs, status

    while not state["done"]:
        yield render(f"Scanning {domain} - {len(state['rows'])} row(s) so far...")
        time.sleep(0.8)

    if state["error"]:
        with lock:
            logs = "\n".join(state["log"])
        yield [], None, logs, f"### Failed\n\n`{state['error']}`"
        return

    result = state["result"]
    rows = [[row.get(column, "") for column in DISPLAY_COLUMNS] for row in result["rows"]]
    with lock:
        logs = "\n".join(state["log"])
    yield rows, result["csv_path"], logs, _summary_markdown(result)


def build_ui() -> "gr.Blocks":
    # Do not probe port 25 here: on a blocked network that costs 10s at cold start and
    # the answer is re-checked per run anyway.
    with gr.Blocks(**_blocks_kwargs()) as demo:
        gr.Markdown(f"# {TITLE}\n{DESCRIPTION}")
        gr.Markdown(
            "> **Hugging Face blocks outbound port 25**, so `Verify emails` cannot work "
            "here and is off by default. Addresses are pattern-based guesses marked "
            "`unverified`; run the scan on a host with port 25 open to confirm them."
        )

        with gr.Row():
            domain = gr.Textbox(
                label="Domain", placeholder="acme.com", scale=3,
                info="Registrable domain only, no scheme or path",
            )
            do_linkedin = gr.Checkbox(
                label="LinkedIn sweep", value=True,
                info="Costs search credits (variants x pages)",
            )
            do_verify = gr.Checkbox(
                label="Verify emails", value=False,
                info="Needs outbound port 25; unavailable on Hugging Face",
            )

        with gr.Accordion("Limits (keep small on the free CPU tier)", open=False):
            with gr.Row():
                max_pages = gr.Slider(2, 25, value=8, step=1, label="Website pages")
                sweep_variants = gr.Slider(2, 20, value=10, step=1, label="LinkedIn query variants")
                permute_max = gr.Slider(1, 19, value=19, step=1,
                                label="Email candidates per person")

        run_button = gr.Button("Scan", variant="primary")
        summary = gr.Markdown()
        table = gr.Dataframe(
            headers=DISPLAY_COLUMNS, wrap=True, interactive=False,
            label="Results", elem_id="results",
        )
        download = gr.File(label="Download CSV")
        logs_box = gr.Textbox(label="Log", lines=14, max_lines=24, autoscroll=True)

        run_button.click(
            _run,
            inputs=[domain, do_linkedin, do_verify, max_pages, sweep_variants, permute_max],
            outputs=[table, download, logs_box, summary],
        )
        gr.Examples(
            examples=[["buffer.com", True, False, 8, 8, 4],
                      ["posthog.com", True, False, 8, 10, 4]],
            inputs=[domain, do_linkedin, do_verify, max_pages, sweep_variants, permute_max],
        )
    return demo


demo = build_ui().queue(default_concurrency_limit=1, max_size=8)

def main() -> None:
    kwargs = _supported_kwargs(demo.launch, _launch_kwargs())
    try:
        demo.launch(**kwargs)
    except Exception:  # noqa: BLE001 - a Space must show the reason, not exit silently
        traceback.print_exc()
        print("[app] launch failed, retrying with defaults")
        demo.launch()


if __name__ == "__main__":
    main()
