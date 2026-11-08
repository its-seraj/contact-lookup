"""CSV / JSON writers and a compact console summary."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from .models import CSV_COLUMNS

STATUS_BADGE = {
    "valid": "valid",
    "unverified": "unverified",
    "pattern": "inferred",
    "name_only": "name only",
    "catch_all": "catch-all",
    "unknown": "unknown",
    "greylisted": "greylist",
    "risky": "risky",
    "invalid": "invalid",
}


def write_csv(path: Path, records) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record.to_row())
    return path


def write_json(path: Path, result) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "domain": result.domain,
        "meta": result.meta,
        "employees": [record.to_row() for record in result.records],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _clip(text: str, width: int) -> str:
    text = str(text or "")
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def _table(rows, headers) -> str:
    widths = []
    for index, header in enumerate(headers):
        widths.append(
            max(len(header), max((len(str(r[index])) for r in rows), default=0))
        )
    lines = []
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    lines.append(header_line)
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def summary(result, limit: int = 25, out=None) -> str:
    out = out or sys.stdout
    meta = result.meta or {}
    records = result.records

    def emit(line: str = "") -> None:
        print(line, file=out)

    emit("")
    emit(f"  domain      : {result.domain}")
    emit(f"  people      : {len(records)}  ({meta.get('with_email', 0)} with email, "
         f"{meta.get('verified', 0)} SMTP-verified)")
    if meta.get("pattern"):
        emit(f"  pattern     : {meta['pattern']}")
    mail = meta.get("mail") or {}
    if mail.get("providers"):
        emit(f"  mail        : {', '.join(mail['providers'])}")
    emit(f"  elapsed     : {meta.get('elapsed_seconds', 0)}s")

    if records:
        rows = []
        for record in records[:limit]:
            rows.append([
                _clip(STATUS_BADGE.get(record.status, record.status), 10),
                _clip(record.full_name or "-", 24),
                _clip(record.title or "-", 26),
                _clip(record.email or "-", 34),
                record.confidence,
                _clip(record.linkedin.replace("https://www.linkedin.com/in/", "") or "-", 20)
                if record.linkedin else "-",
            ])
        emit("")
        emit(_table(rows, ["status", "name", "title", "email", "conf", "linkedin"]))
        if len(records) > limit:
            emit(f"  ... {len(records) - limit} more row(s) in the exported files")

    ok = [s for s in result.sources if s.ok]
    skipped = [s for s in result.sources if s.skipped]
    failed = [s for s in result.sources if not s.ok and not s.skipped]
    if ok or skipped or failed:
        emit("")
        parts = [f"{s.source}={s.count}" for s in ok]
        if skipped:
            parts.append("skipped: " + ", ".join(s.source for s in skipped))
        if failed:
            parts.append("failed: " + ", ".join(s.source for s in failed))
        emit("  sources     : " + "; ".join(parts))

    for source in failed:
        emit(f"  ! {source.source}: {source.error}")
    return ""
