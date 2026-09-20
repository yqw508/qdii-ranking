"""Conditional NAV-history cache and performance result service."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import (
    BENCHMARK_MAX_STALENESS_DAYS,
    PERFORMANCE_CACHE_SCHEMA_VERSION,
    PERFORMANCE_DATA_URL,
)
from ..errors import DataError
from ..sources.performance import calculate_performance_from_points, parse_performance_page
from .base import parse_cache_date, write_json_atomic


def _parse_performance_page(payload: str, code: str) -> list[dict[str, Any]]:
    return parse_performance_page(payload, code)


def _calculate_performance_from_points(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
    return calculate_performance_from_points(*args, **kwargs)


class PerformanceResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.corrupt_rebuilds = 0
        self.conditional_requests = 0
        self.not_modified = 0
        self.updates = 0
        self._stats_lock = Lock()

    @staticmethod
    def _decode_points(payload: dict[str, Any], code: str) -> tuple[list[dict[str, Any]], str | None]:
        if (
            payload.get("schema_version") != PERFORMANCE_CACHE_SCHEMA_VERSION
            or payload.get("code") != code
            or not isinstance(payload.get("points"), list)
        ):
            raise DataError("Cached NAV history identity is invalid")
        points: list[dict[str, Any]] = []
        previous: date | None = None
        for item in payload["points"]:
            if not isinstance(item, dict) or set(item) != {
                "date",
                "nav",
                "equity_return_pct",
                "unit_money",
            }:
                raise DataError("Cached NAV history row is invalid")
            observed = parse_cache_date(str(item["date"]))
            nav = float(item["nav"])
            daily_return = item["equity_return_pct"]
            if (
                (previous is not None and observed <= previous)
                or not math.isfinite(nav)
                or nav <= 0
                or (
                    daily_return is not None
                    and not math.isfinite(float(daily_return))
                )
            ):
                raise DataError("Cached NAV history contains invalid values")
            points.append(
                {
                    "date": observed,
                    "nav": nav,
                    "equity_return_pct": (
                        None if daily_return is None else float(daily_return)
                    ),
                    "unit_money": str(item["unit_money"]),
                }
            )
            previous = observed
        if not points:
            raise DataError("Cached NAV history is empty")
        last_modified = payload.get("last_modified")
        if last_modified is not None and not isinstance(last_modified, str):
            raise DataError("Cached NAV Last-Modified value is invalid")
        return points, last_modified

    @staticmethod
    def _encode_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "date": point["date"].isoformat(),
                "nav": point["nav"],
                "equity_return_pct": point["equity_return_pct"],
                "unit_money": point["unit_money"],
            }
            for point in points
        ]

    @staticmethod
    def _validate_page_snapshot(
        points: list[dict[str, Any]],
        fund: dict[str, Any],
        as_of: date,
        allow_page_lead: bool = False,
    ) -> str | None:
        raw_date = fund.get("latest_nav_date")
        raw_value = fund.get("latest_nav_value")
        if raw_date is None or raw_value is None:
            return None
        observed = parse_cache_date(str(raw_date))
        if observed > as_of:
            return None
        match = next((point for point in reversed(points) if point["date"] == observed), None)
        if match is not None:
            if math.isclose(
                float(match["nav"]), float(raw_value), rel_tol=0, abs_tol=1e-8
            ):
                return None
            raise DataError(
                f"NAV history for {fund['code']} does not match fund-page observation "
                f"{observed}={float(raw_value):g}"
            )
        latest = max(
            (point for point in points if point["date"] <= as_of),
            key=lambda point: point["date"],
        )
        lag_days = (observed - latest["date"]).days
        if allow_page_lead and 0 < lag_days <= BENCHMARK_MAX_STALENESS_DAYS:
            return (
                f"{fund['code']} 的基金主页净值已更新至 {observed}，完整复权净值历史仍为 "
                f"{latest['date']}；已强制重新验证完整历史并按后者计算。"
            )
        raise DataError(
            f"NAV history for {fund['code']} does not contain fund-page observation "
            f"{observed}={float(raw_value):g}"
        )

    def _save(
        self,
        path: Path,
        code: str,
        last_modified: str | None,
        points: list[dict[str, Any]],
    ) -> None:
        write_json_atomic(
            path,
            {
                "schema_version": PERFORMANCE_CACHE_SCHEMA_VERSION,
                "code": code,
                "last_modified": last_modified,
                "points": self._encode_points(points),
            },
        )

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        as_of: date,
        benchmark: Nasdaq100Benchmark,
    ) -> tuple[dict[str, Any], list[str]]:
        code = fund["code"]
        path = self.directory / "nav-history" / f"{code}.json"
        points: list[dict[str, Any]] | None = None
        last_modified: str | None = None
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                points, last_modified = self._decode_points(payload, code)
            except (DataError, OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
                points = None
                last_modified = None

        url = PERFORMANCE_DATA_URL.format(code=code, cache_buster=as_of.strftime("%Y%m%d"))
        if last_modified:
            with self._stats_lock:
                self.conditional_requests += 1
        status, response_text, response_last_modified = client.get_conditional_text(
            url,
            referer=fund["fund_page_url"],
            last_modified=last_modified,
        )
        if status == 304:
            if points is None:
                raise DataError(f"NAV source returned 304 without a cache for fund {code}")
            try:
                self._validate_page_snapshot(points, fund, as_of)
            except DataError:
                status, response_text, response_last_modified = client.get_conditional_text(
                    url, referer=fund["fund_page_url"]
                )
                if status != 200 or response_text is None:
                    raise
            else:
                with self._stats_lock:
                    self.hits += 1
                    self.not_modified += 1
                return _calculate_performance_from_points(
                    points, code, as_of, url, benchmark,
                    inception_date=fund.get("inception_date"),
                )

        if status != 200 or response_text is None:
            raise DataError(f"Unexpected NAV history response {status} for fund {code}")
        points = _parse_performance_page(response_text, code)
        page_warning = self._validate_page_snapshot(
            points, fund, as_of, allow_page_lead=True
        )
        with self._stats_lock:
            self.misses += 1
            self.updates += 1
        self._save(path, code, response_last_modified, points)
        performance, warnings = _calculate_performance_from_points(
            points, code, as_of, url, benchmark,
            inception_date=fund.get("inception_date"),
        )
        if page_warning is not None:
            warnings.append(page_warning)
        return performance, warnings

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "corrupt_rebuilds": self.corrupt_rebuilds,
                "conditional_requests": self.conditional_requests,
                "not_modified": self.not_modified,
                "updates": self.updates,
            }

__all__ = ["PerformanceResultCache"]
