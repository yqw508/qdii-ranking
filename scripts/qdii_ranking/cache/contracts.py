"""Contract-benchmark and holding-cost result cache."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import CONTRACT_RESULT_CACHE_SCHEMA_VERSION, CONTRACT_RESULT_METHOD_VERSION
from ..errors import DataError
from ..sources.contracts import fetch_latest_legal_documents, resolve_contract_benchmark
from .base import parse_cache_date, write_json_atomic


def _fetch_latest_legal_documents(*args: Any, **kwargs: Any) -> Any:
    return fetch_latest_legal_documents(*args, **kwargs)


def _resolve_contract_benchmark(*args: Any, **kwargs: Any) -> Any:
    return resolve_contract_benchmark(*args, **kwargs)


class ContractProfileResultCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()

    @staticmethod
    def _fund_identity(fund: dict[str, Any]) -> str:
        return hashlib.sha256(
            f"{fund['code']}\0{fund['name']}\0{fund['fund_type']}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _document_ids(
        client: HttpClient,
        code: str,
        as_of: date,
        snapshot: FundAnnouncementSnapshot,
    ) -> tuple[str | None, str | None]:
        prospectus, summary = _fetch_latest_legal_documents(
            client, code, as_of, snapshot=snapshot
        )
        return (
            prospectus.announcement_id if prospectus else None,
            summary.announcement_id if summary else None,
        )

    @staticmethod
    def _validate(
        payload: dict[str, Any],
        code: str,
        fund_identity: str,
        prospectus_id: str | None,
        summary_id: str | None,
        catalog_fingerprint: str,
        as_of: date,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        if (
            payload.get("schema_version") != CONTRACT_RESULT_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != CONTRACT_RESULT_METHOD_VERSION
            or payload.get("code") != code
            or payload.get("fund_identity") != fund_identity
            or payload.get("prospectus_id") != prospectus_id
            or payload.get("summary_id") != summary_id
            or payload.get("catalog_fingerprint") != catalog_fingerprint
        ):
            raise DataError("Cached contract profile identity is invalid")
        profile = payload.get("profile")
        holding_cost = payload.get("holding_cost")
        warnings = payload.get("warnings")
        if (
            not isinstance(profile, dict)
            or not isinstance(holding_cost, dict)
            or not isinstance(warnings, list)
            or not all(isinstance(item, str) for item in warnings)
        ):
            raise DataError("Cached contract profile result is incomplete")
        for raw_date in (
            profile.get("prospectus_published_date"),
            profile.get("product_summary_published_date"),
            holding_cost.get("source_published_date"),
            holding_cost.get("measurement_date"),
        ):
            if raw_date is not None and parse_cache_date(str(raw_date)) > as_of:
                raise DataError("Cached contract profile contains future data")
        return dict(profile), dict(holding_cost), list(warnings)

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        as_of: date,
        document_cache: PeriodicReportCache,
        catalog: ContractBenchmarkCatalog,
        snapshot: FundAnnouncementSnapshot,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        code = fund["code"]
        prospectus_id, summary_id = self._document_ids(
            client, code, as_of, snapshot
        )
        fund_identity = self._fund_identity(fund)
        path = self.directory / f"{code}.json"
        if path.exists():
            try:
                result = self._validate(
                    json.loads(path.read_text(encoding="utf-8")),
                    code,
                    fund_identity,
                    prospectus_id,
                    summary_id,
                    catalog.fingerprint,
                    as_of,
                )
                with self._stats_lock:
                    self.hits += 1
                return result
            except (DataError, OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
        result = _resolve_contract_benchmark(
            client,
            fund,
            as_of,
            document_cache,
            catalog,
            snapshot=snapshot,
        )
        profile, holding_cost, warnings = result
        write_json_atomic(
            path,
            {
                "schema_version": CONTRACT_RESULT_CACHE_SCHEMA_VERSION,
                "method_version": CONTRACT_RESULT_METHOD_VERSION,
                "code": code,
                "fund_identity": fund_identity,
                "prospectus_id": prospectus_id,
                "summary_id": summary_id,
                "catalog_fingerprint": catalog.fingerprint,
                "profile": profile,
                "holding_cost": holding_cost,
                "warnings": warnings,
            },
        )
        with self._stats_lock:
            self.misses += 1
        return result

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }

__all__ = ["ContractProfileResultCache"]
