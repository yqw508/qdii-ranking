"""Announcement-index cache with same-run source revalidation."""

from __future__ import annotations

import json
import math
import os
from datetime import date
from pathlib import Path
from threading import Lock, get_ident
from typing import Any

from ..config import ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION
from ..documents import extract_pdf_text
from ..errors import DataError
from ..sources.announcements import (
    _announcement_has_legal_pair,
    _announcement_page,
    _parse_announcement_page,
)
from .base import parse_cache_date, write_json_atomic


def _announcement_record(*args: Any, **kwargs: Any) -> Any:
    from ..models import AnnouncementRecord

    return AnnouncementRecord(*args, **kwargs)


def _announcement_snapshot(*args: Any, **kwargs: Any) -> Any:
    from ..models import FundAnnouncementSnapshot

    return FundAnnouncementSnapshot(*args, **kwargs)


def _source_announcement_page(*args: Any, **kwargs: Any) -> Any:
    return _announcement_page(*args, **kwargs)


def _source_parse_announcement_page(*args: Any, **kwargs: Any) -> Any:
    return _parse_announcement_page(*args, **kwargs)


def _source_announcement_has_legal_pair(*args: Any, **kwargs: Any) -> Any:
    return _announcement_has_legal_pair(*args, **kwargs)


def _extract_pdf_text(payload: bytes) -> str:
    return extract_pdf_text(payload)


class AnnouncementIndexCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.checks = 0
        self.pages_fetched = 0
        self.full_seeds = 0
        self.cache_loads = 0
        self.corrupt_rebuilds = 0
        self._stats_lock = Lock()
        self._key_locks: dict[str, Lock] = {}
        self._key_locks_lock = Lock()

    @staticmethod
    def _decode(
        payload: dict[str, Any], code: str
    ) -> tuple[dict[str, AnnouncementRecord], bool]:
        if (
            payload.get("schema_version") != ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION
            or payload.get("code") != code
            or not isinstance(payload.get("items"), list)
            or not isinstance(payload.get("history_seeded"), bool)
        ):
            raise DataError("Cached announcement index identity is invalid")
        items: dict[str, AnnouncementRecord] = {}
        for raw in payload["items"]:
            if not isinstance(raw, dict) or set(raw) != {
                "id",
                "title",
                "published_date",
            }:
                raise DataError("Cached announcement index row is invalid")
            record = _announcement_record(
                str(raw["id"]),
                str(raw["title"]),
                parse_cache_date(str(raw["published_date"])),
            )
            if not record.announcement_id or record.announcement_id in items:
                raise DataError("Cached announcement index contains duplicate IDs")
            items[record.announcement_id] = record
        return items, bool(payload["history_seeded"])

    @staticmethod
    def _encode(
        code: str,
        items: dict[str, AnnouncementRecord],
        history_seeded: bool,
    ) -> dict[str, Any]:
        ordered = sorted(
            items.values(),
            key=lambda item: (item.published_date, item.announcement_id),
            reverse=True,
        )
        return {
            "schema_version": ANNOUNCEMENT_INDEX_CACHE_SCHEMA_VERSION,
            "code": code,
            "history_seeded": history_seeded,
            "items": [
                {
                    "id": item.announcement_id,
                    "title": item.title,
                    "published_date": item.published_date.isoformat(),
                }
                for item in ordered
            ],
        }

    def _get_locked(
        self, client: HttpClient, code: str, as_of: date
    ) -> FundAnnouncementSnapshot:
        path = self.directory / f"{code}.json"
        cached: dict[str, AnnouncementRecord] = {}
        history_seeded = False
        if path.exists():
            try:
                cached, history_seeded = self._decode(
                    json.loads(path.read_text(encoding="utf-8")), code
                )
                with self._stats_lock:
                    self.cache_loads += 1
            except (DataError, OSError, ValueError, json.JSONDecodeError):
                with self._stats_lock:
                    self.corrupt_rebuilds += 1
                cached = {}
                history_seeded = False

        first = _source_announcement_page(client, code, 1)
        with self._stats_lock:
            self.checks += 1
            self.pages_fetched += 1
        first_records = _source_parse_announcement_page(first, code)
        latest_page_ids = tuple(record.announcement_id for record in first_records)
        for record in first_records:
            cached[record.announcement_id] = record
        total_count = int(first.get("TotalCount") or len(cached))
        page_size = int(first.get("PageSize") or 100)
        total_pages = max(1, math.ceil(total_count / max(1, page_size)))

        if not history_seeded:
            with self._stats_lock:
                self.full_seeds += 1
            page = 2
            while page <= total_pages and not _source_announcement_has_legal_pair(
                item for item in cached.values() if item.published_date <= as_of
            ):
                payload = _source_announcement_page(client, code, page)
                with self._stats_lock:
                    self.pages_fetched += 1
                page_items = _source_parse_announcement_page(payload, code)
                for record in page_items:
                    cached[record.announcement_id] = record
                if not page_items:
                    break
                page += 1
            history_seeded = True

        write_json_atomic(path, self._encode(code, cached, history_seeded))
        visible = tuple(
            sorted(
                (
                    item
                    for item in cached.values()
                    if item.published_date <= as_of
                ),
                key=lambda item: (item.published_date, item.announcement_id),
                reverse=True,
            )
        )
        if not visible:
            raise DataError(f"No announcements were disclosed by {as_of} for fund {code}")
        return _announcement_snapshot(code, as_of, visible, latest_page_ids)

    def get(
        self, client: HttpClient, code: str, as_of: date
    ) -> FundAnnouncementSnapshot:
        with self._key_locks_lock:
            key_lock = self._key_locks.setdefault(code, Lock())
        with key_lock:
            return self._get_locked(client, code, as_of)

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "checks": self.checks,
                "pages_fetched": self.pages_fetched,
                "full_seeds": self.full_seeds,
                "cache_loads": self.cache_loads,
                "corrupt_rebuilds": self.corrupt_rebuilds,
            }

class PeriodicReportCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.hits = 0
        self.downloads = 0
        self.corrupt_redownloads = 0
        self.text_extractions = 0
        self._stats_lock = Lock()
        self._key_locks: dict[str, Lock] = {}
        self._key_locks_lock = Lock()

    @staticmethod
    def _validate(pdf_bytes: bytes) -> str:
        if len(pdf_bytes) < 1000 or not pdf_bytes.lstrip().startswith(b"%PDF-"):
            raise DataError("Downloaded periodic report is not a valid PDF")
        text = _extract_pdf_text(pdf_bytes)
        if not text.strip():
            raise DataError("Downloaded periodic report has no extractable text")
        return text

    def get_text(
        self,
        client: HttpClient,
        report: PeriodicReport | LegalDocument,
        referer: str,
        force_refresh: bool = False,
    ) -> str:
        with self._key_locks_lock:
            key_lock = self._key_locks.setdefault(report.announcement_id, Lock())
        with key_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{report.announcement_id}.pdf"
            if path.exists() and not force_refresh:
                try:
                    text = self._validate(path.read_bytes())
                    with self._stats_lock:
                        self.hits += 1
                        self.text_extractions += 1
                    return text
                except (DataError, OSError):
                    with self._stats_lock:
                        self.corrupt_redownloads += 1
            pdf_bytes = client.get_bytes(report.source_url, referer=referer)
            text = self._validate(pdf_bytes)
            temporary = path.with_name(
                f"{path.name}.{os.getpid()}.{get_ident()}.tmp"
            )
            temporary.write_bytes(pdf_bytes)
            temporary.replace(path)
            with self._stats_lock:
                self.downloads += 1
                self.text_extractions += 1
            return text

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "hits": self.hits,
                "downloads": self.downloads,
                "corrupt_redownloads": self.corrupt_redownloads,
                "text_extractions": self.text_extractions,
            }



__all__ = ["AnnouncementIndexCache", "PeriodicReportCache"]
