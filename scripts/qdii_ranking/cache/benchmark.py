"""Nasdaq-100 CNY benchmark cache."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import (
    BENCHMARK_CACHE_SCHEMA_VERSION,
    BENCHMARK_HISTORY_BUFFER_DAYS,
    BENCHMARK_MAX_STALENESS_DAYS,
    BENCHMARK_WINDOW_YEARS,
)
from ..errors import DataError
from ..runtime import years_ago
from ..sources.benchmark import fetch_nasdaq100_history, fetch_safe_usd_cny_history
from .base import parse_cache_date, write_json_atomic


def _fetch_nasdaq100_history(*args: Any, **kwargs: Any) -> Any:
    return fetch_nasdaq100_history(*args, **kwargs)


def _fetch_safe_usd_cny_history(*args: Any, **kwargs: Any) -> Any:
    return fetch_safe_usd_cny_history(*args, **kwargs)


def _years_ago(*args: Any, **kwargs: Any) -> Any:
    return years_ago(*args, **kwargs)


def _nasdaq100_benchmark(*args: Any, **kwargs: Any) -> Any:
    from ..models import Nasdaq100Benchmark

    return Nasdaq100Benchmark(*args, **kwargs)


class Nasdaq100BenchmarkCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cache_hits = 0
        self.fetches = 0
        self.fallbacks = 0

    @staticmethod
    def _decode_series(payload: Any, value_field: str) -> dict[date, float]:
        if not isinstance(payload, list):
            raise DataError("Benchmark cache series is not a list")
        points: dict[date, float] = {}
        for item in payload:
            if not isinstance(item, dict) or set(item) != {"date", value_field}:
                raise DataError("Benchmark cache row is invalid")
            observed = parse_cache_date(str(item["date"]))
            value = float(item[value_field])
            if not math.isfinite(value) or value <= 0 or observed in points:
                raise DataError("Benchmark cache contains an invalid or duplicate point")
            points[observed] = value
        return points

    def _load(self) -> tuple[dict[date, float], dict[date, float]]:
        if not self.path.exists():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != BENCHMARK_CACHE_SCHEMA_VERSION:
                raise DataError("Benchmark cache schema is incompatible")
            xndx = self._decode_series(payload.get("xndx"), "value")
            fx = self._decode_series(payload.get("usd_cny"), "rate")
            if not xndx or not fx:
                raise DataError("Benchmark cache is empty")
            self.cache_hits += 1
            return xndx, fx
        except (DataError, OSError, ValueError, json.JSONDecodeError):
            return {}, {}

    def _save(self, xndx: dict[date, float], fx: dict[date, float]) -> None:
        write_json_atomic(
            self.path,
            {
                "schema_version": BENCHMARK_CACHE_SCHEMA_VERSION,
                "benchmark": "XNDX_CNY",
                "xndx": [
                    {"date": observed.isoformat(), "value": xndx[observed]}
                    for observed in sorted(xndx)
                ],
                "usd_cny": [
                    {"date": observed.isoformat(), "rate": fx[observed]}
                    for observed in sorted(fx)
                ],
            },
        )

    @staticmethod
    def _validate_coverage(
        series: dict[date, float], label: str, required_start: date, as_of: date
    ) -> None:
        if not series or min(series) > required_start + timedelta(
            days=BENCHMARK_MAX_STALENESS_DAYS
        ):
            raise DataError(f"{label} history does not cover the required three-year window")
        latest = max(observed for observed in series if observed <= as_of)
        if (as_of - latest).days > BENCHMARK_MAX_STALENESS_DAYS:
            raise DataError(f"{label} history is stale as of {as_of}: latest {latest}")

    def get(self, client: HttpClient, as_of: date) -> tuple[Nasdaq100Benchmark, list[str]]:
        required_start = _years_ago(as_of, BENCHMARK_WINDOW_YEARS) - timedelta(
            days=BENCHMARK_HISTORY_BUFFER_DAYS
        )
        xndx, fx = self._load()
        xndx = {observed: value for observed, value in xndx.items() if observed <= as_of}
        fx = {observed: value for observed, value in fx.items() if observed <= as_of}
        warnings: list[str] = []
        cache_start = required_start - timedelta(days=BENCHMARK_MAX_STALENESS_DAYS)
        if (
            not xndx
            or not fx
            or min(xndx) > required_start + timedelta(days=BENCHMARK_MAX_STALENESS_DAYS)
            or min(fx) > required_start + timedelta(days=BENCHMARK_MAX_STALENESS_DAYS)
        ):
            fetch_start = cache_start
        else:
            fetch_start = max(cache_start, min(max(xndx), max(fx)) - timedelta(days=7))

        sources = (
            ("Nasdaq XNDX", xndx, _fetch_nasdaq100_history),
            ("SAFE USD/CNY", fx, _fetch_safe_usd_cny_history),
        )
        for label, series, fetcher in sources:
            try:
                series.update(fetcher(client, fetch_start, as_of))
                self.fetches += 1
            except DataError:
                if not series:
                    raise
                self.fallbacks += 1
                warnings.append(
                    f"纳指100基准更新失败，使用完整缓存：{label} 最新数据 "
                    f"{max(observed for observed in series if observed <= as_of).isoformat()}。"
                )

        xndx = {
            observed: value
            for observed, value in xndx.items()
            if cache_start <= observed <= as_of
        }
        fx = {
            observed: value
            for observed, value in fx.items()
            if cache_start <= observed <= as_of
        }
        self._validate_coverage(xndx, "Nasdaq XNDX", required_start, as_of)
        self._validate_coverage(fx, "SAFE USD/CNY", required_start, as_of)
        self._save(xndx, fx)
        return _nasdaq100_benchmark(xndx, fx), warnings

    def stats(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "fetches": self.fetches,
            "fallbacks": self.fallbacks,
        }

__all__ = ["Nasdaq100BenchmarkCache"]
