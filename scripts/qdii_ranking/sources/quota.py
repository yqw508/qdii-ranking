"""Quota notice parsing and effective direct/agency limit resolution."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..documents import extract_pdf_text
from ..errors import DataError
from ..models import FundAnnouncementSnapshot, LegalDocument
from ..runtime import HttpClient, parse_date
from .announcements import fetch_announcements

DIRECT_CHANNEL_PATTERN = (
    r"(?<!非)直销销售机构|(?<!非)直销机构|直销渠道|直销中心柜台|电子直销平台|网上直销平台"
)
AGENCY_CHANNEL_PATTERN = r"非直销销售机构|代销机构|代销渠道"


def normalize_notice_text(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Some announcement PDFs extract every Chinese character and numeric
    # punctuation as separate text runs. Rejoin those runs before matching.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"(?<=\d)\s*([,.])\s*(?=\d)", r"\1", text)


def parse_cny_amount(value: str, unit: str) -> int:
    number = float(value.replace(",", ""))
    if unit == "万元":
        number *= 10000
    return int(round(number))


def find_amounts(text: str) -> list[tuple[int, int, str]]:
    results: list[tuple[int, int, str]] = []
    for match in re.finditer(r"(?<![\d-])([\d][\d,.]*)\s*(万元|元)", text):
        try:
            results.append((match.start(), parse_cny_amount(match.group(1), match.group(2)), match.group(0)))
        except ValueError:
            continue
    return results


def extract_channel_amount(text: str, channel_pattern: str) -> int | None:
    best: tuple[int, int] | None = None
    for channel_match in re.finditer(channel_pattern, text):
        clause = re.split(r"[。；]", text[channel_match.end() :], maxsplit=1)[0]
        explicit = re.search(
            r"(?:不得超过|不超过|高于|超过|限额(?:调整)?为)\s*"
            r"([\d][\d,.]*)\s*(万元|元)",
            clause,
        )
        if explicit:
            return parse_cny_amount(explicit.group(1), explicit.group(2))
        above = re.search(r"([\d][\d,.]*)\s*(万元|元)\s*以上", clause)
        if above:
            return parse_cny_amount(above.group(1), above.group(2))
        start = max(0, channel_match.start() - 80)
        end = min(len(text), channel_match.end() + 220)
        window = text[start:end]
        for amount_pos, amount, _ in find_amounts(window):
            absolute_pos = start + amount_pos
            distance = abs(absolute_pos - channel_match.end())
            context_start = max(0, amount_pos - 55)
            context_end = min(len(window), amount_pos + 55)
            context = window[context_start:context_end]
            if not re.search(r"上限|超过|不得超过|不超过|限制|暂停办理|累计申购", context):
                continue
            candidate = (distance, amount)
            if best is None or candidate[0] < best[0]:
                best = candidate
    return best[1] if best else None


def extract_global_amount(text: str) -> int | None:
    patterns = [
        r"限制\s*(?:大额\s*)?申购\s*(?:及\s*定期定额投资\s*)?金额\s*"
        r"(?:[（(]\s*单\s*位\s*[：:]\s*(?:人民币\s*)?元\s*[）)])?\s*([\d,.]+)",
        r"调整\s*申购\s*(?:[（(]\s*含\s*定期定额投资\s*[）)])?\s*金额\s*"
        r"(?:[（(]\s*单\s*位\s*[：:]\s*人民币\s*元\s*[）)])?\s*([\d,.]+)",
        r"(?:累计申购|申购金额)[^。；]{0,180}?(?:不超过|不得超过|不应超过|上限调整为)\s*"
        r"(?:人民币\s*)?([\d,.]+)\s*(?:人民币\s*)?(万元|元)",
        r"超过\s*([\d,.]+)\s*(万元|元)[^。；]{0,50}?(?:申购|大额申购)",
        r"(?:金额累计限额|业务限额)为\s*([\d,.]+)\s*(?:人民币\s*)?(万元|元)",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        if index < 2:
            return int(round(float(match.group(1).replace(",", ""))))
        return parse_cny_amount(match.group(1), match.group(2))
    return None


def extract_effective_date(text: str, published: date) -> date:
    patterns = [
        r"(?:调整|暂停|恢复)[^。；]{0,30}?起始日\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:起|（含)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return published


def detect_share_aggregation(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text)
    if re.search(
        r"A[类级].{0,60}C[类级].{0,35}(?:合并计算|合计金额)|A类、C类.{0,30}合计金额",
        compact,
    ):
        return "A/C combined"
    if re.search(
        r"分别计算|分开计算|单独计算(?:限额)?|单一基金份额|单一类别|A类人民币份额或C类人民币份额",
        compact,
    ):
        return "A/C separate"
    return None


def extract_future_transitions(
    text: str, source_url: str, published: date
) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    date_pattern = re.compile(
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:起|（含[^）]*）)"
    )
    matches = list(date_pattern.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 500)
        clause = text[match.start():end]
        clause = re.split(r"[。；]", clause, maxsplit=1)[0]
        if not re.search(r"恢复|调整|暂停", clause):
            continue
        amount = extract_global_amount(clause)
        if amount is None:
            continue
        transitions.append(
            {
                "effective_date": date(int(match.group(1)), int(match.group(2)), int(match.group(3))),
                "direct_amount_cny": extract_channel_amount(
                    clause, DIRECT_CHANNEL_PATTERN
                ),
                "agency_amount_cny": extract_channel_amount(clause, AGENCY_CHANNEL_PATTERN),
                "global_amount_cny": amount,
                "source_url": source_url,
                "published_date": published.isoformat(),
                "share_aggregation": detect_share_aggregation(clause),
                "all_channels_combined": "全部销售机构累计" in re.sub(r"\s+", "", clause),
                "confidence": "high",
            }
        )
    return transitions


def parse_quota_notice(
    text: str, published: date, source_url: str
) -> list[dict[str, Any]]:
    normalized = normalize_notice_text(text)
    direct = extract_channel_amount(
        normalized, DIRECT_CHANNEL_PATTERN
    )
    agency = extract_channel_amount(normalized, AGENCY_CHANNEL_PATTERN)
    global_amount = extract_global_amount(normalized)
    compact = re.sub(r"\s+", "", normalized)
    if direct is None and agency is None and global_amount is None:
        future_restore = re.search(
            r"恢复(?:办理)?大额申购.{0,40}(?:具体时间|时间).{0,20}另行公告",
            compact,
        )
        if (
            re.search(r"恢复(?:办理)?大额申购", compact)
            and not re.search(r"暂停接受.*?超过", compact)
            and not future_restore
        ):
            global_status = "unlimited"
        else:
            return []
    else:
        global_status = "limited"
    base = {
        "effective_date": extract_effective_date(normalized, published),
        "direct_amount_cny": direct,
        "agency_amount_cny": agency,
        "global_amount_cny": global_amount,
        "global_status": global_status,
        "source_url": source_url,
        "published_date": published.isoformat(),
        "share_aggregation": detect_share_aggregation(normalized),
        "all_channels_combined": bool(
            "全部销售机构累计" in compact
            or re.search(r"多家销售渠道.{0,60}累计计算", compact)
        ),
        "confidence": "high" if global_amount is not None or (direct is not None and agency is not None) else "medium",
    }
    transitions = [base]
    for transition in extract_future_transitions(normalized, source_url, published):
        if transition["effective_date"] != base["effective_date"]:
            transitions.append(transition)
    return transitions


def new_limit_state(status: str = "unknown", amount: int | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "amount_cny": amount,
        "effective_date": None,
        "source_url": None,
        "confidence": "low" if status == "unknown" else "medium",
    }


def apply_limit(
    state: dict[str, Any], status: str, amount: int | None, transition: dict[str, Any]
) -> None:
    state.update(
        {
            "status": status,
            "amount_cny": amount,
            "effective_date": transition["effective_date"].isoformat(),
            "source_url": transition["source_url"],
            "confidence": transition.get("confidence", "medium"),
        }
    )


def resolve_quota(
    client: HttpClient,
    fund: dict[str, Any],
    as_of: date,
    document_cache: PeriodicReportCache | None = None,
    snapshot: FundAnnouncementSnapshot | None = None,
    notice_cache: QuotaNoticeParseCache | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if fund["purchase_status"] == "open" and fund.get("page_agency_limit_cny") is None:
        direct = new_limit_state("unlimited")
        agency = new_limit_state("unlimited")
    else:
        direct = new_limit_state()
        agency = new_limit_state(
            "limited" if fund.get("page_agency_limit_cny") else "unknown",
            fund.get("page_agency_limit_cny"),
        )
        if agency["status"] == "limited":
            agency["source_url"] = fund["fund_page_url"]
            agency["confidence"] = "medium"

    aggregation = "not applicable"
    all_channels_combined = False
    applied_sources: set[str] = set()
    transitions: list[dict[str, Any]] = []
    latest_unparsed_notice_date: date | None = None
    notices = fetch_announcements(client, fund["code"], as_of, snapshot=snapshot)
    for notice in notices:
        try:
            if notice_cache is not None:
                if document_cache is None:
                    raise DataError("Quota notice cache requires the PDF document cache")
                parsed_transitions = notice_cache.get(
                    client, fund, notice, document_cache
                )
            elif document_cache is None:
                pdf = client.get_bytes(notice["url"], referer=fund["fund_page_url"])
                text = extract_pdf_text(pdf)
                parsed_transitions = parse_quota_notice(
                    text, notice["published"], notice["url"]
                )
            else:
                document = LegalDocument(
                    str(notice["id"]),
                    str(notice["title"]),
                    notice["published"],
                    str(notice["url"]),
                    "quota_notice",
                )
                text = document_cache.get_text(
                    client, document, fund["fund_page_url"]
                )
                parsed_transitions = parse_quota_notice(
                    text, notice["published"], notice["url"]
                )
            if not parsed_transitions:
                raise DataError("quota notice produced no effective limit transition")
            transitions.extend(parsed_transitions)
        except DataError as exc:
            warnings.append(f"{fund['code']} quota notice could not be parsed: {exc}")
            if (
                latest_unparsed_notice_date is None
                or notice["published"] > latest_unparsed_notice_date
            ):
                latest_unparsed_notice_date = notice["published"]

    if latest_unparsed_notice_date is not None:
        # A newer unreadable limit notice invalidates page state and older
        # transitions. A later readable notice can establish a fresh state.
        direct = new_limit_state()
        agency = new_limit_state()
        transitions = [
            item
            for item in transitions
            if parse_date(str(item["published_date"])) > latest_unparsed_notice_date
        ]

    transitions.sort(key=lambda item: (item["effective_date"], item["published_date"]))
    for transition in transitions:
        if transition["effective_date"] > as_of:
            continue
        if transition.get("share_aggregation"):
            aggregation = transition["share_aggregation"]
        if transition.get("all_channels_combined"):
            all_channels_combined = True
        global_amount = transition.get("global_amount_cny")
        direct_amount = transition.get("direct_amount_cny")
        agency_amount = transition.get("agency_amount_cny")
        global_status = transition.get("global_status", "limited")
        if direct_amount is not None:
            apply_limit(direct, "limited", direct_amount, transition)
            applied_sources.add(transition["source_url"])
        if agency_amount is not None:
            apply_limit(agency, "limited", agency_amount, transition)
            applied_sources.add(transition["source_url"])
        if direct_amount is None and agency_amount is None and global_status == "unlimited":
            apply_limit(direct, "unlimited", None, transition)
            apply_limit(agency, "unlimited", None, transition)
            applied_sources.add(transition["source_url"])
        elif global_amount is not None:
            if direct_amount is None:
                apply_limit(direct, "limited", global_amount, transition)
            if agency_amount is None:
                apply_limit(agency, "limited", global_amount, transition)
            applied_sources.add(transition["source_url"])

    if direct["status"] == "unknown" or agency["status"] == "unknown":
        warnings.append(
            f"{fund['code']} 的申购额度无法确定；引用前请核对关联公告。"
        )
    statuses = {direct["status"], agency["status"]}
    if "unknown" in statuses:
        quota_status = "unknown"
        confidence = "low"
    elif "limited" in statuses:
        quota_status = "limited"
        confidence = "high" if all(item["confidence"] == "high" for item in (direct, agency)) else "medium"
    else:
        quota_status = "unlimited"
        confidence = "medium"
    if all_channels_combined:
        channel_rule = "all sales channels combined"
    elif direct["amount_cny"] != agency["amount_cny"]:
        channel_rule = "direct and agency limits differ"
    else:
        channel_rule = "same fund-level limit"
    return (
        {
            "quota_status": quota_status,
            "quota_confidence": confidence,
            "direct_limit": direct,
            "agency_limit": agency,
            "share_class_rule": aggregation,
            "channel_rule": channel_rule,
            "quota_source_urls": sorted(applied_sources),
        },
        warnings,
    )
