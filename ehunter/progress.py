"""Incremental row snapshots for streaming UIs.

The pipeline mutates one set of records from stage to stage. A UI should not wait for the
slow tail (SMTP verification) before showing anything, so each stage emits only the rows
that are **new or changed** since the previous stage, plus the keys that disappeared.

Rows are keyed by person identity (name first, then email, then GitHub handle) so an
address appearing later updates that row instead of replacing it.
"""

from __future__ import annotations

import json


def row_key(row: dict) -> str:
    """Stable identity for a table row across stages."""
    from .names import alpha

    first = alpha(row.get("first_name") or "")
    last = alpha(row.get("last_name") or "")
    if first and last:
        return f"n:{first}.{last}"
    if first:
        return f"n:{first}"
    email = (row.get("email") or "").strip().lower()
    if email:
        return f"e:{email}"
    handle = (row.get("github") or "").strip().lower()
    if handle:
        return f"g:{handle}"
    # Last resort: something stable for a row that has neither name nor address.
    return "x:" + str(abs(hash(json.dumps(row, sort_keys=True, default=str))))


class RowEmitter:
    """Turns a full record list into minimal deltas."""

    def __init__(self, emit):
        self._emit = emit
        self._seen: "dict[str, str]" = {}
        self._stages: "list[str]" = []

    @property
    def stages(self) -> "list[str]":
        return list(self._stages)

    def snapshot(self, rows, stage: str) -> None:
        if not self._emit:
            return
        current: "dict[str, str]" = {}
        changed: "list[dict]" = []
        for row in rows:
            key = row_key(row)
            blob = json.dumps(row, sort_keys=True, default=str)
            current[key] = blob
            if self._seen.get(key) != blob:
                changed.append(row)
        removed = [key for key in self._seen if key not in current]
        if changed or removed:
            self._stages.append(stage)
            self._emit({
                "stage": stage,
                "rows": changed,
                "removed": removed,
                "total": len(current),
                "rows_total": len(rows),
            })
        self._seen = current
