"""Listed-premium holding-cost cache."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import ETF_HOLDING_COST_CACHE_SCHEMA_VERSION, ETF_HOLDING_COST_METHOD_VERSION
from ..errors import DataError
from .base import parse_cache_date, write_json_atomic


class ExchangePremiumHoldingCostCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.stale_fallbacks = 0
        self.writes = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()

    @staticmethod
    def _decode(
        payload: dict[str, Any], code: str, as_of: date
    ) -> tuple[str | None, dict[str, Any]]:
        if (
            payload.get("schema_version") != ETF_HOLDING_COST_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != ETF_HOLDING_COST_METHOD_VERSION
            or payload.get("code") != code
            or (
                payload.get("summary_id") is not None
                and not isinstance(payload.get("summary_id"), str)
            )
            or not isinstance(payload.get("holding_cost"), dict)
        ):
            raise DataError("Cached ETF holding-cost identity is invalid")
        cost = dict(payload["holding_cost"])
        if set(cost) != {
            "status",
            "annualized_pct",
            "measurement_date",
            "source_title",
            "source_published_date",
            "source_url",
        } or cost["status"] not in {"parsed", "unavailable"}:
            raise DataError("Cached ETF holding-cost fields are invalid")
        value = cost["annualized_pct"]
        if cost["status"] == "parsed":
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise DataError("Cached ETF holding cost is non-numeric") from exc
            if not math.isfinite(numeric) or numeric < 0 or numeric > 100:
                raise DataError("Cached ETF holding cost is outside its valid range")
            cost["annualized_pct"] = round(numeric, 2)
            if (
                not isinstance(cost["source_title"], str)
                or not cost["source_title"].strip()
                or not isinstance(cost["source_published_date"], str)
                or not str(cost["source_url"] or "").startswith("https://")
            ):
                raise DataError("Cached ETF holding-cost source is invalid")
        else:
            if value is not None:
                raise DataError("Unavailable cached ETF holding cost has a value")
            source_values = (
                cost["source_title"],
                cost["source_published_date"],
                cost["source_url"],
            )
            if not (
                all(item is None for item in source_values)
                or (
                    isinstance(source_values[0], str)
                    and bool(source_values[0].strip())
                    and isinstance(source_values[1], str)
                    and str(source_values[2] or "").startswith("https://")
                )
            ):
                raise DataError("Unavailable cached ETF holding-cost source is incomplete")
        for field in ("measurement_date", "source_published_date"):
            raw_date = cost[field]
            if raw_date is not None and parse_cache_date(str(raw_date)) > as_of:
                raise DataError("Cached ETF holding cost contains future data")
        return payload.get("summary_id"), cost

    def load(
        self, code: str, as_of: date
    ) -> tuple[str | None, dict[str, Any]] | None:
        path = self.directory / f"{code}.json"
        if not path.exists():
            return None
        try:
            result = self._decode(
                json.loads(path.read_text(encoding="utf-8")), code, as_of
            )
        except (DataError, OSError, ValueError, json.JSONDecodeError):
            with self._stats_lock:
                self.corrupt_rebuilds += 1
            return None
        return result

    def save(
        self, code: str, summary_id: str | None, holding_cost: dict[str, Any]
    ) -> None:
        write_json_atomic(
            self.directory / f"{code}.json",
            {
                "schema_version": ETF_HOLDING_COST_CACHE_SCHEMA_VERSION,
                "method_version": ETF_HOLDING_COST_METHOD_VERSION,
                "code": code,
                "summary_id": summary_id,
                "holding_cost": holding_cost,
            },
        )
        with self._stats_lock:
            self.writes += 1

    def mark_hit(self) -> None:
        with self._stats_lock:
            self.hits += 1

    def mark_miss(self) -> None:
        with self._stats_lock:
            self.misses += 1

    def mark_stale_fallback(self) -> None:
        with self._stats_lock:
            self.stale_fallbacks += 1

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "stale_fallbacks": self.stale_fallbacks,
                "writes": self.writes,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }

__all__ = ["ExchangePremiumHoldingCostCache"]
