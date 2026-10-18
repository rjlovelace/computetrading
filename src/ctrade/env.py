"""Load settings from a `.env` file so they don't have to be exported by hand."""

from __future__ import annotations

import os
from pathlib import Path


def find_env_file(start: Path | None = None) -> Path | None:
    """The nearest `.env` in the current folder or one of its parents."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    return None


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines; blank lines, # comments and a leading `export` are allowed."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Path | None = None) -> Path | None:
    """Set variables from `.env`; anything already set in the environment wins."""
    path = path or find_env_file()
    if path is None:
        return None
    for key, value in parse_env(path.read_text(encoding="utf-8-sig")).items():
        os.environ.setdefault(key, value)
    return path
