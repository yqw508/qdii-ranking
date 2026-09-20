"""Legal-document discovery, contract benchmark parsing, and holding costs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from ..errors import DataError
from ..models import AnnouncementRecord, FundAnnouncementSnapshot, LegalDocument
from ..runtime import HttpClient
from .announcements import (
    _announcement_page,
    _is_rmb_product_summary,
    _parse_announcement_page,
)


class DocumentTextCache(Protocol):
    def get_text(
        self, client: HttpClient, document: LegalDocument, referer: str
    ) -> str: ...


def normalize_benchmark_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", value.upper())


class ContractBenchmarkCatalog:
    def __init__(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not load contract benchmark catalog: {exc}") from exc
        if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
            raise DataError("Unsupported contract benchmark catalog schema")
        self.path = path
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        self.entries: list[dict[str, Any]] = []
        for entry in payload["entries"]:
            aliases = entry.get("aliases") or []
            required = {
                "id",
                "display_name",
                "market_scope",
                "market_label",
                "asset_class",
                "style_label",
                "structure",
                "excluded_target",
            }
            if not required.issubset(entry) or not aliases:
                raise DataError(f"Invalid contract benchmark catalog entry: {entry!r}")
            normalized_aliases = sorted(
                {normalize_benchmark_name(str(alias)) for alias in aliases if str(alias).strip()},
                key=len,
                reverse=True,
            )
            if not normalized_aliases:
                raise DataError(f"Contract benchmark {entry['id']} has no usable aliases")
            self.entries.append({**entry, "normalized_aliases": normalized_aliases})

    def match(self, value: str) -> list[dict[str, Any]]:
        normalized = normalize_benchmark_name(value)
        matches: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            if any(alias in normalized for alias in entry["normalized_aliases"]):
                matches[str(entry["id"])] = entry
        return list(matches.values())


def fetch_latest_legal_documents(
    client: HttpClient,
    code: str,
    as_of: date,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[LegalDocument | None, LegalDocument | None]:
    prospectuses: list[LegalDocument] = []
    summaries: list[LegalDocument] = []
    if snapshot is not None:
        if snapshot.code != code or snapshot.as_of != as_of:
            raise DataError("Announcement snapshot identity does not match legal-document request")
        page_items: list[list[AnnouncementRecord]] = [list(snapshot.items)]
    else:
        page_items = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        if snapshot is not None:
            records = page_items[0]
            total_pages = 1
        else:
            payload = _announcement_page(client, code, page)
            if page == 1:
                total_count = int(payload.get("TotalCount") or 0)
                page_size = int(payload.get("PageSize") or 100)
                total_pages = max(1, math.ceil(total_count / max(page_size, 1)))
            records = _parse_announcement_page(payload, code)
        for record in records:
            title = record.title
            published = record.published_date
            if published > as_of:
                continue
            announcement_id = record.announcement_id
            if not announcement_id:
                continue
            source_url = record.source_url
            if (
                "招募说明书" in title
                and "提示性公告" not in title
                and "摘要" not in title
            ):
                prospectuses.append(
                    LegalDocument(
                        announcement_id,
                        title,
                        published,
                        source_url,
                        "prospectus",
                    )
                )
            if "基金产品资料概要" in title and _is_rmb_product_summary(title):
                summaries.append(
                    LegalDocument(
                        announcement_id,
                        title,
                        published,
                        source_url,
                        "product_summary",
                    )
                )
        if prospectuses and summaries:
            break
        if not records:
            break
        page += 1
    prospectus = (
        max(prospectuses, key=lambda item: (item.published_date, item.announcement_id))
        if prospectuses
        else None
    )
    summary = (
        max(summaries, key=lambda item: (item.published_date, item.announcement_id))
        if summaries
        else None
    )
    return prospectus, summary


def extract_contract_benchmark_statement(text: str) -> str:
    compact = re.sub(
        r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])",
        "",
        text.replace("\u3000", " "),
    )
    compact = re.sub(r"\s+", " ", compact).strip()
    starts = list(
        re.finditer(
            r"(?:本基金(?:选择)?的?)?业绩比较基准(?:为|是|采用|：|:)\s*",
            compact,
        )
    )
    starts.extend(
        re.finditer(
            r"业绩比较基准\s*(?=(?:\d|经|标|纳|MSCI|摩根|彭博|伦敦|富时|恒生|中证|人民币|美元|[A-Z]))",
            compact,
            re.I,
        )
    )
    stop_re = re.compile(
        r"风险收益特征|业绩比较基准的选择理由|如果今后|若今后|在法律法规|"
        r"基金管理人可|本基金为|本基金选择|本基金设置|本基金采取"
    )
    candidates: list[str] = []
    for match in starts:
        tail = compact[match.end() : match.end() + 1200]
        stop = stop_re.search(tail)
        statement = tail[: stop.start() if stop else 800].strip(" ：:。；;")
        sentence_end = re.search(r"[。；;]", statement)
        if sentence_end:
            statement = statement[: sentence_end.start()].strip()
        normalized = normalize_benchmark_name(statement)
        if statement and any(
            token in normalized
            for token in ("指数", "价格", "利率", "INDEX", "PRICE", "VIX")
        ):
            candidates.append(statement)
    if not candidates:
        raise DataError("Could not locate the current performance benchmark statement")
    return min(candidates, key=lambda value: (0 if "%" in value else 1, len(value)))


def _benchmark_weight(statement: str, entry: dict[str, Any]) -> float:
    compact = re.sub(r"\s+", " ", statement)
    for alias in sorted(entry.get("aliases") or [], key=lambda value: len(str(value)), reverse=True):
        parts = [re.escape(part) for part in re.split(r"\s+", str(alias).strip()) if part]
        if not parts:
            continue
        alias_pattern = r"\s*".join(parts)
        for match in re.finditer(alias_pattern, compact, re.I):
            before = compact[max(0, match.start() - 45) : match.start()]
            after = compact[match.end() : match.end() + 90]
            before_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*[×*xX]\s*$", before)
            if before_match:
                return float(before_match.group(1))
            after_match = re.search(
                r"^[^+＋，,。；;]{0,55}?[×*xX]\s*(\d+(?:\.\d+)?)\s*%",
                after,
            )
            if after_match:
                return float(after_match.group(1))
    percentages = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", compact)]
    if percentages:
        return max(percentages)
    return 100.0


def detect_product_structure(text: str, catalog_value: str) -> str:
    normalized = re.sub(r"\s+", "", text)
    if re.search(r"反向|做空|Inverse|Short", normalized, re.I):
        return "inverse"
    if re.search(
        r"(?:[2-9]|两|三)倍(?:做多|多头|杠杆)|杠杆指数|Leveraged|Ultra",
        normalized,
        re.I,
    ):
        return "leveraged"
    if re.search(r"波动率|VIX|Volatility", normalized, re.I):
        return "volatility"
    return catalog_value


def parse_contract_benchmark(
    text: str,
    fund: dict[str, Any],
    catalog: ContractBenchmarkCatalog,
) -> dict[str, Any]:
    statement = extract_contract_benchmark_statement(text)
    matches = catalog.match(statement)
    if not matches and "标的指数" in statement:
        matches = catalog.match(f"{fund['name']} {text[:3000]}")
    components = [
        {
            "benchmark_id": entry["id"],
            "benchmark_name": entry["display_name"],
            "weight_pct": round(_benchmark_weight(statement, entry), 2),
            "market_scope": entry["market_scope"],
            "market_label": entry["market_label"],
            "asset_class": entry["asset_class"],
            "style_label": entry["style_label"],
            "structure": entry["structure"],
            "excluded_target": bool(entry["excluded_target"]),
        }
        for entry in matches
    ]
    components.sort(key=lambda item: (-float(item["weight_pct"]), item["benchmark_id"]))
    single = components[0] if len(components) == 1 else None
    status = "recognized" if single else "composite" if components else "unrecognized"
    return {
        "status": status,
        "benchmark_text": statement,
        "benchmark_id": single["benchmark_id"] if single else None,
        "benchmark_name": (
            single["benchmark_name"]
            if single
            else " + ".join(item["benchmark_name"] for item in components)
            if components
            else "未识别"
        ),
        "benchmark_weight_pct": single["weight_pct"] if single else None,
        "market_scope": single["market_scope"] if single else "composite" if components else "unknown",
        "market_label": single["market_label"] if single else "复合市场" if components else "未识别",
        "asset_class": single["asset_class"] if single else "mixed" if components else "unknown",
        "style_label": single["style_label"] if single else "复合风格" if components else "未识别",
        "structure": detect_product_structure(
            f"{fund['name']} {statement}",
            str(single["structure"]) if single else "standard",
        ),
        "excluded_target": bool(components) and all(
            bool(item["excluded_target"]) for item in components
        ),
        "components": components,
    }


def unavailable_holding_cost(
    summary: LegalDocument | None, status: str = "unavailable"
) -> dict[str, Any]:
    return {
        "status": status,
        "annualized_pct": None,
        "measurement_date": None,
        "source_title": summary.title if summary else None,
        "source_published_date": summary.published_date.isoformat() if summary else None,
        "source_url": summary.source_url if summary else None,
    }


def unreadable_contract_benchmark(fund: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "unreadable",
        "benchmark_text": "未识别",
        "benchmark_id": None,
        "benchmark_name": "未识别",
        "benchmark_weight_pct": None,
        "market_scope": "unknown",
        "market_label": "未识别",
        "asset_class": "unknown",
        "style_label": "未识别",
        "structure": detect_product_structure(fund["name"], "standard"),
        "excluded_target": False,
        "components": [],
    }


def parse_holding_cost(
    text: str, summary: LegalDocument, as_of: date
) -> dict[str, Any]:
    if summary.published_date > as_of:
        raise DataError("Holding-cost source publication date is in the future")
    compact = re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()
    rate_match = re.search(
        r"基金运作综合费率\s*[（(]\s*年化\s*[）)]"
        r"(?:\s*(?:基金运作综合费率|\d+\s*/\s*\d+|[-–—])){0,3}"
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        compact,
    )
    if not rate_match:
        raise DataError("Could not locate annualized comprehensive operating expense")
    rate = float(rate_match.group(1))
    if not math.isfinite(rate) or rate < 0 or rate > 100:
        raise DataError("Annualized comprehensive operating expense is outside its valid range")
    date_match = re.search(
        r"(?:综合费率[^。]{0,80})?测算日期为\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        compact,
    )
    measurement_date = None
    if date_match:
        measured = date(*(int(value) for value in date_match.groups()))
        if measured > as_of:
            raise DataError("Holding-cost measurement date is in the future")
        measurement_date = measured.isoformat()
    return {
        "status": "parsed",
        "annualized_pct": round(rate, 2),
        "measurement_date": measurement_date,
        "source_title": summary.title,
        "source_published_date": summary.published_date.isoformat(),
        "source_url": summary.source_url,
    }


def resolve_contract_benchmark(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    document_cache: DocumentTextCache,
    catalog: ContractBenchmarkCatalog,
    snapshot: FundAnnouncementSnapshot | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    code = fund["code"]
    warnings: list[str] = []
    management_style = (
        "passive"
        if fund["fund_type"] == "指数型-海外股票" and "增强" not in fund["name"]
        else "active"
    )
    try:
        prospectus, summary = fetch_latest_legal_documents(
            client, code, as_of, snapshot=snapshot
        )
    except DataError as exc:
        profile = unreadable_contract_benchmark(fund)
        profile.update(
            {
                "management_style": management_style,
                "prospectus_title": None,
                "prospectus_published_date": None,
                "source_url": None,
                "product_summary_status": "unreadable",
                "product_summary_published_date": None,
                "product_summary_source_url": None,
                "catalog_fingerprint": catalog.fingerprint,
            }
        )
        warnings.extend(
            (
                f"合同基准告警 {code}：法律文件目录无法读取：{exc}",
                f"持有费率告警 {code}：法律文件目录无法读取：{exc}",
            )
        )
        return profile, unavailable_holding_cost(None), warnings

    if prospectus is not None:
        try:
            prospectus_text = document_cache.get_text(
                client, prospectus, fund["fund_page_url"]
            )
            profile = parse_contract_benchmark(prospectus_text, fund, catalog)
        except DataError as exc:
            profile = unreadable_contract_benchmark(fund)
            warnings.append(f"合同基准告警 {code}：{exc}")
    else:
        profile = unreadable_contract_benchmark(fund)
        warnings.append(f"合同基准告警 {code}：截至 {as_of} 没有可用的招募说明书。")

    summary_status = "missing"
    summary_url = summary.source_url if summary else None
    summary_published_date = summary.published_date.isoformat() if summary else None
    holding_cost = unavailable_holding_cost(summary)
    if summary is not None:
        try:
            summary_text = document_cache.get_text(
                client, summary, fund["fund_page_url"]
            )
        except DataError as exc:
            summary_status = "unreadable"
            warnings.append(f"产品概要告警 {code}：{exc}")
            warnings.append(f"持有费率告警 {code}：{exc}")
        else:
            try:
                holding_cost = parse_holding_cost(summary_text, summary, as_of)
            except DataError as exc:
                warnings.append(f"持有费率告警 {code}：{exc}")
            try:
                summary_profile = parse_contract_benchmark(summary_text, fund, catalog)
                prospectus_ids = {
                    item["benchmark_id"] for item in profile["components"]
                }
                summary_ids = {
                    item["benchmark_id"] for item in summary_profile["components"]
                }
                if profile["status"] in {"unreadable", "unrecognized"}:
                    summary_status = "unreadable"
                elif prospectus_ids != summary_ids:
                    summary_status = "conflict"
                    warnings.append(
                        f"产品概要告警 {code}：招募说明书与人民币产品概要的合同基准不一致。"
                    )
                else:
                    summary_status = "matched"
            except DataError as exc:
                summary_status = "unreadable"
                warnings.append(f"产品概要告警 {code}：{exc}")
    else:
        warnings.append(f"持有费率告警 {code}：截至 {as_of} 没有可用的人民币产品概要。")

    profile.update(
        {
            "management_style": management_style,
            "prospectus_title": prospectus.title if prospectus else None,
            "prospectus_published_date": (
                prospectus.published_date.isoformat() if prospectus else None
            ),
            "source_url": prospectus.source_url if prospectus else None,
            "product_summary_status": summary_status,
            "product_summary_published_date": summary_published_date,
            "product_summary_source_url": summary_url,
            "catalog_fingerprint": catalog.fingerprint,
        }
    )
    return profile, holding_cost, warnings
