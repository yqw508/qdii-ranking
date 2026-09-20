"""Generic JSON cache primitives with stable stats and atomic writes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from ..atomic import atomic_write_text


def parse_cache_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    corrupt_rebuilds: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "corrupt_rebuilds": self.corrupt_rebuilds,
        }


class JsonFileCache:
    """Reusable file cache; source-specific validation stays in the codec."""

    def __init__(
        self,
        directory: Path,
        decode: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.directory = directory
        self.decode = decode or (lambda payload: payload)
        self.stats = CacheStats()
        self._lock = Lock()

    def load(self, key: str) -> Any | None:
        path = self.directory / f"{key}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = self.decode(payload)
        except FileNotFoundError:
            with self._lock:
                self.stats.misses += 1
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            with self._lock:
                self.stats.misses += 1
                self.stats.corrupt_rebuilds += 1
            return None
        with self._lock:
            self.stats.hits += 1
        return result

    def save(self, key: str, payload: dict[str, Any]) -> None:
        atomic_write_text(
            self.directory / f"{key}.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
