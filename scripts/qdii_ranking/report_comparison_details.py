"""Render curated, two-report comparisons without refreshing ranking data."""

from __future__ import annotations

import html
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .report_comparisons import ReportComparisonError, validate_report_comparison_index


def number(value: Any) -> Decimal:
    """Keep disclosed decimal precision; reject non-finite or malformed numbers."""
    if not isinstance(value, str):
        raise ReportComparisonError("Comparison numbers must be decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ReportComparisonError(f"Invalid comparison number: {value!r}") from exc
    if not result.is_finite():
        raise ReportComparisonError("Non-finite comparison number")
    return result


def comparison_entry(data: dict, code: str) -> dict:
    fund = data["funds"][code]
    return {
        "id": data["comparison_id"], "title": data["title"],
        "generated_at": data["generated_at"], "summary": " ".join(fund["insights"]),
        "periods": [{key: p[key] for key in ("label", "report_date", "published_date", "source_url")}
                    for p in fund["periods"]],
        "detail_url": f"report-comparisons/{code}/{data['comparison_id']}.html",
    }


def return_windows(period: dict) -> dict:
    return period.get("primary_return_windows", period.get("a_return_windows", {}))


def validate_comparison_details(data: dict) -> dict:
    """Validate paired evidence before it can create a public comparison entry."""
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ReportComparisonError(message)

    require(data.get("schema_version") == 1, "Unexpected comparison detail schema")
    require(isinstance(data.get("funds"), dict) and bool(data["funds"]), "No comparison details")
    try:
        entries = {}
        for code, fund in data["funds"].items():
            require(all(isinstance(x, str) and x.strip() for x in fund["insights"]), "Empty insight")
            require(len(fund["insights"]) >= 1, "Missing comparison conclusions")
            require(all(isinstance(x, str) for x in fund["notes"]), "Invalid comparison note")
            require(len(fund["periods"]) == 2, "A comparison needs two reports")
            require(fund["periods"][0]["report_date"] < fund["periods"][1]["report_date"], "Reports are out of order")
            primary_label = fund.get("primary_share_label", "A")
            require(primary_label in ("A", "人民币"), "Unsupported primary share label")
            active_flags = ["active_portfolio" in p for p in fund["periods"]]
            require(active_flags[0] == active_flags[1], "Active sleeve requires both reports")
            fund_holding_flags = ["fund_holdings" in p for p in fund["periods"]]
            require(fund_holding_flags[0] == fund_holding_flags[1], "Underlying-fund sleeve requires both reports")
            if any(fund_holding_flags):
                require(all(isinstance(fund.get(k), str) and fund[k].strip() for k in (
                    "fund_holdings_title", "fund_holdings_section", "fund_holdings_scope_note")),
                        "Underlying-fund sleeve scope is required")
            if any(active_flags):
                require(all(isinstance(fund.get(k), str) and fund[k].strip() for k in (
                    "sectors_title", "sectors_section", "holdings_title", "holdings_section",
                    "holdings_scope_note", "holding_changes_title")), "Separate sleeve scope is required")
            class_labels = []
            for period in fund["periods"]:
                require(bool(re.fullmatch(r"[0-9a-f]{64}", period["pdf_sha256"])), "Missing PDF digest")
                require(all(type(period["pages"][key]) is int and period["pages"][key] > 0
                            for key in ("financial", "performance", "assets", "regions", "sectors", "holdings", "strategy")), "Missing PDF locator")
                if "fund_holdings" in period:
                    require(type(period["pages"].get("fund_holdings")) is int and period["pages"]["fund_holdings"] > 0,
                            "Missing underlying-fund PDF locator")
                classes = period["classes"]
                require(bool(classes) and classes[0]["label"] == primary_label, "Missing primary share class")
                labels = [c["label"] for c in classes]
                require(len(labels) == len(set(labels)), "Duplicate share class")
                class_labels.append(labels)
                for c in classes:
                    require(number(c["nav"]) > 0 and number(c["net_assets_cny"]) > 0, "Invalid NAV or net assets")
                    require(number(c["return_pct"]) >= -100, "Invalid return")
                number(period["benchmark_pct"])
                require(not ("primary_return_windows" in period and "a_return_windows" in period), "Ambiguous return windows")
                windows = return_windows(period)
                require(number(windows["过去三个月"]["return_pct"]) == number(classes[0]["return_pct"]), "Quarter return mismatch")
                require(number(windows["过去三个月"]["benchmark_pct"]) == number(period["benchmark_pct"]), "Quarter benchmark mismatch")
                for window in windows.values():
                    number(window["return_pct"])
                    number(window["benchmark_pct"])
                for field in ("assets", "regions", "sectors"):
                    require(bool(period[field]), f"Missing {field}")
                    for value in period[field].values():
                        if value is not None:
                            require(0 <= number(value) <= 100, f"Invalid {field} percentage")
                total = sum((number(v) for v in period["assets"].values() if v is not None), Decimal(0))
                require(abs(total - 100) <= Decimal('0.10'), "Asset allocation does not account for total assets")
                holdings = period["holdings"]
                require([h["rank"] for h in holdings] == list(range(1, 11)), "Expected exactly ten disclosed ranks")
                require(len({h["issuer_id"] for h in holdings}) == 10, "Duplicate holding identity")
                for holding in holdings:
                    require(isinstance(holding["issuer_id"], str) and bool(holding["issuer_id"]), "Missing holding identity")
                    require(isinstance(holding["name"], str) and bool(holding["name"]), "Missing holding name")
                    require(0 < number(holding["weight_pct"]) <= 100, "Invalid holding weight")
                require(sum(number(h["weight_pct"]) for h in holdings) <= 100, "Invalid top-ten concentration")
                if "fund_holdings" in period:
                    fund_holdings = period["fund_holdings"]
                    require([h["rank"] for h in fund_holdings] == list(range(1, 11)),
                            "Expected exactly ten disclosed underlying-fund ranks")
                    require(len({h["issuer_id"] for h in fund_holdings}) == 10,
                            "Duplicate underlying-fund identity")
                    for holding in fund_holdings:
                        require(all(isinstance(holding.get(k), str) and holding[k].strip()
                                    for k in ("issuer_id", "name", "weight_pct")),
                                "Missing underlying-fund identity")
                        require(0 < number(holding["weight_pct"]) <= 100,
                                "Invalid underlying-fund weight")
                        analogy = holding.get("domestic_analogue")
                        require(isinstance(analogy, dict), "Missing domestic fund analogue")
                        require(isinstance(analogy.get("benchmark"), str) and analogy["benchmark"].strip(),
                                "Missing domestic analogue benchmark")
                        require(isinstance(analogy.get("examples"), list), "Invalid domestic analogue examples")
                        for example in analogy["examples"]:
                            require(isinstance(example, dict), "Invalid domestic analogue example")
                            require(all(isinstance(example.get(k), str) and example[k].strip()
                                        for k in ("code", "name", "exchange")),
                                    "Incomplete domestic analogue example")
                        require(isinstance(analogy.get("note"), str) and analogy["note"].strip(),
                                "Missing domestic analogue note")
                    require(sum(number(h["weight_pct"]) for h in fund_holdings) <= 100,
                            "Invalid underlying-fund top-ten concentration")
                if "active_portfolio" in period:
                    active = period["active_portfolio"]
                    require(all(type(period["pages"].get(k)) is int and period["pages"][k] > 0
                                for k in ("active_sectors", "active_holdings")), "Missing active sleeve PDF locator")
                    top5 = active["holdings"]
                    require([h["rank"] for h in top5] == list(range(1, 6)), "Expected five active investment ranks")
                    require(len({h["issuer_id"] for h in top5}) == 5, "Duplicate active holding identity")
                    for holding in top5:
                        require(all(isinstance(holding.get(k), str) and holding[k] for k in ("name", "issuer_id")), "Missing active holding identity")
                        require(0 < number(holding["weight_pct"]) <= 100, "Invalid active holding weight")
                    for sleeve in (period, active):
                        require(bool(sleeve["sectors"]), "Missing sleeve industry evidence")
                        weights = [number(v) for v in sleeve["sectors"].values() if v is not None]
                        require(all(0 <= w <= 100 for w in weights), "Invalid sleeve industry weight")
                        total = number(sleeve["sectors_total_pct"])
                        require(0 <= total <= 100 and abs(sum(weights) - total) <= Decimal('0.10'), "Sleeve industry total mismatch")
                    require(sum(number(h["weight_pct"]) for h in top5) <= number(active["sectors_total_pct"]), "Active holdings exceed active sleeve")
                    require(sum(number(h["weight_pct"]) for h in holdings) <= number(period["sectors_total_pct"]), "Index holdings exceed index sleeve")
                    equity_total = sum(number(v) for v in period["regions"].values())
                    require(abs(number(period["sectors_total_pct"]) + number(active["sectors_total_pct"]) - equity_total) <= Decimal('0.03'), "Sleeves disagree with equity total")
                require(bool(period["strategy"]) and all(isinstance(p, str) and p.strip() for p in period["strategy"]), "Missing report discussion")
            require(class_labels[0] == class_labels[1], "Share classes need explicit reconciliation")
            entries[code] = {"name": fund["name"], "list_url": f"report-comparisons/{code}/",
                             "comparisons": [comparison_entry(data, code)]}
        validate_report_comparison_index({"schema_version": 1, "funds": entries})
    except (KeyError, TypeError, AttributeError) as exc:
        raise ReportComparisonError(f"Incomplete comparison evidence: {exc}") from exc
    return data


def load_comparison_details(path: Path) -> dict:
    return validate_comparison_details(json.loads(path.read_text(encoding="utf-8")))


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def percent(value: str | None, missing: str = "—") -> str:
    return missing if value is None else f"{number(value):.2f}%"


def delta(a: str | None, b: str | None) -> str:
    return "不可比" if a is None or b is None else f"{number(b) - number(a):+.2f} 个百分点"


def table(headers: list[str], rows: list[list[str]], *, table_id: str = "") -> str:
    identity = f' id="{esc(table_id)}"' if table_id else ""
    return (f'<p class="table-hint">表格可左右滑动查看全部列</p><div class="table-wrap"><table{identity}><thead><tr>'
            + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
            + "</tbody></table></div>")


def sources(periods: list[dict], key: str, section: str) -> str:
    return '<p class="foot">核对位置：' + " · ".join(
        f'<a href="{esc(p["source_url"])}#page={p["pages"][key]}" target="_blank" rel="noopener noreferrer">'
        f'{esc(p["label"])} {esc(section)}（起始 PDF 第 {p["pages"][key]} 页）</a>' for p in periods
    ) + "</p>"


def holding_changes(fund: dict, *, active: bool = False) -> list[list[str]]:
    """Join identities across quarters; never equate holdings at the same rank."""
    p2, p3 = fund["periods"]
    if active:
        p2, p3 = p2["active_portfolio"], p3["active_portfolio"]
    limit = "五" if active else "十"
    a = {h["issuer_id"]: h for h in p2["holdings"]}
    b = {h["issuer_id"]: h for h in p3["holdings"]}
    rows = []
    for identity in [*b, *(key for key in a if key not in b)]:
        h2, h3 = a.get(identity), b.get(identity)
        name = (h3 or h2)["name"]
        if h2 and h3:
            change = delta(h2["weight_pct"], h3["weight_pct"])
            rank_change = f'第 {h2["rank"]} → {h3["rank"]} 名'
        else:
            change = f"不可比：另一期未进入前{limit}大"
            rank_change = f"进入本期前{limit}大" if h3 else f"未进入本期前{limit}大"
        rows.append([name,
                     f'第 {h2["rank"]} 名 · {percent(h2["weight_pct"])}' if h2 else f"未进入该期前{limit}大",
                     f'第 {h3["rank"]} 名 · {percent(h3["weight_pct"])}' if h3 else f"未进入该期前{limit}大",
                     change, rank_change])
    return rows


def fund_holding_changes(fund: dict) -> list[list[str]]:
    """Join the disclosed underlying-fund snapshots by identity."""
    p2, p3 = fund["periods"]
    a = {h["issuer_id"]: h for h in p2["fund_holdings"]}
    b = {h["issuer_id"]: h for h in p3["fund_holdings"]}
    rows = []
    for identity in [*b, *(key for key in a if key not in b)]:
        h2, h3 = a.get(identity), b.get(identity)
        name = (h3 or h2)["name"]
        if h2 and h3:
            change = delta(h2["weight_pct"], h3["weight_pct"])
            rank_change = f'第 {h2["rank"]} → {h3["rank"]} 名'
        else:
            change = "不可比：另一期未进入前十大"
            rank_change = "进入本期前十大" if h3 else "未进入本期前十大"
        rows.append([name,
                     f'第 {h2["rank"]} 名 · {percent(h2["weight_pct"])}' if h2 else "未进入该期前十大",
                     f'第 {h3["rank"]} 名 · {percent(h3["weight_pct"])}' if h3 else "未进入该期前十大",
                     change, rank_change])
    return rows


_STYLE = """
:root{color-scheme:light;--bg:#f3f5f7;--surface:#fff;--text:#18222c;--muted:#66727e;--border:#d7dde3;--blue:#195c9b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;overflow-wrap:anywhere}
.page{width:min(100%,1100px);margin:0 auto;padding:24px 14px 44px}a{color:var(--blue)}.back{font-weight:700;text-decoration:none}.header{padding:18px 0}.header h1{margin:0;font-size:25px}.header p,.foot{color:var(--muted)}.note{padding:13px 15px;border-left:4px solid var(--blue);background:#eaf2f9}.section{margin-top:18px;padding:18px;border:1px solid var(--border);border-radius:8px;background:var(--surface)}h2{margin:0 0 12px;font-size:19px}h3{margin:18px 0 8px;font-size:16px}.insights{margin:0;padding-left:23px}.insights li{margin:8px 0}.table-wrap{overflow-x:auto}.table-hint{display:none}table{width:100%;border-collapse:collapse;min-width:640px;font-variant-numeric:tabular-nums}th,td{padding:10px;text-align:left;border-bottom:1px solid #e5e9ed;vertical-align:top}th{color:var(--muted);font-weight:600;background:#fafbfc}td:not(:first-child){white-space:nowrap}.foot{font-size:13px}.source-list{padding-left:20px}.source-list li{margin:9px 0}.columns{display:grid;grid-template-columns:1fr 1fr;gap:24px}.columns p{margin:8px 0}#top-ten td,#fund-top-ten td{white-space:normal;min-width:70px}#top-ten td:nth-child(2),#top-ten td:nth-child(4){min-width:170px}#fund-top-ten td:nth-child(2),#fund-top-ten td:nth-child(4),#fund-top-ten td:nth-child(5),#fund-top-ten td:nth-child(7){min-width:190px}@media(max-width:640px){.page{padding-top:16px}.header h1{font-size:21px}.section{padding:14px}.columns{grid-template-columns:1fr;gap:8px}.table-hint{display:block;font-size:12px;color:var(--muted);margin:6px 0}}
"""


def render_comparison_detail(data: dict, code: str) -> str:
    fund = data["funds"][code]
    p2, p3 = periods = fund["periods"]
    sections = []

    def section(title: str, body: str) -> None:
        sections.append(f'<section class="section"><h2>{esc(title)}</h2>{body}</section>')

    section("先看结论", '<ul class="insights">' + "".join(f'<li>{esc(x)}</li>' for x in fund["insights"]) + '</ul>')
    if fund["notes"]:
        section("比较口径与重要说明", '<ul class="insights">' + "".join(f'<li>{esc(x)}</li>' for x in fund["notes"]) + '</ul>')

    rows = []
    for a, b in zip(p2["classes"], p3["classes"]):
        rows.append([a.get("display_label", a["label"] + " 类") + "单季收益", percent(a["return_pct"]), percent(b["return_pct"]),
                     percent(p2["benchmark_pct"]), percent(p3["benchmark_pct"]),
                     f'{number(a["return_pct"]) - number(p2["benchmark_pct"]):+.2f} → '
                     f'{number(b["return_pct"]) - number(p3["benchmark_pct"]):+.2f} 个百分点'])
    body = table(["份额类别", "二季度收益", "三季度收益", "二季度基准", "三季度基准", "相对基准（两季）"], rows)
    rows = []
    for label, a in return_windows(p2).items():
        b = return_windows(p3)[label]
        rows.append([label, percent(a["return_pct"]), percent(b["return_pct"]), percent(a["benchmark_pct"]), percent(b["benchmark_pct"])])
    body += f'<h3>{esc(fund.get("return_window_label", "A 类人民币计价"))}：截至各报告期末的累计观察窗口</h3>'
    body += table(["窗口", "6月末净值增长", "9月末净值增长", "6月末同期基准", "9月末同期基准"], rows)
    body += '<p class="foot">“过去三个月”分别对应 4–6 月、7–9 月，其他滚动窗口起止日不同；均为累计收益，非年化收益。相对基准为收益之差。未扣除持有人申购赎回等交易费用。</p>'
    if fund.get("benchmark_note"):
        body += f'<p class="note">{esc(fund["benchmark_note"])}</p>'
    section("收益与基准", body + sources(periods, "performance", "§3.2.1"))

    rows = []
    for a, b in zip(p2["classes"], p3["classes"]):
        rows.append([a.get("display_label", a["label"] + " 类") + "单位净值（元）", a["nav"], b["nav"], f'{number(b["nav"]) - number(a["nav"]):+.4f} 元'])
        rows.append([a.get("net_assets_label", a["label"] + " 类资产净值（亿元）"), f'{number(a["net_assets_cny"])/10**8:.4f}', f'{number(b["net_assets_cny"])/10**8:.4f}', f'{(number(b["net_assets_cny"])-number(a["net_assets_cny"]))/10**8:+.4f} 亿元'])
    totals = [sum(number(c["net_assets_cny"]) for c in p["classes"]) for p in periods]
    if len(p2["classes"]) > 1 or not p2["classes"][0].get("net_assets_label"):
        rows.append(["各类别资产净值合计（亿元）", f'{totals[0]/10**8:.4f}', f'{totals[1]/10**8:.4f}', f'{(totals[1]-totals[0])/10**8:+.4f} 亿元'])
    body = table(["项目", "2026-06-30", "2026-09-30", "期末差额"], rows)
    body += '<p class="foot">金额按原报告人民币口径汇总，亿元展示保留四位小数。资产净值变化同时受收益、申购赎回等影响，不能当作净申购金额；单位净值差额也不能替代含分红口径的净值增长率。</p>'
    section("资产规模与净值", body + sources(periods, "financial", "§3.1"))

    if "fund_holdings" in p2:
        def analogy_text(holding: dict) -> str:
            analogy = holding["domestic_analogue"]
            examples = "、".join(
                f'{example["name"]}（{example["code"]}，{example["exchange"]}）'
                for example in analogy["examples"]
            )
            return f'{analogy["benchmark"]}；' + (f'可参考：{examples}。' if examples else "暂无完全对应的国内场内基金。") + f' {analogy["note"]}'

        rows = []
        for a, b in zip(p2["fund_holdings"], p3["fund_holdings"]):
            rows.append([str(a["rank"]), a["name"], percent(a["weight_pct"]), analogy_text(a),
                         b["name"], percent(b["weight_pct"]), analogy_text(b)])
        body = f'<p>{esc(fund.get("fund_holdings_scope_note", ""))}</p>' + table(
            ["名次", "二季度底层基金", "二季度权重", "二季度国内场内类比",
             "三季度底层基金", "三季度权重", "三季度国内场内类比"], rows, table_id="fund-top-ten")
        sums = [sum(number(h["weight_pct"]) for h in p["fund_holdings"]) for p in periods]
        body += f'<p>前十大底层基金权重合计：{sums[0]:.2f}% → {sums[1]:.2f}%（{sums[1]-sums[0]:+.2f} 个百分点）。</p>'
        body += '<p class="foot">国内场内基金仅用于帮助理解指数或策略暴露，均不是对应海外基金的同一产品；产品结构、汇率、费率、跟踪误差和交易价格可能不同。类比按底层基金名称及其公开跟踪方向整理，不代表推荐或收益等价。</p>'
        section(fund.get("fund_holdings_title", "底层基金投资前十大及国内场内类比"),
                body + sources(periods, "fund_holdings", fund.get("fund_holdings_section", "§5.9")))
        section("同一底层基金权重与名次变化",
                table(["底层基金（报告口径）", "二季度", "三季度", "权重变化", "名次变化"],
                      fund_holding_changes(fund), table_id="fund-holding-changes")
                + '<p class="foot">按底层基金/ETF身份合并两期快照；另一期未进入前十大时不把未披露权重按零计算。</p>')

    for key, title, basis, locator in [
        ("assets", "资产配置", "占基金总资产比例", "§5.1"),
        ("regions", "地区配置", "占基金资产净值比例", "§5.2"),
        ("sectors", "行业配置", "占基金资产净值比例", "§5.3"),
    ]:
        if key == "sectors":
            title = fund.get("sectors_title", title)
            locator = fund.get("sectors_section", locator)
        keys = list(dict.fromkeys([*p2[key], *p3[key]]))
        rows = [[k, percent(p2[key].get(k), "未列示" if key == "regions" else "—"),
                 percent(p3[key].get(k), "未列示" if key == "regions" else "—"),
                 delta(p2[key].get(k), p3[key].get(k))] for k in keys]
        if key == "regions":
            sums = [sum(number(v) for v in p[key].values()) for p in periods]
            rows.append(["股票及存托凭证合计（披露分项求和）", f'{sums[0]:.2f}%', f'{sums[1]:.2f}%', f'{sums[1]-sums[0]:+.2f} 个百分点'])
        if key == "sectors" and "sectors_total_pct" in p2:
            rows.append(["本部分股票合计（报告披露）", percent(p2["sectors_total_pct"]), percent(p3["sectors_total_pct"]), delta(p2["sectors_total_pct"], p3["sectors_total_pct"])])
        body = f'<p>{esc(basis)}；变化为三季度末减二季度末。</p>' + table(["项目", "二季度末", "三季度末", "变化"], rows)
        if key == "assets":
            body += '<p class="foot">本表分母是总资产，与下面地区、行业和持仓的净资产分母不同。横线保留原报告空项，不当作已披露的 0；0.00% 保留报告四舍五入结果。分项合计可能存在尾差。</p>'
        elif key == "regions":
            body += '<p class="foot">地区依照证券或存托凭证挂牌交易所分类；美国 ADR 不等同于发行人注册地或美国业务收入。未列示不等于零，不推算缺失值。本页不改动榜单的美国权益穿透结果。</p>'
        else:
            body += '<p class="foot">沿用本基金两期报告的行业分类，只展示至少一期有数值的类别。横线保留原报告空项，不推算为零；未同时披露数值时不计算变化。</p>'
        section(title, body + sources(periods, key, locator))

    rows = [[str(a["rank"]), a["name"], percent(a["weight_pct"]), b["name"], percent(b["weight_pct"])]
            for a, b in zip(p2["holdings"], p3["holdings"])]
    body = table(["名次", "二季度持仓", "二季度权重", "三季度持仓", "三季度权重"], rows, table_id="top-ten")
    sums = [sum(number(h["weight_pct"]) for h in p["holdings"]) for p in periods]
    body += f'<p>前十大权重合计：{sums[0]:.2f}% → {sums[1]:.2f}%（{sums[1]-sums[0]:+.2f} 个百分点）。</p>'
    body += '<p class="foot">每期严格保留原报告第 1–10 名，权重分母为基金资产净值。同一名次下的跨市场或股份类别拆行按报告合并，见重要说明；同一行仅比较名次，不把不同股票的权重直接相减。</p>'
    if fund.get("holdings_scope_note"):
        body = f'<p class="note">{esc(fund["holdings_scope_note"])}</p>' + body
    section(fund.get("holdings_title", "前十大持仓权重（完整十个名次）"), body + sources(periods, "holdings", fund.get("holdings_section", "§5.4")))
    section(fund.get("holding_changes_title", "同一公司持仓权重与名次变化"), table(["公司（报告口径）", "二季度", "三季度", "权重变化", "名次变化"], holding_changes(fund), table_id="holding-changes")
            + '<p class="foot">未进入另一期前十大不代表新买入或清仓，其未披露权重也不能按零计算。期末权重变化受到价格、汇率、交易及净资产变化共同影响，不能直接解释为买卖数量。</p>')

    if "active_portfolio" in p2:
        a2, a3 = p2["active_portfolio"], p3["active_portfolio"]
        keys = list(dict.fromkeys([*a2["sectors"], *a3["sectors"]]))
        rows = [[k, percent(a2["sectors"].get(k)), percent(a3["sectors"].get(k)), delta(a2["sectors"].get(k), a3["sectors"].get(k))] for k in keys
                if a2["sectors"].get(k) is not None or a3["sectors"].get(k) is not None]
        rows.append(["积极投资股票合计（报告披露）", percent(a2["sectors_total_pct"]), percent(a3["sectors_total_pct"]), delta(a2["sectors_total_pct"], a3["sectors_total_pct"])])
        section("行业配置：积极投资部分", '<p>占全基金资产净值比例；与指数投资部分分开披露。</p>'
                + table(["行业", "二季度末", "三季度末", "变化"], rows, table_id="active-sectors")
                + '<p class="foot">横线保留原报告空项，不推算为零。合计使用报告披露值，分项四舍五入可能产生尾差。</p>'
                + sources(periods, "active_sectors", "§5.3.2"))
        rows = [[str(a["rank"]), a["name"], percent(a["weight_pct"]), b["name"], percent(b["weight_pct"])]
                for a, b in zip(a2["holdings"], a3["holdings"])]
        section("积极投资前五大持仓（完整五个名次）", '<p>权重占全基金资产净值，不是占积极投资部分的比例；不与指数部分拼接成全基金前十大。</p>'
                + table(["名次", "二季度持仓", "二季度权重", "三季度持仓", "三季度权重"], rows, table_id="active-top-five")
                + sources(periods, "active_holdings", "§5.4.2"))
        section("积极投资：同一公司权重与名次变化", table(["公司", "二季度", "三季度", "权重变化", "名次变化"], holding_changes(fund, active=True), table_id="active-holding-changes")
                + '<p class="foot">比较范围仅为积极投资部分前五名；未进入另一季该部分前五名不代表基金未持有或已清仓，也不能按零计算权重差。</p>')

    body = '<p class="foot">以下分别概括两期管理人报告的观点及操作说明，不作为独立验证的宏观事实，也不据此推算持仓收益贡献。</p><div class="columns">'
    for p in periods:
        body += f'<div><h3>{esc(p["label"])}</h3>' + ''.join(f'<p>{esc(s)}</p>' for s in p["strategy"]) + '</div>'
    body += '</div>'
    section("报告披露的市场背景与投资策略", body + sources(periods, "strategy", "投资策略和业绩表现说明"))
    section("原始报告", '<ul class="source-list">' + ''.join(
        f'<li>{esc(p["label"])} · 报告期 {esc(p["report_date"])} · 发布 {esc(p["published_date"])}：'
        f'<a href="{esc(p["source_url"])}" target="_blank" rel="noopener noreferrer">原始公告/PDF ↗</a></li>' for p in periods)
        + '</ul><p class="foot">数据取自上述两期定期报告的 §3、投资策略说明和 §5；页面为披露字段整理及明确列示的差额计算，不替代基金管理人公告。</p>')
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(fund["name"])} · {esc(data["title"])}</title><style>{_STYLE}</style></head><body><main class="page">'
            '<a class="back" href="index.html">← 返回财报对比列表</a><header class="header">'
            f'<h1>{esc(fund["name"])}</h1><p>{esc(data["title"])} · 基金代码 {esc(code)}</p>'
            f'<p>生成于 {esc(data["generated_at"])}</p></header><p class="note">以两期季报披露为准，'
            '每张表单独标明分母；未披露的持仓、交易与数值不作估算。</p>\n'
            + '\n'.join(sections) + '\n</main></body></html>\n')
