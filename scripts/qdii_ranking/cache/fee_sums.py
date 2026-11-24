"""Persistent cache for the fund-archive three-fee sum (display-only data)."""

from __future__ import annotations

import json
import math
import zlib
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

from ..errors import DataError
from .base import parse_cache_date, write_json_atomic

FEE_SUM_CACHE_SCHEMA_VERSION = 1
FEE_SUM_MAX_AGE_DAYS = 7
FEE_SUM_MIN_AGE_DAYS = 5


def fee_sum_max_age_days(code: str) -> int:
    """Fixed per-code lifetime of 5-7 days so same-day entries expire on different days."""
    span = FEE_SUM_MAX_AGE_DAYS - FEE_SUM_MIN_AGE_DAYS + 1
    return FEE_SUM_MIN_AGE_DAYS + zlib.crc32(code.encode("ascii")) % span
# A newer announcement with any of these words forces a fresh page read.
FEE_CHANGE_KEYWORDS = (
    "费率", "管理费", "托管费", "销售服务费", "产品资料概要", "招募说明书", "基金合同",
)
_COMPONENTS = {"management_pct", "custody_pct", "sales_service_pct"}


class FeeSumCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.fetches = 0
        self.expired = 0
        self.revalidated_by_announcement = 0
        self.no_snapshot = 0
        self.corrupt_rebuilds = 0
        self.failures = 0
        self._lock = Lock()

    def _bump(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def load(self, code: str, as_of: date) -> dict[str, Any] | None:
        path = self.directory / f"{code}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != FEE_SUM_CACHE_SCHEMA_VERSION or payload.get("code") != code:
                raise DataError("Fee-sum cache identity is invalid")
            cost = payload["cost"]
            parts = cost.get("components") or {}
            fetched = parse_cache_date(str(cost["source_published_date"]))
            if (
                cost.get("status") != "fee_sum"
                or set(parts) != _COMPONENTS
                or not math.isclose(
                    round(sum(float(v) for v in parts.values()), 2),
                    float(cost["annualized_pct"]), abs_tol=1e-9,
                )
                or fetched > as_of
            ):
                raise DataError("Fee-sum cache content is invalid")
            return dict(cost)
        except (DataError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._bump("corrupt_rebuilds")
            return None

    def save(self, code: str, cost: dict[str, Any]) -> None:
        if cost.get("status") != "fee_sum":
            return  # Never persist an unavailable result.
        write_json_atomic(
            self.directory / f"{code}.json",
            {"schema_version": FEE_SUM_CACHE_SCHEMA_VERSION, "code": code, "cost": cost},
        )

    def refresh_reason(
        self, cost: dict[str, Any] | None, snapshot: Any, as_of: date, code: str
    ) -> str | None:
        """Return why the cached value cannot be reused, or None to reuse it."""
        if cost is None:
            return "missing"
        fetched = parse_cache_date(str(cost["source_published_date"]))
        if (as_of - fetched).days >= fee_sum_max_age_days(code):
            return "expired"
        if snapshot is None:
            return "no_snapshot"
        for item in snapshot.items:
            if item.published_date >= fetched and any(word in item.title for word in FEE_CHANGE_KEYWORDS):
                return "revalidated_by_announcement"
        return None

    def record(self, reason: str | None, ok: bool) -> None:
        if reason is None:
            self._bump("hits")
            return
        self._bump("fetches")
        if reason in {"expired", "revalidated_by_announcement", "no_snapshot"}:
            self._bump(reason)
        if not ok:
            self._bump("failures")

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self.hits,
                "fetches": self.fetches,
                "expired": self.expired,
                "revalidated_by_announcement": self.revalidated_by_announcement,
                "no_snapshot": self.no_snapshot,
                "failures": self.failures,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }
