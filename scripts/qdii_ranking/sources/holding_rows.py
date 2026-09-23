"""Conservative parsing of index-fund rows with explicit page continuations."""

import re
from typing import Any

from ..errors import DataError


REPORT_PAGE_RE = re.compile(
    r"(?m)^[^\n]*[0-9〇零○ＯO一二三四五六七八九]{4}[ \t]*年"
    r"[^\n]*(?:中期|半年度|年度|季度)报告[ \t\r]*\n"
    r"[ \t]*第\s*\d+\s*页\s*共\s*\d+\s*页[ \t\r]*(?:\n|$)"
)
INDEX_TYPE_RE = re.compile(
    r"(?P<name>.+?)\s+指\s*数\s*型\s+"
    r"(?:(?:交\s*易\s*型|契\s*约\s*型|普\s*通)\s*)?开\s*放\s*式\s+",
    re.S,
)
COMPLETE_NAME_RE = re.compile(r".+(?:证券投资基金|指数基金|联接基金)(?:[（(]QDII[）)])?$")
CONTINUED_NAME_RE = re.compile(r".+交易型开放式指数证券投资基金(?:[（(]QDII[）)])?$")
LEGAL_NAME_ENDING = "交易型开放式指数证券投资基金"


def parse_index_holding_row(raw: str, rank: int, code: str) -> dict[str, Any] | None:
    """A continuation after the amount belongs to the name only with page evidence."""
    pages = REPORT_PAGE_RE.split(raw)
    match = INDEX_TYPE_RE.match(pages[0].strip())
    if match is None:
        # An isolated index label is a recognized holding, not a placeholder.
        if re.search(r"(?m)^\s*指\s*数\s*型(?:\s|$)", raw):
            raise DataError(f"Could not parse index fund type/operation for fund {code} row {rank}")
        return None

    def fail(reason: str) -> None:
        raise DataError(f"Could not parse top fund investment row {rank} for fund {code}: {reason}")

    if len(pages) > 2:
        fail("ambiguous multiple page boundaries")
    first_page = pages[0].strip()
    # Only an amount immediately followed by its percentage ends this row.
    # Notes, section numbers and amounts on the following page are not weights.
    values = re.fullmatch(
        r"(?P<manager>.+?)\s+(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})"
        r"\s+(?P<weight>\d{1,3}\.\d{2}|-)\s*",
        first_page[match.end():], re.S,
    )
    if values is None:
        fail("missing amount/weight or unanchored continuation")
    if re.search(r"基金名称|基金类型|序号|注[:：]|第\s*\d+\s*页", values["manager"]):
        fail("unexpected header or note inside row")
    name = re.sub(r"\s+", "", match["name"])
    tail = re.sub(r"\s+", "", pages[1]) if len(pages) == 2 else ""
    if tail:
        if COMPLETE_NAME_RE.fullmatch(name):
            fail("continuation follows an already complete name")
        legal_tail = re.sub(r"[（(]QDII[）)]$", "", tail)
        if (
            not re.fullmatch(r"[\u4e00-\u9fffA-Z（）()]+", tail)
            or re.search(r"管理|公司|序号|基金名称|基金类型|附注|报告", tail)
            or legal_tail not in {LEGAL_NAME_ENDING[i:] for i in range(len(LEGAL_NAME_ENDING))}
            or not CONTINUED_NAME_RE.fullmatch(name + tail)
        ):
            fail("ambiguous name continuation")
        name += tail
    elif not COMPLETE_NAME_RE.fullmatch(name):
        # Names can be arbitrary when fully on one page; a page break requires
        # a demonstrably complete name so missing continuation cannot pass.
        if len(pages) > 1:
            fail("missing name continuation after page boundary")
    weight = 0.0 if values["weight"] == "-" else float(values["weight"])
    if not 0 <= weight <= 100:
        fail("invalid holding percentage")
    return {"rank": rank, "fund_name": name, "weight_pct": weight, "reported_category": None}
