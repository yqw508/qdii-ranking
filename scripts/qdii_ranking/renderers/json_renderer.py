"""JSON renderer for the ranking public contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..atomic import atomic_write_text
from ..contracts import ensure_schema


def render_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_schema(payload)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
