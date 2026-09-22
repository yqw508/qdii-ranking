"""Product-overview evidence for unlabeled primary shares in the OTC display list."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

from ..config import EXCLUDED_FUND_TYPES
from ..errors import DataError
from ..models import LegalDocument
from .candidates import is_nasdaq100_otc_name


EVIDENCE_FIELDS = (
    "method", "code", "name", "summary_short_name", "currency", "share_class",
    "operation_mode", "announcement_id", "published_date", "source_url",
)
EVIDENCE_LABEL = "人民币主份额，经产品概要确认"


def compact_share_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).upper()


def normalized_share_name(value: str) -> str:
    return compact_share_text(value).replace("(QDII)", "")


def needs_primary_share_verification(meta: dict[str, Any]) -> bool:
    """Only unlabeled names may enter the additional verification path."""
    name = compact_share_text(meta["name"])
    fund_type = meta["fund_type"]
    if not (
        (fund_type.startswith("QDII") or fund_type == "指数型-海外股票")
        and fund_type not in EXCLUDED_FUND_TYPES
        and is_nasdaq100_otc_name(meta["name"])
    ):
        return False
    if re.search(r"美元|美金|港币|港元|USD|HKD|后端|人民币|RMB", name):
        return False
    if "ETF" in name and "联接" not in name and "LOF" not in name:
        return False
    # Structure acronyms and English words are not share-class markers.
    without_structure = re.sub(r"\((?:QDII(?:-FOF)?|LOF|ETF)\)", "", name)
    if re.search(r"(?<![A-Z])[A-Z](?:类|份额|[12]?(?:$|[(/)]))", without_structure):
        return False
    return True


def parse_primary_share_evidence(
    text: str, meta: dict[str, Any], summary: LegalDocument, as_of: date
) -> dict[str, str]:
    """Fail closed on missing, contradictory or multiple share identities."""
    if not needs_primary_share_verification(meta):
        raise DataError("名称或类型不符合未标记主份额核实范围")
    if summary.document_type != "product_summary" or summary.published_date > as_of:
        raise DataError("产品概要类型或发布日期无效（不得使用未来公告）")
    compact = compact_share_text(text)
    sections = re.findall(r"一[、.]产品概况(.*?)(?=二[、.])", compact)
    if len(sections) != 1:
        raise DataError("无法唯一定位产品概况")
    overview = sections[0]
    if re.search(r"下属|份额类别|各类基金份额|[A-Z]类", overview):
        raise DataError("产品概况存在多份额或非主份额映射")
    if set(re.findall(r"(?<!\d)\d{6}(?!\d)", overview)) != {meta["code"]}:
        raise DataError("产品概况基金代码不匹配或存在多代码歧义")
    labels = (
        "基金简称", "基金代码", "基金管理人", "基金托管人", "境外投资顾问",
        "境外托管人", "基金合同生效日", "基金类型", "交易币种", "运作方式",
        "开放频率", "基金经理", "其他",
    )
    boundary = "|".join(labels)

    def field(label: str) -> str:
        values = re.findall(rf"{label}[:：]?(.*?)(?={boundary}|$)", overview)
        if len(values) != 1 or not values[0]:
            raise DataError(f"产品概况{label}缺失或重复")
        return values[0]

    code = field("基金代码")
    short_name = field("基金简称")
    if code != meta["code"] or normalized_share_name(short_name) != normalized_share_name(meta["name"]):
        raise DataError("产品概况代码/简称与候选身份不一致")
    if not needs_primary_share_verification({**meta, "name": short_name}):
        raise DataError("产品概况简称不是未标记主份额")
    if field("交易币种") != "人民币":
        raise DataError("产品概况交易币种未明确且唯一为人民币")
    if field("运作方式") != "普通开放式":
        raise DataError("产品概况运作方式未明确为普通开放式")
    return {
        "method": "product_summary", "code": code, "name": meta["name"],
        "summary_short_name": short_name, "currency": "CNY",
        "share_class": "unlabeled_primary", "operation_mode": "普通开放式",
        "announcement_id": summary.announcement_id,
        "published_date": summary.published_date.isoformat(),
        "source_url": summary.source_url,
    }
