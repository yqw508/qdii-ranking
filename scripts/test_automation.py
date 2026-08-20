import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import send_qdii_email as mailer
import update_qdii_ranking as ranking
import validate_qdii_ranking as validator


RUN_DATE = "2026-08-20"


def make_record(rank):
    code = f"{rank:06d}"
    source = f"https://example.test/{code}/notice.pdf"
    confirmed = 100.0 - rank
    possible = confirmed + (0.5 if rank == 1 else 0.0)
    return {
        "rank": rank,
        "code": code,
        "name": f"全球科技精选{rank}(QDII)A",
        "fund_type": "QDII-普通股票",
        "institution_holding_ratio_pct": 50.0 - rank,
        "holder_report_date": "2025-12-31",
        "inception_date": "2020-01-01",
        "scale_billion_cny": 10.0 + rank,
        "scale_report_date": "2026-06-30",
        "purchase_status": "limited",
        "purchase_status_text": "限额申购",
        "fund_page_url": f"https://example.test/fund/{code}",
        "performance_source_url": f"https://example.test/performance/{code}.js",
        "one_year_return_pct": 20.0 + rank,
        "one_year_max_drawdown_pct": -10.0 - rank,
        "one_year_performance_start_date": "2025-08-18",
        "one_year_performance_end_date": "2026-08-18",
        "three_year_return_pct": 80.0 + rank,
        "three_year_max_drawdown_pct": -20.0 - rank,
        "three_year_performance_start_date": "2023-08-18",
        "three_year_performance_end_date": "2026-08-18",
        "us_equity_exposure": {
            "confirmed_pct": confirmed,
            "possible_pct": possible,
            "direct_us_pct": confirmed,
            "lookthrough_confirmed_pct": 0.0,
            "unresolved_pct": possible - confirmed,
            "report_date": "2026-06-30",
            "published_date": "2026-07-21",
            "source_url": f"https://example.test/{code}/report.pdf",
            "components": [],
            "status": "qualified",
        },
        "quota_status": "limited",
        "quota_confidence": "high",
        "direct_limit": {
            "status": "limited",
            "amount_cny": 100000,
            "effective_date": "2026-08-01",
            "source_url": source,
            "confidence": "high",
        },
        "agency_limit": {
            "status": "limited",
            "amount_cny": 1000,
            "effective_date": "2026-08-01",
            "source_url": source,
            "confidence": "high",
        },
        "share_class_rule": "A/C separate",
        "channel_rule": "direct and agency limits differ",
        "quota_source_urls": [source],
    }


def make_payload():
    return {
        "schema_version": 6,
        "run_date": RUN_DATE,
        "generated_at": "2026-08-20T09:08:00+08:00",
        "holder_report_date": "2025-12-31",
        "holder_period_fund_count": 24000,
        "filters": {
            "top": 10,
            "min_scale_billion_cny": 3.0,
            "min_age_years": 3,
            "min_three_year_return_pct": 50.0,
            "min_us_equity_pct": 50.0,
            "base_candidates_total": 42,
            "performance_candidates_scanned": 42,
            "performance_qualified_count": 27,
            "us_equity_candidates_scanned": 27,
            "us_equity_qualified_count": 11,
            "full_scan_completed": True,
            "ranking_method": "established",
            "us_equity_method": "conservative confirmed lower bound",
            "exclude_keywords": ["债", "亚洲", "中国", "港"],
            "pre_rank_exclude_keywords": ["债"],
            "post_enrichment_exclude_keywords": ["亚洲", "中国", "港"],
            "exclude_fund_types": ["QDII-纯债"],
            "share_class": "RMB A or explicit RMB primary share without C/D marker",
            "purchasable_only": True,
        },
        "cache": {
            "performance": {"hits": 42, "misses": 0, "corrupt_rebuilds": 0},
            "fund_us_equity_exposures": {
                "hits": 27,
                "misses": 0,
                "corrupt_rebuilds": 0,
            },
            "periodic_reports": {
                "hits": 0,
                "downloads": 0,
                "corrupt_redownloads": 0,
            },
            "underlying_exposures": {"hits": 0, "misses": 0},
        },
        "records": [make_record(rank) for rank in range(1, 11)],
        "warnings": [
            "跳过未完整披露的持有人报告期 2026-06-30：覆盖率不足。",
            "000001 Sample ETF 无法按 2026-06-30 的可用数据穿透，其 0.50% 仓位仅计入可能上限。",
            "000099 美股占比区间 49.00%-51.00% 跨越 50% 阈值，按保守规则排除。",
        ],
        "sources": {},
    }


def write_artifacts(root, payload):
    output_dir = root / "output"
    publish_dir = root / "public"
    ranking.write_json(output_dir / "latest.json", payload)
    ranking.write_csv(output_dir / "latest.csv", payload["records"])
    ranking.write_markdown(output_dir / "latest.md", payload)
    ranking.write_html(output_dir / "latest.html", payload)
    ranking.write_html(publish_dir / "index.html", payload)
    return output_dir, publish_dir


class RankingValidatorTests(unittest.TestCase):
    def validate(self, payload=None, expected_date=RUN_DATE):
        with TemporaryDirectory() as directory:
            output_dir, publish_dir = write_artifacts(
                Path(directory), payload or make_payload()
            )
            return validator.validate_local_artifacts(
                output_dir, publish_dir, expected_date
            )

    def test_accepts_complete_artifacts_and_reportable_warnings(self):
        payload, warnings = self.validate()
        self.assertEqual(10, len(payload["records"]))
        self.assertEqual(3, len(warnings))

    def test_rejects_record_shortfall(self):
        payload = make_payload()
        payload["records"].pop()
        with self.assertRaisesRegex(validator.ValidationError, "exactly ten"):
            self.validate(payload)

    def test_rejects_incomplete_full_scan(self):
        payload = make_payload()
        payload["filters"]["full_scan_completed"] = False
        with self.assertRaisesRegex(validator.ValidationError, "Full scan"):
            self.validate(payload)

    def test_rejects_incorrect_order(self):
        payload = make_payload()
        payload["records"][0]["us_equity_exposure"]["confirmed_pct"] = 80.0
        with self.assertRaisesRegex(validator.ValidationError, "sort rule"):
            self.validate(payload)

    def test_rejects_unknown_quota(self):
        payload = make_payload()
        payload["records"][0]["quota_status"] = "unknown"
        payload["records"][0]["quota_confidence"] = "low"
        payload["records"][0]["direct_limit"] = {
            "status": "unknown",
            "amount_cny": None,
        }
        with self.assertRaisesRegex(validator.ValidationError, "quota is unresolved"):
            self.validate(payload)

    def test_rejects_quota_source_not_listed_in_announcements(self):
        payload = make_payload()
        payload["records"][0]["quota_source_urls"] = [
            "https://example.test/different-notice.pdf"
        ]
        with self.assertRaisesRegex(validator.ValidationError, "quota_source_urls"):
            self.validate(payload)

    def test_rejects_unrecognized_warning(self):
        payload = make_payload()
        payload["warnings"].append("Unexpected new warning category")
        with self.assertRaisesRegex(validator.ValidationError, "Blocking warnings"):
            self.validate(payload)

    def test_rejects_stale_date(self):
        with self.assertRaisesRegex(validator.ValidationError, "Shanghai date"):
            self.validate(expected_date="2026-08-21")

    def test_rejects_generated_html_difference(self):
        with TemporaryDirectory() as directory:
            output_dir, publish_dir = write_artifacts(Path(directory), make_payload())
            (publish_dir / "index.html").write_text("different", encoding="utf-8")
            with self.assertRaisesRegex(validator.ValidationError, "byte-identical"):
                validator.validate_local_artifacts(output_dir, publish_dir, RUN_DATE)

    def test_rejects_csv_metric_difference(self):
        with TemporaryDirectory() as directory:
            output_dir, publish_dir = write_artifacts(Path(directory), make_payload())
            path = output_dir / "latest.csv"
            content = path.read_text(encoding="utf-8-sig").replace("99.0", "98.0", 1)
            path.write_text(content, encoding="utf-8-sig")
            with self.assertRaisesRegex(validator.ValidationError, "confirmed exposure"):
                validator.validate_local_artifacts(output_dir, publish_dir, RUN_DATE)


class FakeSmtp:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_args = None
        self.message = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def login(self, sender, auth_code):
        self.login_args = (sender, auth_code)

    def send_message(self, message):
        self.message = message


class RankingEmailTests(unittest.TestCase):
    def setUp(self):
        FakeSmtp.instances.clear()
        self.environ = {
            "QQ_SMTP_USER": "sender@qq.com",
            "QQ_SMTP_AUTH_CODE": "smtp-auth-code",
            "QQ_MAIL_TO": "first@qq.com;second@example.com",
        }

    def test_success_email_contains_table_link_and_material_notes(self):
        payload = make_payload()
        sender, _, recipients = mailer.smtp_configuration(self.environ)
        message = mailer.build_success_message(
            payload,
            "https://example.test/?v=2026-08-20",
            sender,
            recipients,
        )
        plain = message.get_body(preferencelist=("plain",)).get_content()
        html_body = message.get_body(preferencelist=("html",)).get_content()
        self.assertEqual("[QDII榜单] 2026-08-20 更新成功", message["Subject"])
        self.assertIn("000001", plain)
        self.assertIn("99.00%-99.50%", plain)
        self.assertIn("https://example.test/?v=2026-08-20", plain)
        self.assertIn("<table", html_body)
        self.assertIn("10万元", html_body)

    def test_failure_email_contains_stage_and_run_link(self):
        message = mailer.build_failure_message(
            RUN_DATE,
            "deployment",
            "deploy=failure",
            "https://github.com/example/actions/runs/1",
            "sender@qq.com",
            ["sender@qq.com"],
        )
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("deployment", plain)
        self.assertIn("deploy=failure", plain)
        self.assertIn("actions/runs/1", plain)
        self.assertIn("未提交或部署", plain)

    def test_notification_failure_reports_already_published_state(self):
        message = mailer.build_failure_message(
            RUN_DATE,
            "success-email",
            "success_email=failure",
            "https://github.com/example/actions/runs/1",
            "sender@qq.com",
            ["sender@qq.com"],
            "published",
        )
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("通知发送异常", message["Subject"])
        self.assertIn("网页已经验证发布", plain)

    def test_recipient_defaults_to_sender(self):
        environ = deepcopy(self.environ)
        environ["QQ_MAIL_TO"] = ""
        sender, _, recipients = mailer.smtp_configuration(environ)
        self.assertEqual([sender], recipients)

    def test_missing_authorization_code_is_rejected(self):
        environ = {"QQ_SMTP_USER": "sender@qq.com"}
        with self.assertRaisesRegex(mailer.MailConfigurationError, "AUTH_CODE"):
            mailer.smtp_configuration(environ)

    def test_smtp_uses_ssl_login_without_exposing_credentials(self):
        payload = make_payload()
        sender, _, recipients = mailer.smtp_configuration(self.environ)
        message = mailer.build_success_message(
            payload, "https://example.test", sender, recipients
        )
        mailer.send_message(message, self.environ, FakeSmtp)
        smtp = FakeSmtp.instances[0]
        self.assertEqual((mailer.SMTP_HOST, mailer.SMTP_PORT, 30), (smtp.host, smtp.port, smtp.timeout))
        self.assertEqual(("sender@qq.com", "smtp-auth-code"), smtp.login_args)
        self.assertIs(message, smtp.message)


if __name__ == "__main__":
    unittest.main()
