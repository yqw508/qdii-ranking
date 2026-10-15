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
    # A bare page footer between amount rows must not become an extra share cell.
    text = re.sub(r"(?m)^[ \t]*\d{1,3}[ \t]*\r?\n(?=[ \t]*下[ \t]*属)", "", text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Some announcement PDFs extract every Chinese character and numeric
    # punctuation as separate text runs. Rejoin those runs before matching.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=\d)\s*([，,.])\s*(?=\d)", lambda m: m.group(1).replace("，", ","), text)
    return re.sub(r"[（(]单位\s*[：:]\s*人民\s*([\d.,\s-]+)\s*币元[）)]", r"（单位：人民币元）\1", text)


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
            r"(?:人民币\s*)?([\d][\d,.]*)\s*(?:人民币\s*)?(万元|元)",
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
        r"恢复\s*申购\s*日\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
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


def _share_table_events(text: str, published: date, source_url: str) -> list[dict[str, Any]] | None:
    """Keep every disclosed share in the document cache; select the fund later."""
    codes_match = re.search(r"交易代码\s*((?:\d{6}\s*)+)", text)
    if not codes_match:
        return None
    codes = re.findall(r"\d{6}", codes_match.group(1))
    tail = text[codes_match.end():]
    compact = re.sub(r"\s+", "", tail)
    amounts_match = re.search(r"限制申购金额(.*?)(?:限制定期|2\s*其他|注[：:]|$)", tail, re.S)
    restored_match = re.search(r"是否恢复上述业务([是否\-－—]+)", compact)
    if amounts_match:
        amount_text = amounts_match.group(1)
        row = re.match(r"\s*(?:[（(][^）)]{0,40}[）)]\s*)?"
                       r"((?:(?:[\d][\d,.]*|[-－—])\s*(?:元人民币|人民币元|美元|港币元|港元|元)?\s*)+)", amount_text)
        cells = re.findall(r"([\d][\d,.]*|[-－—])\s*(元人民币|人民币元|美元|港币元|港元|元)?", row.group(1)) if row else []
        amounts = [cell[0] for cell in cells]
        units_match = re.search(r"金额单位((?:(?:人民币元|美元|港币元|港元))+)", compact)
        units = [{"元人民币": "人民币元", "元": "人民币元"}.get(cell[1], cell[1]) for cell in cells]
        if units_match:
            units = re.findall(r"人民币元|美元|港币元|港元", units_match.group(1))
        elif not any(units) and re.search(r"单位\s*[：:]\s*(?:人民币\s*)?元", amount_text):
            units = ["人民币元"] * len(codes)
        header = text[:codes_match.start()].rsplit("基金简称", 1)[-1]
        header_units = re.findall(r"人民币|美元|港币", header)
        if len(header_units) == len(codes):
            units = [{"人民币": "人民币元", "美元": "美元", "港币": "港币元"}[u] for u in header_units]
        if len(amounts) != len(codes) or len(units) != len(codes):
            raise DataError("Share quota table amounts/currencies do not match its trading codes")
        if any(not unit and amount not in ("-", "－", "—") for amount, unit in zip(amounts, units)):
            raise DataError("Share quota table currency is unresolved")
        rows = [(code, "limited", float(amount.replace(',', '')), unit)
                for code, amount, unit in zip(codes, amounts, units) if amount not in ("-", "－", "—")]
    elif restored_match:
        flags = list(restored_match.group(1))
        if len(flags) != len(codes):
            raise DataError("Share restoration table does not match its trading codes")
        rows = [(code, "unchanged", None, None)
                for code, flag in zip(codes, flags) if flag == "是"]
    else:
        return None
    direct_only = bool(re.search(r"(?:调整|限制|暂停)直销(?:机构|渠道)", text[:180]))
    direct_override = extract_channel_amount(text, DIRECT_CHANNEL_PATTERN)
    agency_override = extract_channel_amount(text, AGENCY_CHANNEL_PATTERN)
    title_class = re.search(r"([ACDE])\s*类基金份额", text.split("公告", 1)[0])
    header = text[:codes_match.start()].rsplit("基金简称", 1)[-1]
    classes = [a or b for a, b in re.findall(r"[）)]\s*([ACDE])|(?:人民币|美元(?:现汇|现钞)?)\s*([ACDE])", header)]

    def scoped_override(code: str, pattern: str, amount: int | None) -> int | None:
        if title_class and len(classes) == len(codes) and classes[codes.index(code)] != title_class.group(1):
            return None
        match = re.search(pattern, text)
        if match:
            clause = re.split(r"[。；]", text[match.end():], maxsplit=1)[0]
            scope = re.findall(r"(?<!\d)\d{6}(?!\d)", clause)
            if scope and code not in scope:
                return None
        return amount

    return [{
        "effective_date": extract_effective_date(text, published),
        "direct_amount_cny": (int(amount) if direct_only else scoped_override(code, DIRECT_CHANNEL_PATTERN, direct_override)) if unit == "人民币元" else None,
        "agency_amount_cny": scoped_override(code, AGENCY_CHANNEL_PATTERN, agency_override) if unit == "人民币元" and not direct_only else None,
        "global_amount_cny": int(amount) if unit == "人民币元" and not direct_only else None,
        "global_status": status, "subscription_status": "open",
        "share_codes": [code],
        "currency": {"人民币元": "CNY", "美元": "USD", "港币元": "HKD", "港元": "HKD"}.get(unit),
        "source_amount": amount, "source_url": source_url,
        "published_date": published.isoformat(),
        "share_aggregation": detect_share_aggregation(text),
        "all_channels_combined": "全部销售机构累计" in re.sub(r"\s+", "", text),
        "confidence": "high",
    } for code, status, amount, unit in rows]


def parse_quota_notice(
    text: str, published: date, source_url: str
) -> list[dict[str, Any]]:
    normalized = normalize_notice_text(text)
    table_events = _share_table_events(normalized, published, source_url)
    if table_events is not None:
        return table_events
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
        elif (
            re.search(r"恢复(?:办理)?(?:人民币份额)?申购", compact)
            and re.search(r"恢复申购日\d{4}年\d{1,2}月\d{1,2}日|自\d{4}年\d{1,2}月\d{1,2}日起[^。]{0,80}恢复", compact)
            and not re.search(r"恢复[^。]{0,40}(?:时间|日期)[^。]{0,20}另行公告", compact)
        ):
            global_status = "unchanged"
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
        "subscription_status": "open" if global_status == "unchanged" else None,
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
    evidence: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    latest_unparsed_notice_date: date | None = None
    notices = fetch_announcements(client, fund["code"], as_of, snapshot=snapshot)
    for notice in notices:
        entry = {"announcement_id": str(notice["id"]), "title": notice["title"],
                 "published_date": notice["published"].isoformat(), "source_url": notice["url"]}
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
            selected = [item for item in parsed_transitions
                        if not item.get("share_codes") or fund["code"] in item["share_codes"]]
            if any(item.get("currency") not in (None, "CNY") for item in selected):
                raise DataError("Selected quota is not denominated in CNY")
            transitions.extend(selected)
            entry.update(status="parsed" if selected else "not_applicable", transitions=[
                {**item, "effective_date": item["effective_date"].isoformat()} for item in selected])
        except DataError as exc:
            entry.update(status="unresolved", error=str(exc))
            failures.append(entry)
            if (
                latest_unparsed_notice_date is None
                or notice["published"] > latest_unparsed_notice_date
            ):
                latest_unparsed_notice_date = notice["published"]
        evidence.append(entry)

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

    # A restoration of ordinary subscriptions does not remove a monetary ceiling.
    # Apply same-day general restoration before explicit caps, independent of index order.
    transitions.sort(key=lambda item: (item["effective_date"], item["published_date"],
                                       item.get("global_status", "limited") == "limited",
                                       item["source_url"]))
    current_subscription = None
    day_limits: dict[tuple, set[int]] = {}
    conflicting: set[str] = set()
    for transition in transitions:
        if transition["effective_date"] > as_of:
            continue
        if transition.get("share_aggregation"):
            aggregation = transition["share_aggregation"]
        if transition.get("all_channels_combined"):
            all_channels_combined = True
        if transition.get("subscription_status"):
            current_subscription = transition["subscription_status"]
            applied_sources.add(transition["source_url"])
        global_amount = transition.get("global_amount_cny")
        direct_amount = transition.get("direct_amount_cny")
        agency_amount = transition.get("agency_amount_cny")
        global_status = transition.get("global_status", "limited")
        for channel, amount in (("direct", direct_amount if direct_amount is not None else global_amount),
                                ("agency", agency_amount if agency_amount is not None else global_amount)):
            if amount is not None:
                key = (transition["effective_date"], transition["published_date"], channel)
                day_limits.setdefault(key, set()).add(amount)
                if len(day_limits[key]) > 1:
                    conflicting.add(channel)
            # A later fully dated transition can supersede an earlier conflict.
            if amount is not None or global_status == "unlimited":
                if not any(k[2] == channel and len(v) > 1 and k[:2] >=
                           (transition["effective_date"], transition["published_date"])
                           for k, v in day_limits.items()):
                    conflicting.discard(channel)
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

    for channel in conflicting:
        state = direct if channel == "direct" else agency
        state.update(new_limit_state())
        warnings.append(f"{fund['code']} quota notice could not be parsed: conflicting same-day {channel} ceilings")
    for failure in failures:
        superseded = all(state["status"] != "unknown" and state.get("source_url") and
                         any(item["source_url"] == state["source_url"] and
                             item["published_date"] > failure["published_date"] for item in transitions)
                         for state in (direct, agency))
        failure["status"] = "superseded" if superseded else "unresolved"
        detail = f"{failure['announcement_id']} {failure['title']} ({failure['source_url']}): {failure['error']}"
        warnings.append(f"{fund['code']} 历史额度公告解析失败，已被后续明确额度覆盖：{detail}" if superseded else
                        f"{fund['code']} quota notice could not be parsed: {detail}")
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
            "subscription_status": current_subscription,
            "notice_evidence": evidence,
        },
        warnings,
    )
