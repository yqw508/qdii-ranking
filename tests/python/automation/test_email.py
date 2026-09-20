import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import send_qdii_email as mailer
import report_update_metrics as metrics_reporter
import update_qdii_ranking as ranking
import validate_qdii_ranking as validator

RUN_DATE = "2026-08-20"

from tests.python.support.automation import (
    RUN_DATE,
    make_exchange_premium,
    make_global_record,
    make_payload,
    make_record,
    write_artifacts,
)

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
        payload["global_supplement"]["records"][0]["name"] = "富国中国精选混合(QDII)人民币A"
        payload["global_supplement"]["records"][0][
            "routing_reason"
        ] = "us_main_name_geography_override"
        payload["warnings"].append(
            "纳指100基准更新失败，使用完整缓存：Nasdaq XNDX 最新数据 2026-08-19。"
        )
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
        self.assertIn("100001", plain)
        self.assertIn("美国主榜（3只）", plain)
        self.assertIn("全球补充榜（3只）", plain)
        self.assertIn("纳斯达克100指数", plain)
        self.assertIn("德国DAX指数", plain)
        self.assertIn("地域名称分流", plain)
        self.assertIn("因名称命中美国主榜地域关键词", plain)
        self.assertIn("99.00%-99.50%", plain)
        self.assertIn("99.0%/1.01", plain)
        self.assertIn("使用完整缓存", plain)
        self.assertIn("https://example.test/?v=2026-08-20", plain)
        self.assertIn("<table", html_body)
        self.assertIn("10万元", html_body)
        self.assertIn("99.0%", html_body)
        self.assertIn("收益回撤比", html_body)
        self.assertIn("被剔除候选摘要", plain)
        self.assertIn("000099, 000100", plain)
        self.assertIn("被剔除候选摘要", html_body)

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
