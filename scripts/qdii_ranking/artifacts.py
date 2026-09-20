"""Compatibility artifact writers shared by the CLI and internal caches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cache.base import write_json_atomic
from .config import RANKING_SCHEMA_VERSION
from .renderers.csv_renderer import render_csv
from .renderers.html_renderer import render_html
from .renderers.json_renderer import render_json
from .renderers.markdown_renderer import render_markdown


def write_json(path: Path, payload: dict[str, Any]) -> None:
    if payload.get("schema_version") == RANKING_SCHEMA_VERSION:
        render_json(path, payload)
    else:
        write_json_atomic(path, payload)


def write_csv(path: Path, payload: dict[str, Any]) -> None:
    render_csv(path, payload)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    render_markdown(path, payload)


def write_html(path: Path, payload: dict[str, Any]) -> None:
    render_html(path, payload)


__all__ = ["write_csv", "write_html", "write_json", "write_markdown"]
