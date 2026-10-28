"""Validation and rendering for generated fund report comparisons."""

from __future__ import annotations

import html
import json
import posixpath
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPORT_COMPARISON_SCHEMA_VERSION = 1
_CODE_RE = re.compile(r"\d{6}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}\Z")
_ROOT = "report-comparisons/"


class ReportComparisonError(ValueError):
    """Raised when a report comparison index violates its public contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReportComparisonError(message)


def _parse_iso_date(value: Any, label: str) -> date:
    _require(isinstance(value, str), f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReportComparisonError(f"{label} is not an ISO date") from exc


def _parse_iso_datetime(value: Any, label: str) -> datetime:
    _require(isinstance(value, str) and value, f"{label} must be an ISO datetime")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReportComparisonError(f"{label} is not an ISO datetime") from exc


def _site_path(value: Any, label: str, code: str, *, directory: bool = False) -> str:
    _require(isinstance(value, str) and value, f"{label} must be a site-relative path")
    parsed = urlparse(value)
    _require(
        not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment,
        f"{label} must be a site-relative path",
    )
    _require(not value.startswith("/") and "\\" not in value, f"{label} is not safe")
    pieces = value.split("/")
    _require(".." not in pieces and "." not in pieces, f"{label} contains traversal")
    normalized = posixpath.normpath(value)
    prefix = f"{_ROOT}{code}"
    _require(normalized == prefix or normalized.startswith(prefix + "/"), f"{label} must stay under {prefix}/")
    if directory:
        _require(value.endswith("/"), f"{label} must be a directory path")
    return value


def _source_url(value: Any, label: str) -> str:
    _require(isinstance(value, str) and value, f"{label} is missing")
    parsed = urlparse(value)
    _require(parsed.scheme == "https" and bool(parsed.netloc), f"{label} must be HTTPS")
    _require(not parsed.username and not parsed.password, f"{label} must not contain credentials")
    return value


def _validate_period(raw: Any, code: str, index: int) -> dict[str, Any]:
    _require(isinstance(raw, dict), f"{code} comparison period {index} must be an object")
    for field in ("label", "report_date", "published_date", "source_url"):
        _require(field in raw, f"{code} comparison period {index} is missing {field}")
    _require(isinstance(raw["label"], str) and raw["label"].strip(), f"{code} comparison period label is empty")
    report_date = _parse_iso_date(raw["report_date"], f"{code} comparison report_date")
    published_date = _parse_iso_date(raw["published_date"], f"{code} comparison published_date")
    _require(published_date >= report_date, f"{code} comparison publication predates report")
    return {
        "label": raw["label"].strip(),
        "report_date": report_date.isoformat(),
        "published_date": published_date.isoformat(),
        "source_url": _source_url(raw["source_url"], f"{code} comparison source_url"),
    }


def _validate_comparison(raw: Any, code: str, index: int) -> dict[str, Any]:
    _require(isinstance(raw, dict), f"{code} comparison {index} must be an object")
    for field in ("id", "title", "generated_at", "summary", "periods", "detail_url"):
        _require(field in raw, f"{code} comparison {index} is missing {field}")
    comparison_id = raw["id"]
    _require(isinstance(comparison_id, str) and _ID_RE.fullmatch(comparison_id), f"{code} comparison id is invalid")
    _require(isinstance(raw["title"], str) and raw["title"].strip(), f"{code} comparison title is empty")
    _parse_iso_datetime(raw["generated_at"], f"{code} comparison generated_at")
    _require(isinstance(raw["summary"], str), f"{code} comparison summary must be text")
    _require(isinstance(raw["periods"], list) and len(raw["periods"]) >= 2, f"{code} comparison needs at least two periods")
    periods = [_validate_period(period, code, period_index) for period_index, period in enumerate(raw["periods"], start=1)]
    detail_url = _site_path(raw["detail_url"], f"{code} comparison detail_url", code)
    return {
        "id": comparison_id,
        "title": raw["title"].strip(),
        "generated_at": raw["generated_at"],
        "summary": raw["summary"].strip(),
        "periods": periods,
        "detail_url": detail_url,
    }


def validate_report_comparison_index(payload: Any) -> dict[str, Any]:
    _require(isinstance(payload, dict), "Report comparison index must be an object")
    _require(payload.get("schema_version") == REPORT_COMPARISON_SCHEMA_VERSION, "Unexpected report comparison schema version")
    funds = payload.get("funds")
    _require(isinstance(funds, dict), "Report comparison funds must be an object")
    normalized_funds: dict[str, Any] = {}
    for code, raw_fund in funds.items():
        _require(isinstance(code, str) and _CODE_RE.fullmatch(code), f"Invalid report comparison fund code: {code!r}")
        _require(isinstance(raw_fund, dict), f"{code} report comparison fund must be an object")
        _require(isinstance(raw_fund.get("name"), str) and raw_fund["name"].strip(), f"{code} report comparison name is empty")
        list_url = _site_path(raw_fund.get("list_url"), f"{code} list_url", code, directory=True)
        raw_comparisons = raw_fund.get("comparisons")
        _require(isinstance(raw_comparisons, list), f"{code} comparisons must be a list")
        comparisons = [_validate_comparison(item, code, index) for index, item in enumerate(raw_comparisons, start=1)]
        ids = [item["id"] for item in comparisons]
        _require(len(ids) == len(set(ids)), f"{code} comparisons contain duplicate ids")
        comparisons.sort(
            key=lambda item: (
                max(period["report_date"] for period in item["periods"]),
                item["generated_at"],
                item["id"],
            ),
            reverse=True,
        )
        normalized_funds[code] = {
            "name": raw_fund["name"].strip(),
            "list_url": list_url,
            "comparisons": comparisons,
        }
    return {"schema_version": REPORT_COMPARISON_SCHEMA_VERSION, "funds": normalized_funds}


def load_report_comparison_index(path: Path) -> dict[str, Any]:
    """Load a generated index; a missing file is the empty index."""
    if not path.exists():
        return {"schema_version": REPORT_COMPARISON_SCHEMA_VERSION, "funds": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportComparisonError(f"Could not read report comparison index: {path}") from exc
    return validate_report_comparison_index(payload)


def _relative_href(from_directory: str, target: str) -> str:
    return posixpath.relpath(target, start=from_directory).replace("\\", "/")


def render_report_comparison_list(path: Path, fund: dict[str, Any]) -> None:
    """Render one fund's generated comparison list page."""
    list_directory = fund["list_url"].rstrip("/")
    cards: list[str] = []
    for comparison in fund["comparisons"]:
        periods = "".join(
            "<li><strong>"
            + html.escape(period["label"])
            + "</strong><span>报告期 "
            + html.escape(period["report_date"])
            + " · 发布 "
            + html.escape(period["published_date"])
            + "</span><a href=\""
            + html.escape(period["source_url"], quote=True)
            + "\" target=\"_blank\" rel=\"noopener noreferrer\">原始公告/PDF ↗</a></li>"
            for period in comparison["periods"]
        )
        detail_href = _relative_href(list_directory, comparison["detail_url"])
        cards.append(
            "<article class=\"comparison-card\"><div class=\"comparison-heading\"><div><h2>"
            + html.escape(comparison["title"])
            + "</h2><p>生成于 "
            + html.escape(comparison["generated_at"])
            + "</p></div><a class=\"comparison-action\" href=\""
            + html.escape(detail_href, quote=True)
            + "\">查看对比 ↗</a></div><p class=\"comparison-summary\">"
            + html.escape(comparison["summary"])
            + "</p><ul class=\"period-list\">"
            + periods
            + "</ul></article>"
        )
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__ · 财报对比</title><style>
:root{color-scheme:light;--bg:#f3f5f7;--surface:#fff;--text:#18222c;--muted:#66727e;--border:#d7dde3;--blue:#195c9b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;overflow-wrap:anywhere}.page{width:min(100%,900px);margin:0 auto;padding:24px 14px 36px}.back{display:inline-block;margin-bottom:16px;color:var(--blue);font-weight:700;text-decoration:none}.header{padding:16px 0 20px}.header h1{margin:0;font-size:24px}.header p{margin:6px 0 0;color:var(--muted)}.comparison-list{display:grid;gap:12px}.comparison-card{padding:16px;border:1px solid var(--border);border-radius:8px;background:var(--surface)}.comparison-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.comparison-heading h2{margin:0;font-size:18px}.comparison-heading p{margin:4px 0 0;color:var(--muted);font-size:12px}.comparison-action{flex:0 0 auto;padding:7px 10px;border-radius:4px;background:var(--blue);color:#fff;font-weight:700;text-decoration:none;white-space:nowrap}.comparison-summary{margin:14px 0 10px}.period-list{display:grid;gap:8px;margin:0;padding:0;list-style:none}.period-list li{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px 14px;align-items:baseline;padding-top:8px;border-top:1px solid #e5e9ed}.period-list li span{color:var(--muted);font-size:12px}.period-list li a{color:var(--blue);font-size:12px;font-weight:700;text-decoration:none}@media(max-width:640px){.page{padding-top:16px}.comparison-heading{display:grid}.comparison-action{justify-self:start}.period-list li{grid-template-columns:1fr}.period-list li span{order:2}.period-list li a{order:3}}
</style></head><body><main class="page"><a class="back" href="../../">← 返回榜单</a><header class="header"><h1>__TITLE__</h1><p>已生成 __COUNT__ 条财报对比</p></header><section class="comparison-list">__CARDS__</section></main></body></html>
"""
    document = document.replace("__TITLE__", html.escape(fund["name"])).replace("__COUNT__", str(len(fund["comparisons"]))).replace("__CARDS__", "".join(cards))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def render_report_comparison_pages(publish_dir: Path, index: dict[str, Any]) -> None:
    """Render list pages for funds that have at least one comparison."""
    for code, fund in index["funds"].items():
        if not fund["comparisons"]:
            continue
        relative_directory = fund["list_url"].rstrip("/").split("/")
        render_report_comparison_list(
            publish_dir.joinpath(*relative_directory, "index.html"), fund
        )


__all__ = [
    "REPORT_COMPARISON_SCHEMA_VERSION",
    "ReportComparisonError",
    "load_report_comparison_index",
    "render_report_comparison_list",
    "render_report_comparison_pages",
    "validate_report_comparison_index",
]
