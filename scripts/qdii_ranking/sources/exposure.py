"""Periodic-report parsing and conservative US-equity look-through."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from ..atomic import atomic_write_text
from ..errors import DataError
from ..models import FundAnnouncementSnapshot, PeriodicReport
from ..runtime import HttpClient, parse_date
from .announcements import fetch_latest_periodic_report


class ReportTextCache(Protocol):
    def get_text(
        self, client: HttpClient, report: PeriodicReport, referer: str
    ) -> str: ...


class ExposureResultCache(Protocol):
    def get(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any], list[str]]: ...


def normalize_instrument_name(value: str) -> str:
    return re.sub(
        r"[^\u4e00-\u9fffA-Z0-9]+", "", value.upper().replace("V AN", "VAN")
    )


class LookthroughResolver:
    def __init__(self, catalog_path: Path, cache_path: Path) -> None:
        try:
            catalog_bytes = catalog_path.read_bytes()
            catalog = json.loads(catalog_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not load US-equity instrument catalog: {exc}") from exc
        self.entries = catalog.get("entries") or []
        self.catalog_fingerprint = hashlib.sha256(catalog_bytes).hexdigest()
        self.cache_path = cache_path
        self.cache: dict[str, dict[str, Any]] = {}
        if cache_path.exists():
            try:
                saved = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    saved.get("schema_version") == 2
                    and saved.get("catalog_fingerprint") == self.catalog_fingerprint
                ):
                    self.cache = saved.get("entries") or {}
            except (OSError, json.JSONDecodeError):
                self.cache = {}
        self.hits = 0
        self.misses = 0
        self._lock = Lock()

    @staticmethod
    def _usable(result: dict[str, Any], report_date: date) -> bool:
        category = result.get("category")
        if category in {"us_equity", "non_us_equity", "fixed_income", "commodity"}:
            return bool(result.get("source_url"))
        if category != "global_equity" or result.get("data_date") is None:
            return False
        observed = parse_date(str(result["data_date"]))
        return observed <= report_date and (report_date - observed).days <= 120

    def _from_catalog(self, normalized_name: str, report_date: date) -> dict[str, Any] | None:
        for entry in self.entries:
            aliases = [normalize_instrument_name(alias) for alias in entry.get("aliases") or []]
            if not any(alias and alias in normalized_name for alias in aliases):
                continue
            result = {
                "category": entry["category"],
                "us_equity_pct": float(entry["us_equity_pct"]),
                "data_date": entry.get("data_date"),
                "source_url": entry["source_url"],
                "source_name": entry.get("source_name"),
            }
            if self._usable(result, report_date):
                return result
        return None

    def resolve(self, fund_name: str, report_date: date) -> dict[str, Any] | None:
        normalized = normalize_instrument_name(fund_name)
        result = self._from_catalog(normalized, report_date)
        if result is None:
            with self._lock:
                self.misses += 1
            return None
        key = "|".join(
            (
                normalized,
                report_date.isoformat(),
                str(result.get("data_date") or "structural"),
                str(result["source_url"]),
            )
        )
        with self._lock:
            cached = self.cache.get(key)
            if cached is not None and self._usable(cached, report_date):
                self.hits += 1
                return cached
            self.misses += 1
            self.cache[key] = result
            self._save()
            return result

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.cache_path,
            json.dumps({
                "schema_version": 2,
                "catalog_fingerprint": self.catalog_fingerprint,
                "entries": self.cache,
            }, ensure_ascii=False, indent=2),
        )

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses}


def clean_report_text(text: str) -> str:
    header = re.compile(
        r"(?m)^.*?[0-9〇零○ＯO一二三四五六七八九]{4}\s*年"
        r"(?:第\s*[1234一二三四]\s*季度|半年度|年度).*?报告\s*$\n^\s*\d+\s*$\n?"
    )
    text = header.sub("", text)
    text = re.sub(r"(?<=\d)\.\s+(?=\d{1,2}(?:\s|$))", ".", text)
    text = re.sub(
        r"(\d{1,3}(?:,\d{3})+\.\d)\s+(\d)(?=\s)", r"\1\2", text
    )
    text = re.sub(
        r"(\d{1,3}(?:,\d{3})+,\d{1,2})\s+(\d{1,2}\.\d{2})(?=\s)",
        r"\1\2",
        text,
    )
    return re.sub(r"(\d{1,3}(?:,\d{3})+)\s+(\.\d{2})(?=\s)", r"\1\2", text)


def parse_fund_investment_rows(text: str, code: str) -> list[dict[str, Any]]:
    headings = list(re.finditer(r"前十名基金投资明\s*细", text))
    if not headings:
        raise DataError(f"Could not locate top fund investments for fund {code}")
    heading = headings[-1]
    end = re.search(r"投资组合报告附注", text[heading.end() :])
    if not end:
        raise DataError(f"Could not locate end of top fund investments for fund {code}")
    table = text[heading.end() : heading.end() + end.start()]
    row_matches = list(re.finditer(r"(?m)^\s*(10|[1-9])\s+", table))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(row_matches):
        row_end = row_matches[index + 1].start() if index + 1 < len(row_matches) else len(table)
        body = re.sub(r"\s+", " ", table[match.end() : row_end]).strip()
        name_match = re.match(
            r"(.+?)\s+(?:ETF\s*基\s*金|指数基\s*金|开放式\s*基\s*金|基\s*金)\s+",
            body,
        )
        if not name_match:
            name_match = re.match(r"(.+?)\s+ETF\s+(?:交易型|契约型)", body)
        if not name_match:
            name_match = re.match(r"(.+?)\s+QDII\s+(?:交易型|契约型|开放式)", body)
        if not name_match:
            name_match = re.match(
                r"(.+?)\s+(?:债\s*券\s*型|股\s*票\s*型|混\s*合\s*型|商\s*品\s*型|权\s*益\s*类)\s+",
                body,
            )
        if not name_match:
            name_match = re.match(
                r"(.+?)\s+(?:债\s*券\s*型|股\s*票\s*型|混\s*合\s*型|商\s*品\s*型|权\s*益\s*类)"
                r"(?:指\s*数)?基\s*金\s+",
                body,
            )
        # Periodic reports commonly render unused rows as dash-only placeholders.
        # A page footer may follow the dashes (and include the word "基金"), so
        # identify the placeholder before the generic "基金" fallback below.
        if not name_match and re.match(r"^(?:-\s*){2,}", body):
            continue
        if not name_match and "基金" not in body:
            continue
        percentages = [
            float(value)
            for value in re.findall(r"(?<![\d,])(\d{1,3}\.\d{2})(?!\d)", body)
            if float(value) <= 100
        ]
        # A dash in the percentage column denotes a negligible holding
        # (the reports use it instead of a rounded 0.00).  Preserve the row
        # with a zero weight so the rest of the look-through scan remains
        # complete, while still failing on genuinely malformed rows.
        if not percentages and name_match and re.search(r"\d[\d,]*\.\d{2}\s+-", body):
            percentages = [0.0]
        if not name_match or not percentages:
            raise DataError(
                f"Could not parse top fund investment row {match.group(1)} for fund {code}"
            )
        fund_name = name_match.group(1)
        if re.search(r"商\s*品\s*型", body):
            reported_category = "commodity"
        elif re.search(r"债\s*券\s*型", body):
            reported_category = "fixed_income"
        else:
            reported_category = None
        if len(normalize_instrument_name(fund_name)) < 12:
            continuation = re.search(
                r"\d{1,3}\.\d{2}\s+([A-Z][A-Z0-9& ]+?ETF)(?:\s+[A-Z][a-z]|\s*$)",
                body,
            )
            if continuation:
                fund_name = f"{fund_name} {continuation.group(1)}"
        rows.append(
            {
                "rank": int(match.group(1)),
                "fund_name": fund_name,
                "weight_pct": percentages[0],
                "reported_category": reported_category,
            }
        )
    if not rows:
        raise DataError(f"No top fund investments were parsed for fund {code}")
    return rows


def parse_us_equity_report(text: str, code: str) -> dict[str, Any]:
    text = clean_report_text(text)
    direct_headings = list(re.finditer(
        r"(?:报告期末|期末)在各个国家（地区）证券市场的"
        r"(?:股票及存托\s*凭证|权益)投资分\s*布",
        text,
    ))
    if not direct_headings:
        raise DataError(f"Could not locate country equity distribution for fund {code}")
    direct_heading = direct_headings[-1]
    direct_segment = text[direct_heading.end() : direct_heading.end() + 1200]
    next_heading = re.search(r"\n\s*\d+(?:\.\d+)+\s*", direct_segment)
    if next_heading:
        direct_segment = direct_segment[: next_heading.start()]
    direct_match = re.search(
        r"美国\s+([\d,.]+)\s+(\d+(?:\.\d+)?)", re.sub(r"\s+", " ", direct_segment)
    )
    direct_us_pct = float(direct_match.group(2)) if direct_match else 0.0

    asset_headings = list(re.finditer(r"(?:报告期末|期末)基金资产组合情况", text))
    if not asset_headings:
        raise DataError(f"Could not locate asset allocation table for fund {code}")
    asset_heading = asset_headings[-1]
    asset_segment = re.sub(r"\s+", " ", text[asset_heading.end() : asset_heading.end() + 2500])
    fund_amount_match = re.search(r"(?:^|\s)2\s+基金投资\s+([\d,.]+)\s+\d+(?:\.\d+)?", asset_segment)
    no_fund_investment = bool(
        re.search(r"(?:^|\s)2\s+基金投资\s+(?:[－—-]\s*){1,2}(?:\s|$)", asset_segment)
    )
    if not fund_amount_match and not no_fund_investment:
        raise DataError(f"Could not parse fund investment amount for fund {code}")
    fund_amount = (
        float(fund_amount_match.group(1).replace(",", "")) if fund_amount_match else 0.0
    )

    holdings: list[dict[str, Any]] = []
    total_fund_pct = 0.0
    if fund_amount > 0:
        net_match = re.search(
            r"期末基金\s*资产\s*净值(.{0,1400}?)"
            r"5\.\s*期末基金\s*份额\s*净值",
            text,
            re.S,
        )
        net_values: list[float] = []
        if net_match:
            net_values = [
                float(value.replace(",", ""))
                for value in re.findall(r"(?<!\d)(\d[\d,]*\.\d{2})(?!\d)", net_match.group(1))
            ]
        else:
            # Midyear and annual reports disclose the aggregate in their balance sheet.
            balance_headings = list(re.finditer(r"\d+\.\d+\s+资产负债表", text))
            for balance_heading in reversed(balance_headings):
                balance_segment = text[balance_heading.end() : balance_heading.end() + 8000]
                balance_match = re.search(r"净资产合计\s+(\d[\d,]*\.\d{2})", balance_segment)
                if balance_match:
                    net_values = [float(balance_match.group(1).replace(",", ""))]
                    break
        if not net_values or sum(net_values) <= 0:
            raise DataError(f"Could not parse positive fund net assets for fund {code}")
        total_fund_pct = fund_amount / sum(net_values) * 100
        holdings = parse_fund_investment_rows(text, code)
    return {
        "direct_us_pct": round(direct_us_pct, 4),
        "fund_investment_pct": round(total_fund_pct, 4),
        "fund_holdings": holdings,
    }


def calculate_us_equity_exposure_base(
    parsed: dict[str, Any],
    report: PeriodicReport,
    resolver: LookthroughResolver,
) -> tuple[dict[str, Any], list[str]]:
    direct_us_pct = float(parsed["direct_us_pct"])
    lookthrough_confirmed = 0.0
    unresolved = 0.0
    components: list[dict[str, Any]] = []
    warnings: list[str] = []
    disclosed_weight = 0.0
    for holding in parsed["fund_holdings"]:
        weight = float(holding["weight_pct"])
        disclosed_weight += weight
        if holding.get("reported_category") in {"commodity", "fixed_income"}:
            resolved = {
                "category": holding["reported_category"],
                "us_equity_pct": 0.0,
                "data_date": report.report_date.isoformat(),
                "source_url": report.source_url,
            }
        else:
            resolved = resolver.resolve(holding["fund_name"], report.report_date)
        if resolved is None:
            contribution = 0.0
            possible_contribution = weight
            unresolved += weight
            if weight >= 0.005:
                warnings.append(
                    f"{holding['fund_name']} 无法按 {report.report_date} 的可用数据穿透，"
                    f"其 {weight:.2f}% 仓位仅计入可能上限。"
                )
            category = "unresolved"
            exposure_pct = None
            source_url = None
            data_date = None
        else:
            exposure_pct = float(resolved["us_equity_pct"])
            contribution = weight * exposure_pct / 100
            possible_contribution = contribution
            category = str(resolved["category"])
            source_url = resolved.get("source_url")
            data_date = resolved.get("data_date")
            lookthrough_confirmed += contribution
        components.append(
            {
                "fund_name": holding["fund_name"],
                "weight_pct": round(weight, 4),
                "category": category,
                "us_equity_pct": exposure_pct,
                "confirmed_contribution_pct": round(contribution, 4),
                "possible_contribution_pct": round(possible_contribution, 4),
                "data_date": data_date,
                "source_url": source_url,
            }
        )
    residual = max(0.0, float(parsed["fund_investment_pct"]) - disclosed_weight)
    if residual > 0.01:
        unresolved += residual
        warnings.append(
            f"前十大基金之外尚有 {residual:.2f}% 基金仓位未披露，仅计入可能上限。"
        )
        components.append(
            {
                "fund_name": "前十大之外未披露基金仓位",
                "weight_pct": round(residual, 4),
                "category": "unresolved_residual",
                "us_equity_pct": None,
                "confirmed_contribution_pct": 0.0,
                "possible_contribution_pct": round(residual, 4),
                "data_date": None,
                "source_url": report.source_url,
            }
        )
    confirmed = min(100.0, direct_us_pct + lookthrough_confirmed)
    possible = min(100.0, confirmed + unresolved)
    return (
        {
            "confirmed_pct": round(confirmed, 2),
            "possible_pct": round(possible, 2),
            "direct_us_pct": round(direct_us_pct, 2),
            "lookthrough_confirmed_pct": round(lookthrough_confirmed, 2),
            "unresolved_pct": round(unresolved, 2),
            "report_date": report.report_date.isoformat(),
            "published_date": report.published_date.isoformat(),
            "source_url": report.source_url,
            "components": components,
        },
        warnings,
    )


def apply_us_equity_threshold(
    exposure: dict[str, Any], threshold: float
) -> tuple[dict[str, Any], list[str]]:
    confirmed = float(exposure["confirmed_pct"])
    possible = float(exposure["possible_pct"])
    warnings: list[str] = []
    if confirmed >= threshold:
        status = "qualified"
    elif possible < threshold:
        status = "excluded"
    else:
        status = "ambiguous"
        warnings.append(
            f"美股占比区间 {confirmed:.2f}%-{possible:.2f}% 跨越 {threshold:g}% 阈值，"
            "按确认下限进入全球补充榜。"
        )
    return {**exposure, "status": status}, warnings


def calculate_us_equity_exposure(
    parsed: dict[str, Any],
    report: PeriodicReport,
    resolver: LookthroughResolver,
    threshold: float,
) -> tuple[dict[str, Any], list[str]]:
    exposure, warnings = calculate_us_equity_exposure_base(parsed, report, resolver)
    classified, threshold_warnings = apply_us_equity_threshold(exposure, threshold)
    return classified, [*warnings, *threshold_warnings]


def fetch_us_equity_exposure(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    report_cache: ReportTextCache,
    resolver: LookthroughResolver,
    threshold: float,
    exposure_cache: ExposureResultCache | None = None,
    report: PeriodicReport | None = None,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if exposure_cache is not None:
        return exposure_cache.get(
            client, fund, as_of, report_cache, resolver, threshold, report=report
        )
    if report is None:
        report = fetch_latest_periodic_report(
            client, fund["code"], as_of, snapshot=snapshot
        )
    text = report_cache.get_text(client, report, fund["fund_page_url"])
    parsed = parse_us_equity_report(text, fund["code"])
    return calculate_us_equity_exposure(parsed, report, resolver, threshold)
