"""Quota-notice parse cache."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import QUOTA_NOTICE_CACHE_SCHEMA_VERSION, QUOTA_NOTICE_METHOD_VERSION
from ..errors import DataError
from .base import parse_cache_date, write_json_atomic


def _parse_quota_notice(*args: Any, **kwargs: Any) -> Any:
    from update_qdii_ranking import parse_quota_notice

    return parse_quota_notice(*args, **kwargs)


def _legal_document(*args: Any, **kwargs: Any) -> Any:
    from ..models import LegalDocument

    return LegalDocument(*args, **kwargs)


class QuotaNoticeParseCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.misses = 0
        self.failures = 0
        self.corrupt_rebuilds = 0
        self._run_failures: dict[tuple[str, str, str, str], str] = {}
        self._stats_lock = Lock()
        self._key_locks: dict[str, Lock] = {}
        self._key_locks_lock = Lock()

    @staticmethod
    def _identity(notice: dict[str, Any]) -> dict[str, str]:
        return {
            "id": str(notice["id"]),
            "title": str(notice["title"]),
            "published_date": notice["published"].isoformat(),
            "source_url": str(notice["url"]),
        }

    @staticmethod
    def _encode_transitions(
        transitions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                **transition,
                "effective_date": transition["effective_date"].isoformat(),
            }
            for transition in transitions
        ]

    @staticmethod
    def _decode(
        payload: dict[str, Any], identity: dict[str, str]
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        if (
            payload.get("schema_version") != QUOTA_NOTICE_CACHE_SCHEMA_VERSION
            or payload.get("method_version") != QUOTA_NOTICE_METHOD_VERSION
            or payload.get("identity") != identity
            or not isinstance(payload.get("ok"), bool)
        ):
            raise DataError("Cached quota notice identity is invalid")
        if not payload["ok"]:
            error = payload.get("error")
            if not isinstance(error, str) or not error:
                raise DataError("Cached quota notice failure is invalid")
            return None, error
        raw_transitions = payload.get("transitions")
        if not isinstance(raw_transitions, list) or not raw_transitions:
            raise DataError("Cached quota notice transitions are empty")
        transitions: list[dict[str, Any]] = []
        for item in raw_transitions:
            if not isinstance(item, dict) or "effective_date" not in item:
                raise DataError("Cached quota transition is invalid")
            transition = dict(item)
            transition["effective_date"] = parse_cache_date(str(item["effective_date"]))
            if transition.get("source_url") != identity["source_url"]:
                raise DataError("Cached quota transition source is invalid")
            transitions.append(transition)
        return transitions, None

    def _get_locked(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        notice: dict[str, Any],
        document_cache: PeriodicReportCache,
    ) -> list[dict[str, Any]]:
        identity = self._identity(notice)
        identity_key = (
            identity["id"],
            identity["title"],
            identity["published_date"],
            identity["source_url"],
        )
        run_error = self._run_failures.get(identity_key)
        if run_error is not None:
            with self._stats_lock:
                self.hits += 1
            raise DataError(run_error)
        path = self.directory / f"{identity['id']}.json"
        if path.exists():
            try:
                transitions, error = self._decode(
                    json.loads(path.read_text(encoding="utf-8")), identity
                )
                if error is None:
                    if transitions is None:
                        raise DataError("Cached quota notice result is missing")
                    with self._stats_lock:
                        self.hits += 1
                    return transitions
            except DataError:
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
            except (OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1

        document = _legal_document(
            identity["id"],
            identity["title"],
            notice["published"],
            identity["source_url"],
            "quota_notice",
        )
        try:
            text = document_cache.get_text(
                client,
                document,
                fund["fund_page_url"],
                force_refresh=True,
            )
            transitions = _parse_quota_notice(
                text, notice["published"], identity["source_url"]
            )
            if not transitions:
                raise DataError("quota notice produced no effective limit transition")
        except DataError as exc:
            self._run_failures[identity_key] = str(exc)
            with self._stats_lock:
                self.misses += 1
                self.failures += 1
            raise
        write_json_atomic(
            path,
            {
                "schema_version": QUOTA_NOTICE_CACHE_SCHEMA_VERSION,
                "method_version": QUOTA_NOTICE_METHOD_VERSION,
                "identity": identity,
                "ok": True,
                "transitions": self._encode_transitions(transitions),
            },
        )
        with self._stats_lock:
            self.misses += 1
        return transitions

    def get(
        self,
        client: HttpClient,
        fund: dict[str, Any],
        notice: dict[str, Any],
        document_cache: PeriodicReportCache,
    ) -> list[dict[str, Any]]:
        key = str(notice["id"])
        with self._key_locks_lock:
            key_lock = self._key_locks.setdefault(key, Lock())
        with key_lock:
            return self._get_locked(client, fund, notice, document_cache)

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "failures": self.failures,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }

__all__ = ["QuotaNoticeParseCache"]
