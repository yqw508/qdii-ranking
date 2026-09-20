"""Atomic artifact writing shared by all output renderers."""

from __future__ import annotations

import os
from threading import get_ident
from pathlib import Path
from typing import Callable


def temporary_sibling(path: Path, label: str = "tmp") -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{get_ident()}.{label}")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(path)
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_render(path: Path, renderer: Callable[[Path], None]) -> None:
    """Render to a unique sibling, then publish with one filesystem replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(path, "rendering")
    try:
        renderer(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
