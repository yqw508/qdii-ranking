#!/usr/bin/env python3
"""Send QDII ranking success or failure notifications through QQ SMTP."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


SHANGHAI_TZ = timezone(timedelta(hours=8))
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465


class MailConfigurationError(RuntimeError):
    """Raised when required SMTP settings are absent or invalid."""


def current_shanghai_date() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def format_percentage(value: Any, show_sign: bool = False) -> str:
    number = float(value)
    return f"{number:+.2f}%" if show_sign else f"{number:.2f}%"


def format_correlation(value: Any) -> str:
    return f"{float(value) * 100:.1f}%"


def format_beta(value: Any) -> str:
    return f"{float(value):.2f}"


def format_limit(limit: Mapping[str, Any]) -> str:
    status = limit.get("status")
    if status == "unlimited":
        return "正常开放"
    if status == "suspended":
        return "暂停申购"
    amount = limit.get("amount_cny")
    if status == "unknown" or amount is None:
        return "待核实"
    amount = int(amount)
    if amount >= 10000 and amount % 10000 == 0:
        return f"{amount // 10000:,}万元"
    return f"{amount:,}元"


def parse_recipients(value: str, fallback: str) -> list[str]:
    recipients = [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
    if not recipients:
        recipients = [fallback]
    if any("@" not in item for item in recipients):
        raise MailConfigurationError("QQ_MAIL_TO contains an invalid email address")
    return recipients


def smtp_configuration(
    environ: Mapping[str, str],
) -> tuple[str, str, list[str]]:
    sender = environ.get("QQ_SMTP_USER", "").strip()
    auth_code = environ.get("QQ_SMTP_AUTH_CODE", "").strip()
    if not sender or "@" not in sender:
        raise MailConfigurationError("QQ_SMTP_USER is not configured")
    if not auth_code:
        raise MailConfigurationError("QQ_SMTP_AUTH_CODE is not configured")
    recipients = parse_recipients(environ.get("QQ_MAIL_TO", ""), sender)
    return sender, auth_code, recipients


def load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailConfigurationError(f"Could not read ranking data: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise MailConfigurationError("Ranking data is incomplete")
    return payload


def material_notes(payload: Mapping[str, Any]) -> list[str]:
    notes: list[str] = []
    for warning in payload.get("warnings", []):
        if warning.startswith("跳过未完整披露的持有人报告期") or (
            "美股占比区间" in warning and "按保守规则排除" in warning
        ) or warning.startswith("纳指100基准更新失败，使用完整缓存："):
            notes.append(warning)
    for record in payload["records"]:
        exposure = record["us_equity_exposure"]
        confirmed = float(exposure["confirmed_pct"])
        possible = float(exposure["possible_pct"])
        if possible > confirmed:
            notes.append(
                f"{record['code']} {record['name']}：美股占比保守区间 "
                f"{confirmed:.2f}%-{possible:.2f}%。"
            )
    return notes


def success_plain_text(payload: Mapping[str, Any], page_url: str) -> str:
    lines = [
        f"QDII 纳指相关榜单已于 {payload['run_date']} 更新并发布。",
        "",
        "排名  基金代码  基金名称  纳指相关/Beta  美股下限  近三年收益  直销/代销额度",
    ]
    for record in payload["records"]:
        lines.append(
            f"{record['rank']:>2}  {record['code']}  {record['name']}  "
            f"{format_correlation(record['nasdaq100_fit']['correlation'])}/"
            f"{format_beta(record['nasdaq100_fit']['beta'])}  "
            f"{format_percentage(record['us_equity_exposure']['confirmed_pct'])}  "
            f"{format_percentage(record['three_year_return_pct'], show_sign=True)}  "
            f"{format_limit(record['direct_limit'])}/{format_limit(record['agency_limit'])}"
        )
    notes = material_notes(payload)
    if notes:
        lines.extend(["", "重要说明：", *(f"- {note}" for note in notes)])
    lines.extend(["", f"网页地址：{page_url}"])
    return "\n".join(lines)


def success_html(payload: Mapping[str, Any], page_url: str) -> str:
    rows: list[str] = []
    for record in payload["records"]:
        fit = record["nasdaq100_fit"]
        rows.append(
            "<tr>"
            f"<td>{record['rank']}</td>"
            f"<td><strong>{html.escape(record['name'])}</strong>"
            f"<br><span>{html.escape(record['code'])}</span></td>"
            f"<td>{format_correlation(fit['correlation'])}<br>β {format_beta(fit['beta'])}</td>"
            f"<td>{format_percentage(record['us_equity_exposure']['confirmed_pct'])}</td>"
            f"<td>{format_percentage(record['three_year_return_pct'], show_sign=True)}</td>"
            f"<td>{float(record['scale_billion_cny']):.2f} 亿元</td>"
            f"<td>{html.escape(format_limit(record['direct_limit']))}</td>"
            f"<td>{html.escape(format_limit(record['agency_limit']))}</td>"
            "</tr>"
        )
    notes = material_notes(payload)
    notes_html = ""
    if notes:
        notes_html = (
            "<h2>重要说明</h2><ul>"
            + "".join(f"<li>{html.escape(note)}</li>" for note in notes)
            + "</ul>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#202124;line-height:1.5">
  <h1 style="font-size:20px;margin:0 0 8px">QDII 纳指相关榜单</h1>
  <p style="margin:0 0 16px">{html.escape(payload['run_date'])} 已更新并发布。</p>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <thead><tr>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">排名</th>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">基金</th>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">纳指相关 / Beta</th>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">美股下限</th>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">近三年</th>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">规模</th>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">直销</th>
      <th style="text-align:left;border-bottom:1px solid #bbb;padding:6px">代销</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  {notes_html}
  <p><a href="{html.escape(page_url, quote=True)}">打开完整榜单</a></p>
</body>
</html>"""


def build_success_message(
    payload: Mapping[str, Any], page_url: str, sender: str, recipients: list[str]
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"[QDII榜单] {payload['run_date']} 更新成功"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(success_plain_text(payload, page_url))
    message.add_alternative(success_html(payload, page_url), subtype="html")
    return message


def build_failure_message(
    run_date: str,
    stage: str,
    detail: str,
    run_url: str,
    sender: str,
    recipients: list[str],
    publication_state: str = "not-published",
) -> EmailMessage:
    safe_stage = stage or "unknown"
    safe_detail = detail or "No additional diagnostic detail was provided."
    publication_notes = {
        "not-published": "本次未提交或部署未通过校验的数据。",
        "repository-updated": "生成页面已提交到 main，但 CloudBase 部署尚未完成。",
        "deployment-unverified": "CloudBase 部署命令已完成，但独立网页尚未通过验证。",
        "published": "网页已经验证发布，本次异常仅发生在成功通知阶段。",
    }
    publication_note = publication_notes.get(publication_state)
    if publication_note is None:
        raise MailConfigurationError("Invalid publication state")
    subject_status = "通知发送异常" if publication_state == "published" else "更新失败"
    heading = "成功通知发送异常" if publication_state == "published" else "自动更新失败"
    plain = (
        f"QDII 榜单 {run_date} {heading}。\n\n"
        f"失败阶段：{safe_stage}\n"
        f"执行结果：{safe_detail}\n"
        f"运行日志：{run_url}\n\n"
        f"{publication_note}"
    )
    html_body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#202124;line-height:1.5">
  <h1 style="font-size:20px">QDII 榜单{html.escape(heading)}</h1>
  <p><strong>日期：</strong>{html.escape(run_date)}</p>
  <p><strong>失败阶段：</strong>{html.escape(safe_stage)}</p>
  <p><strong>执行结果：</strong>{html.escape(safe_detail)}</p>
  <p><a href="{html.escape(run_url, quote=True)}">查看 GitHub Actions 运行日志</a></p>
  <p>{html.escape(publication_note)}</p>
</body></html>"""
    message = EmailMessage()
    message["Subject"] = f"[QDII榜单] {run_date} {subject_status}"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")
    return message


def send_message(
    message: EmailMessage,
    environ: Mapping[str, str] = os.environ,
    smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
) -> None:
    sender, auth_code, _ = smtp_configuration(environ)
    with smtp_factory(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(sender, auth_code)
        smtp.send_message(message)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", required=True, choices=("success", "failure"))
    parser.add_argument("--data", type=Path, default=Path("output/qdii-ranking/latest.json"))
    parser.add_argument("--url", default="")
    parser.add_argument("--date", default=current_shanghai_date())
    parser.add_argument("--failure-stage", default="unknown")
    parser.add_argument("--failure-detail", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument(
        "--publication-state",
        choices=(
            "not-published",
            "repository-updated",
            "deployment-unverified",
            "published",
        ),
        default="not-published",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sender, _, recipients = smtp_configuration(os.environ)
        if args.status == "success":
            if not args.url:
                raise MailConfigurationError("--url is required for a success email")
            payload = load_payload(args.data)
            message = build_success_message(payload, args.url, sender, recipients)
        else:
            message = build_failure_message(
                args.date,
                args.failure_stage,
                args.failure_detail,
                args.run_url,
                sender,
                recipients,
                args.publication_state,
            )
        send_message(message)
    except (MailConfigurationError, OSError, smtplib.SMTPException) as exc:
        print(f"ERROR: Could not send QQ email: {exc}", file=sys.stderr)
        return 1
    print(f"Sent {args.status} notification to {len(recipients)} recipient(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
