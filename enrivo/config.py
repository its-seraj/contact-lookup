"""Env loading without third-party deps. Reads ./.env once, keeps os.environ as source of truth."""

from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def load_dotenv(path: "str | None" = None) -> None:
    """Parse a minimal .env (KEY=VALUE, # comments, optional quotes). Never overrides real env."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    candidates = [Path(path)] if path else [Path.cwd() / ".env"]
    # also look next to the package when run as a module from elsewhere
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    for p in candidates:
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:]
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
        break


def env(name: str, default: "str | None" = None) -> "str | None":
    load_dotenv()
    val = os.environ.get(name)
    if val is None or not str(val).strip():
        return default
    return str(val).strip()


def has_env(*names: str) -> bool:
    return all(env(n) for n in names)
