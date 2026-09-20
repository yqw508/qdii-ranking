"""Announcement-index and periodic-report source adapter."""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, timedelta
from typing import Any, Iterable

from ..cache.base import parse_cache_date as parse_source_date
from ..config import ANNOUNCEMENT_API_URL, NOTICE_TITLE_RE, REPORT_TITLE_EXCLUDE_RE
from ..errors import DataError
from ..models import AnnouncementRecord, FundAnnouncementSnapshot, PeriodicReport


def parse_periodic_report_date(title: str) -> date | None:
    if REPORT_TITLE_EXCLUDE_RE.search(title):
        return None
    year_match = re.search(r"([0-9〇零○ＯO一二三四五六七八九]{4})\s*年", title)
    if not year_match:
        return None
    translation = str.maketrans("〇零○ＯO一二三四五六七八九", "00000123456789")
    try:
        year = int(year_match.group(1).translate(translation))
    except ValueError:
        return None
    quarter_match = re.search(r"第\s*([1234一二三四])\s*季度报告", title)
    if quarter_match:
        quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(
            quarter_match.group(1), int(quarter_match.group(1)) if quarter_match.group(1).isdigit() else 0
        )
        month, day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
        return date(year, month, day)
    if re.search(r"(?:中期|半年度)报告", title):
        return date(year, 6, 30)
    if re.search(r"年度报告", title):
        return date(year, 12, 31)
    return None



def fetch_latest_periodic_report(
    client: HttpClient,
    code: str,
    as_of: date,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> PeriodicReport:
    if snapshot is not None:
        if snapshot.code != code or snapshot.as_of != as_of:
            raise DataError("Announcement snapshot identity does not match report request")
        latest_ids = set(snapshot.latest_page_ids)
        records = tuple(
            item
            for item in snapshot.items
            if not latest_ids or item.announcement_id in latest_ids
        )
    else:
        records = tuple(
            _parse_announcement_page(_announcement_page(client, code, 1), code)
        )
    reports: list[PeriodicReport] = []
    for item in records:
        report_date = parse_periodic_report_date(item.title)
        if report_date is None:
            continue
        published = item.published_date
        if published > as_of or report_date > as_of:
            continue
        announcement_id = item.announcement_id
        reports.append(
            PeriodicReport(
                announcement_id=announcement_id,
                title=item.title,
                report_date=report_date,
                published_date=published,
                source_url=item.source_url,
            )
        )
    if not reports:
        raise DataError(f"No readable periodic report was disclosed by {as_of} for fund {code}")
    return max(reports, key=lambda item: (item.report_date, item.published_date))





def _announcement_page(
    client: HttpClient, code: str, page_index: int, page_size: int = 100
) -> dict[str, Any]:
    params = {
        "fundcode": code,
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "type": "0",
    }
    url = f"{ANNOUNCEMENT_API_URL}?{urllib.parse.urlencode(params)}"
    return client.get_json(url, referer=f"https://fundf10.eastmoney.com/jjgg_{code}.html")



def _parse_announcement_page(
    payload: dict[str, Any], code: str
) -> list[AnnouncementRecord]:
    items = payload.get("Data") or []
    if not isinstance(items, list):
        raise DataError(f"Announcement index is invalid for fund {code}")
    records: list[AnnouncementRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise DataError(f"Announcement index row is invalid for fund {code}")
        announcement_id = str(item.get("ID") or "").strip()
        title = str(item.get("TITLE") or "").strip()
        published_raw = str(item.get("PUBLISHDATEDesc") or "").strip()
        if not announcement_id or not title or not published_raw:
            continue
        records.append(
            AnnouncementRecord(
                announcement_id,
                title,
                parse_source_date(published_raw),
            )
        )
    return records



def _announcement_has_legal_pair(items: Iterable[AnnouncementRecord]) -> bool:
    has_prospectus = any(
        "招募说明书" in item.title
        and "提示性公告" not in item.title
        and "摘要" not in item.title
        for item in items
    )
    has_summary = any(
        "基金产品资料概要" in item.title and _is_rmb_product_summary(item.title)
        for item in items
    )
    return has_prospectus and has_summary





def _is_rmb_product_summary(title: str) -> bool:
    if "提示性公告" in title or "美元" in title or "港币" in title:
        return False
    if re.search(r"人民币[CD]|[CD](?:类)?(?:份额)?人民币|\([CD]类份额\)|（[CD]类份额）", title):
        return False
    return True



def fetch_announcements(
    client: HttpClient,
    code: str,
    as_of: date,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> list[dict[str, Any]]:
    if snapshot is not None:
        if snapshot.code != code or snapshot.as_of != as_of:
            raise DataError("Announcement snapshot identity does not match quota request")
        latest_ids = set(snapshot.latest_page_ids)
        records = tuple(
            item
            for item in snapshot.items
            if not latest_ids or item.announcement_id in latest_ids
        )
    else:
        records = tuple(
            _parse_announcement_page(_announcement_page(client, code, 1), code)
        )
    notices = []
    cutoff = as_of - timedelta(days=550)
    for item in records:
        published = item.published_date
        if published > as_of or published < cutoff or not NOTICE_TITLE_RE.search(item.title):
            continue
        announcement_id = item.announcement_id
        notices.append(
            {
                "id": announcement_id,
                "title": item.title,
                "published": published,
                "url": item.source_url,
            }
        )
    notices.sort(key=lambda item: item["published"])
    return notices[-12:]
