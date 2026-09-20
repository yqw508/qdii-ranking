"""Fund US-equity exposure result cache."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import FUND_EXPOSURE_CACHE_SCHEMA_VERSION, US_EQUITY_METHOD_VERSION
from ..errors import DataError
from .base import parse_cache_date, write_json_atomic


def _fetch_latest_periodic_report(*args: Any, **kwargs: Any) -> Any:
    from update_qdii_ranking import fetch_latest_periodic_report

    return fetch_latest_periodic_report(*args, **kwargs)


def _parse_us_equity_report(*args: Any, **kwargs: Any) -> Any:
    from update_qdii_ranking import parse_us_equity_report

    return parse_us_equity_report(*args, **kwargs)


def _calculate_us_equity_exposure_base(*args: Any, **kwargs: Any) -> Any:
    from update_qdii_ranking import calculate_us_equity_exposure_base

    return calculate_us_equity_exposure_base(*args, **kwargs)


def _apply_us_equity_threshold(*args: Any, **kwargs: Any) -> Any:
    from update_qdii_ranking import apply_us_equity_threshold

    return apply_us_equity_threshold(*args, **kwargs)


class FundExposureResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()

    @staticmethod
    def _validate(
        payload: dict[str, Any],
        code: str,
        report: PeriodicReport,
        as_of: date,
        catalog_fingerprint: str,
    ) -> tuple[dict[str, Any], list[str]]:
        if (
            payload.get("schema_version") != FUND_EXPOSURE_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != US_EQUITY_METHOD_VERSION
            or payload.get("catalog_fingerprint") != catalog_fingerprint
            or payload.get("code") != code
            or payload.get("announcement_id") != report.announcement_id
            or payload.get("report_date") != report.report_date.isoformat()
            or payload.get("published_date") != report.published_date.isoformat()
        ):
            raise DataError("Cached US-equity exposure identity does not match the report")
        if report.report_date > as_of or report.published_date > as_of:
            raise DataError("Cached US-equity exposure uses a future report")
        exposure = payload.get("exposure")
        warnings = payload.get("warnings")
        if not isinstance(exposure, dict) or not isinstance(warnings, list):
            raise DataError("Cached US-equity exposure is incomplete")
        required_fields = {
            "confirmed_pct",
            "possible_pct",
            "direct_us_pct",
            "lookthrough_confirmed_pct",
            "unresolved_pct",
            "report_date",
            "published_date",
            "source_url",
            "components",
        }
        if not required_fields.issubset(exposure) or not isinstance(
            exposure["components"], list
        ):
            raise DataError("Cached US-equity exposure fields are incomplete")
        if (
            exposure["report_date"] != report.report_date.isoformat()
            or exposure["published_date"] != report.published_date.isoformat()
            or exposure["source_url"] != report.source_url
        ):
            raise DataError("Cached US-equity exposure source does not match the report")
        confirmed = float(exposure["confirmed_pct"])
        possible = float(exposure["possible_pct"])
        if not 0 <= confirmed <= possible <= 100:
            raise DataError("Cached US-equity exposure interval is invalid")
        for component in exposure["components"]:
            if not isinstance(component, dict):
                raise DataError("Cached US-equity component is invalid")
            data_date = component.get("data_date")
            if data_date is None:
                continue
            observed = parse_cache_date(str(data_date))
            if observed > report.report_date:
                raise DataError("Cached US-equity component uses future data")
            if (
                component.get("category") == "global_equity"
                and (report.report_date - observed).days > 120
            ):
                raise DataError("Cached global-equity component is stale")
        if not all(isinstance(warning, str) for warning in warnings):
            raise DataError("Cached US-equity warnings are invalid")
        return dict(exposure), list(warnings)

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        as_of: date,
        report_cache: PeriodicReportCache,
        resolver: LookthroughResolver,
        threshold: float,
        report: PeriodicReport | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        code = fund["code"]
        if report is None:
            report = _fetch_latest_periodic_report(client, code, as_of)
        if report.report_date > as_of or report.published_date > as_of:
            raise DataError(f"Periodic report for fund {code} is later than {as_of}")
        path = self.directory / code / f"{report.announcement_id}.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                exposure, warnings = self._validate(
                    payload, code, report, as_of, resolver.catalog_fingerprint
                )
                with self._stats_lock:
                    self.hits += 1
                classified, threshold_warnings = _apply_us_equity_threshold(
                    exposure, threshold
                )
                return classified, [*warnings, *threshold_warnings]
            except (DataError, OSError, TypeError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
        with self._stats_lock:
            self.misses += 1
        text = report_cache.get_text(client, report, fund["fund_page_url"])
        parsed = _parse_us_equity_report(text, code)
        exposure, warnings = _calculate_us_equity_exposure_base(parsed, report, resolver)
        write_json_atomic(
            path,
            {
                "schema_version": FUND_EXPOSURE_CACHE_SCHEMA_VERSION,
                "method_version": US_EQUITY_METHOD_VERSION,
                "catalog_fingerprint": resolver.catalog_fingerprint,
                "code": code,
                "announcement_id": report.announcement_id,
                "report_date": report.report_date.isoformat(),
                "published_date": report.published_date.isoformat(),
                "exposure": exposure,
                "warnings": warnings,
            },
        )
        classified, threshold_warnings = _apply_us_equity_threshold(exposure, threshold)
        return classified, [*warnings, *threshold_warnings]

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }

__all__ = ["FundExposureResultCache"]
