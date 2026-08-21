import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import send_qdii_email as mailer
import report_update_metrics as metrics_reporter
import update_qdii_ranking as ranking
import validate_qdii_ranking as validator


RUN_DATE = "2026-08-20"


class PerformanceReportingTests(unittest.TestCase):
    def test_report_quantifies_latency_requests_and_pdf_work(self):
        baseline = {
            "summary": {
                "refresh_seconds": {"median": 600},
                "end_to_end_seconds": {"median": 640},
                "announcement_index_calls_minimum": 102,
                "pdf_text_extractions": 301,
            },
            "targets": {
                "refresh_median_seconds_max": 240,
                "end_to_end_median_seconds_max": 300,
                "announcement_index_reduction_pct_min": 66,
            },
        }
        current = {
            "refresh_seconds": 180,
            "http": {"announcement_index": {"calls": 34}},
            "cache": {"announcement_pdfs": {"text_extractions": 0}},
        }
        report = metrics_reporter.build_report(baseline, current, 240)
        self.assertIn("70.0%", report)
        self.assertIn("66.7%", report)
        self.assertIn("100.0%", report)
        self.assertEqual(4, report.count("PASS"))

    def test_workflow_runs_daily_at_0707_and_has_cross_version_cache_restore(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "update-ranking.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "7 23 * * *"', workflow)
        self.assertIn("qdii-ranking-${{ runner.os }}-", workflow)
        self.assertIn("Report performance comparison", workflow)


def make_record(rank, ranking_list="us_main"):
    code = f"{rank if ranking_list == 'us_main' else 100000 + rank:06d}"
    source = f"https://example.test/{code}/notice.pdf"
    confirmed = 100.0 - rank
    possible = confirmed + (0.5 if rank == 1 else 0.0)
    return {
        "rank": rank,
        "ranking_list": ranking_list,
        "routing_reason": (
            "confirmed_us_exposure"
            if ranking_list == "us_main"
            else "us_exposure_below_threshold"
        ),
        "code": code,
        "name": f"全球科技精选{rank}(QDII)A",
        "fund_type": "QDII-普通股票",
        "management_style": "active",
        "product_structure_tags": ["主动", "股票", "大盘成长"],
        "contract_benchmark": {
            "status": "recognized",
            "benchmark_text": "纳斯达克100指数收益率×95%+人民币活期存款利率×5%",
            "benchmark_id": "nasdaq-100" if ranking_list == "us_main" else "dax",
            "benchmark_name": "纳斯达克100指数" if ranking_list == "us_main" else "德国DAX指数",
            "benchmark_weight_pct": 95.0,
            "market_scope": "us" if ranking_list == "us_main" else "non_us",
            "market_label": "美国" if ranking_list == "us_main" else "德国",
            "asset_class": "equity",
            "style_label": "大盘成长" if ranking_list == "us_main" else "大盘宽基",
            "structure": "standard",
            "excluded_target": False,
            "components": [
                {
                    "benchmark_id": "nasdaq-100" if ranking_list == "us_main" else "dax",
                    "benchmark_name": "纳斯达克100指数" if ranking_list == "us_main" else "德国DAX指数",
                    "weight_pct": 95.0,
                    "market_scope": "us" if ranking_list == "us_main" else "non_us",
                    "market_label": "美国" if ranking_list == "us_main" else "德国",
                    "asset_class": "equity",
                    "style_label": "大盘成长" if ranking_list == "us_main" else "大盘宽基",
                    "structure": "standard",
                    "excluded_target": False,
                }
            ],
            "management_style": "active",
            "prospectus_title": "更新招募说明书",
            "prospectus_published_date": "2026-06-01",
            "source_url": f"https://example.test/{code}/prospectus.pdf",
            "product_summary_status": "matched",
            "product_summary_published_date": "2026-06-02",
            "product_summary_source_url": f"https://example.test/{code}/summary.pdf",
            "catalog_fingerprint": "a" * 64,
        },
        "holding_cost": {
            "status": "parsed",
            "annualized_pct": 0.66 + rank / 100,
            "measurement_date": "2026-06-01",
            "source_title": "人民币产品资料概要",
            "source_published_date": "2026-06-02",
            "source_url": f"https://example.test/{code}/summary.pdf",
        },
        "institution_holding_ratio_pct": 50.0 - rank,
        "holder_report_date": "2025-12-31",
        "inception_date": "2010-01-01",
        "scale_billion_cny": 10.0 + rank,
        "scale_report_date": "2026-06-30",
        "purchase_status": "limited",
        "purchase_status_text": "限额申购",
        "fund_page_url": f"https://example.test/fund/{code}",
        "performance_source_url": f"https://example.test/performance/{code}.js",
        "nav_history_start_date": "2010-01-04",
        "nav_history_end_date": "2026-08-18",
        "one_year_return_pct": 20.0 + rank,
        "one_year_max_drawdown_pct": -10.0 - rank,
        "one_year_performance_start_date": "2025-08-18",
        "one_year_performance_end_date": "2026-08-18",
        "three_year_return_pct": 80.0 + rank,
        "three_year_max_drawdown_pct": -20.0 - rank,
        "three_year_performance_start_date": "2023-08-18",
        "three_year_performance_end_date": "2026-08-18",
        "five_year_return_pct": 120.0 + rank,
        "five_year_performance_start_date": "2021-08-18",
        "five_year_performance_end_date": "2026-08-18",
        "ten_year_return_pct": 220.0 + rank,
        "ten_year_performance_start_date": "2016-08-18",
        "ten_year_performance_end_date": "2026-08-18",
        "nasdaq100_fit": {
            "correlation": round(1.0 - rank / 100, 4),
            "beta": round(1.0 + rank / 100, 4),
            "tracking_error_pct": 4.0 + rank,
            "observations": 154,
            "start_date": "2023-08-18",
            "end_date": "2026-08-18",
        },
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


def make_global_record(rank):
    record = make_record(rank, "global_supplement")
    record["us_equity_exposure"].update(
        {
            "confirmed_pct": 30.0 - rank,
            "possible_pct": 35.0 - rank,
            "direct_us_pct": 30.0 - rank,
            "unresolved_pct": 5.0,
            "status": "excluded",
        }
    )
    total_return = float(record["three_year_return_pct"])
    span_days = 1096
    annualized = ((1 + total_return / 100) ** (365 / span_days) - 1) * 100
    record["three_year_annualized_return_pct"] = round(annualized, 2)
    record["return_drawdown_ratio"] = round(
        annualized / abs(float(record["three_year_max_drawdown_pct"])), 4
    )
    return record


def make_payload():
    return {
        "schema_version": 10,
        "run_date": RUN_DATE,
        "generated_at": "2026-08-20T09:08:00+08:00",
        "holder_report_date": "2025-12-31",
        "holder_period_fund_count": 24000,
        "filters": {
            "top": 10,
            "min_scale_billion_cny": None,
            "min_age_years": 3,
            "min_three_year_return_pct": 30.0,
            "min_five_year_return_pct_if_available": 60.0,
            "min_ten_year_return_pct_if_available": 100.0,
            "min_us_equity_pct": 50.0,
            "min_direct_limit_cny_inclusive": 200,
            "base_candidates_total": 42,
            "performance_candidates_scanned": 42,
            "performance_qualified_count": 27,
            "contract_candidates_scanned": 27,
            "contract_metadata_resolved_count": 8,
            "us_equity_candidates_scanned": 27,
            "us_routed_count": 12,
            "global_routed_count": 15,
            "us_quota_candidates_scanned": 12,
            "us_quota_qualified_count": 3,
            "global_quota_candidates_scanned": 15,
            "global_quota_qualified_count": 3,
            "full_scan_completed": True,
            "ranking_method": validator.EXPECTED_RANKING_METHOD,
            "global_supplement_ranking_method": validator.EXPECTED_GLOBAL_RANKING_METHOD,
            "us_equity_method": "conservative confirmed lower bound",
            "contract_benchmark_method": "latest prospectus",
            "us_main_exclude_keywords": ["亚洲", "中国", "港"],
            "global_exclude_keywords": [],
            "exclude_fund_types": ["QDII-商品", "QDII-混合债", "QDII-纯债"],
            "exclude_asset_classes": ["bond", "commodity"],
            "share_class": "OTC RMB A or explicit RMB primary share without C/D marker",
            "purchasable_only": True,
        },
        "cache": {
            "nasdaq100_benchmark": {"cache_hits": 1, "fetches": 2, "fallbacks": 0},
            "performance": {"hits": 42, "misses": 0, "corrupt_rebuilds": 0},
            "fund_us_equity_exposures": {
                "hits": 27,
                "misses": 0,
                "corrupt_rebuilds": 0,
            },
            "announcement_pdfs": {
                "hits": 0,
                "downloads": 0,
                "corrupt_redownloads": 0,
            },
            "underlying_exposures": {"hits": 0, "misses": 0},
        },
        "benchmark": {
            "symbol": "XNDX",
            "name": "NASDAQ-100 Total Return",
            "return_type": "gross_total_return",
            "currency": "CNY",
            "window_years": 3,
            "frequency": "weekly",
            "max_source_staleness_days": 7,
            "min_observations": 140,
            "min_span_days": 1000,
            "index_source_url": "https://example.test/xndx",
            "fx_source_url": "https://example.test/usd-cny",
            "index_start_date": "2023-08-06",
            "index_latest_date": "2026-08-19",
            "fx_start_date": "2023-08-06",
            "fx_latest_date": "2026-08-19",
        },
        "records": [make_record(rank) for rank in range(1, 4)],
        "global_supplement": {
            "ranking_method": validator.EXPECTED_GLOBAL_RANKING_METHOD,
            "qualified_count": 3,
            "records": sorted(
                [make_global_record(rank) for rank in range(1, 4)],
                key=lambda item: -float(item["return_drawdown_ratio"]),
            ),
        },
        "exclusion_summary": [
            {
                "reason": "direct_limit_below_threshold",
                "label": "直销额度低于 200 元",
                "count": 2,
                "codes": ["000099", "000100"],
            }
        ],
        "warnings": [
            "跳过未完整披露的持有人报告期 2026-06-30：覆盖率不足。",
            "000001 Sample ETF 无法按 2026-06-30 的可用数据穿透，其 0.50% 仓位仅计入可能上限。",
            "000099 美股占比区间 49.00%-51.00% 跨越 50% 阈值，按确认下限进入全球补充榜。",
        ],
        "sources": {},
    }


def write_artifacts(root, payload):
    output_dir = root / "output"
    publish_dir = root / "public"
    ranking.write_json(output_dir / "latest.json", payload)
    ranking.write_csv(output_dir / "latest.csv", payload)
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
        self.assertEqual(3, len(payload["records"]))
        self.assertEqual(3, len(payload["global_supplement"]["records"]))
        self.assertEqual(3, len(warnings))

    def test_rejects_missing_three_year_history_warning(self):
        payload = make_payload()
        payload["warnings"].append(
            "000001 的净值历史不足 3 年，近三年涨幅和最大回撤无法计算。"
        )
        with self.assertRaisesRegex(validator.ValidationError, "Blocking warnings"):
            self.validate(payload)

    def test_accepts_revalidated_cross_source_nav_lag_warning(self):
        payload = make_payload()
        payload["warnings"].append(
            "000001 的基金主页净值已更新至 2026-08-20，完整复权净值历史仍为 "
            "2026-08-19；已强制重新验证完整历史并按后者计算。"
        )
        _validated, warnings = self.validate(payload)
        self.assertTrue(any("强制重新验证完整历史" in item for item in warnings))

    def test_accepts_record_shortfall(self):
        payload = make_payload()
        payload["records"].pop()
        validated, _ = self.validate(payload)
        self.assertEqual(2, len(validated["records"]))

    def test_accepts_missing_long_history_and_holding_cost(self):
        payload = make_payload()
        record = payload["records"][0]
        for prefix in ("five_year", "ten_year"):
            record[f"{prefix}_return_pct"] = None
            record[f"{prefix}_performance_start_date"] = None
            record[f"{prefix}_performance_end_date"] = None
        record["holding_cost"] = {
            "status": "unavailable",
            "annualized_pct": None,
            "measurement_date": None,
            "source_title": None,
            "source_published_date": None,
            "source_url": None,
        }
        payload["warnings"].append("持有费率告警 000001：产品概要无法解析。")
        validated, _ = self.validate(payload)
        self.assertIsNone(validated["records"][0]["ten_year_return_pct"])

    def test_rejects_available_long_return_below_conditional_threshold(self):
        for field, value in (
            ("five_year_return_pct", 59.99),
            ("ten_year_return_pct", 99.99),
        ):
            with self.subTest(field=field):
                payload = make_payload()
                payload["records"][0][field] = value
                with self.assertRaisesRegex(
                    validator.ValidationError, "conditional .* return threshold"
                ):
                    self.validate(payload)

    def test_scale_is_validated_but_not_an_eligibility_threshold(self):
        payload = make_payload()
        payload["records"][0]["scale_billion_cny"] = 0.01
        validated, _ = self.validate(payload)
        self.assertEqual(0.01, validated["records"][0]["scale_billion_cny"])

    def test_accepts_composite_contract_without_style_threshold(self):
        payload = make_payload()
        contract = payload["records"][0]["contract_benchmark"]
        second = deepcopy(contract["components"][0])
        second.update(
            benchmark_id="hang-seng",
            benchmark_name="恒生指数",
            weight_pct=20.0,
            market_scope="excluded",
            market_label="中国香港",
            excluded_target=True,
        )
        contract.update(
            status="composite",
            benchmark_id=None,
            benchmark_name="纳斯达克100指数 + 恒生指数",
            benchmark_weight_pct=None,
            market_scope="composite",
            market_label="复合市场",
            asset_class="mixed",
            style_label="复合风格",
            components=[contract["components"][0], second],
        )
        validated, _ = self.validate(payload)
        self.assertEqual("composite", validated["records"][0]["contract_benchmark"]["status"])

    def test_accepts_excluded_target_flag_as_display_only_metadata(self):
        payload = make_payload()
        contract = payload["records"][0]["contract_benchmark"]
        contract.update(
            benchmark_id="csi-hk-us-china-technology",
            benchmark_name="中证香港美国上市中美科技指数",
            benchmark_text="中证香港美国上市中美科技指数收益率×85%+活期存款利率×15%",
            benchmark_weight_pct=85.0,
            market_scope="excluded",
            market_label="中国及中国香港",
            excluded_target=True,
        )
        contract["components"][0].update(
            benchmark_id="csi-hk-us-china-technology",
            benchmark_name="中证香港美国上市中美科技指数",
            weight_pct=85.0,
            market_scope="excluded",
            market_label="中国及中国香港",
            excluded_target=True,
        )
        validated, _ = self.validate(payload)
        self.assertTrue(validated["records"][0]["contract_benchmark"]["excluded_target"])

    def test_accepts_geography_override_above_us_threshold_in_global_list(self):
        payload = make_payload()
        record = payload["global_supplement"]["records"][0]
        record["name"] = "富国中国精选混合(QDII)人民币A"
        record["routing_reason"] = "us_main_name_geography_override"
        record["us_equity_exposure"].update(
            confirmed_pct=80.0,
            possible_pct=82.0,
            direct_us_pct=80.0,
            unresolved_pct=2.0,
            status="qualified",
        )
        validated, _ = self.validate(payload)
        self.assertEqual(
            "us_main_name_geography_override",
            validated["global_supplement"]["records"][0]["routing_reason"],
        )

    def test_rejects_global_record_above_us_threshold_without_override(self):
        payload = make_payload()
        record = payload["global_supplement"]["records"][0]
        record["us_equity_exposure"].update(confirmed_pct=80.0, possible_pct=82.0)
        with self.assertRaisesRegex(validator.ValidationError, "without an override"):
            self.validate(payload)

    def test_rejects_geography_override_without_matching_name(self):
        payload = make_payload()
        record = payload["global_supplement"]["records"][0]
        record["routing_reason"] = "us_main_name_geography_override"
        with self.assertRaisesRegex(validator.ValidationError, "no matching name keyword"):
            self.validate(payload)

    def test_rejects_us_main_name_with_geography_keyword(self):
        payload = make_payload()
        payload["records"][0]["name"] = "国富亚洲机会股票(QDII)A"
        with self.assertRaisesRegex(validator.ValidationError, "cannot enter the US main"):
            self.validate(payload)

    def test_rejects_contract_target_exclusion_reason(self):
        payload = make_payload()
        payload["exclusion_summary"].append(
            {
                "reason": "excluded_target_market",
                "label": "以中国、香港或泛亚洲为主要目标",
                "count": 1,
                "codes": ["016701"],
            }
        )
        with self.assertRaisesRegex(
            validator.ValidationError, "Contract benchmark metadata must not exclude"
        ):
            self.validate(payload)

    def test_rejects_both_lists_empty(self):
        payload = make_payload()
        payload["records"] = []
        payload["global_supplement"]["records"] = []
        with self.assertRaisesRegex(validator.ValidationError, "Both ranking lists are empty"):
            self.validate(payload)

    def test_rejects_incomplete_full_scan(self):
        payload = make_payload()
        payload["filters"]["full_scan_completed"] = False
        with self.assertRaisesRegex(validator.ValidationError, "Full scan"):
            self.validate(payload)

    def test_rejects_incorrect_order(self):
        payload = make_payload()
        payload["records"][0]["nasdaq100_fit"]["correlation"] = 0.5
        with self.assertRaisesRegex(validator.ValidationError, "sort rule"):
            self.validate(payload)

    def test_rejects_missing_or_incomplete_nasdaq_fit(self):
        payload = make_payload()
        payload["records"][0]["nasdaq100_fit"]["observations"] = 139
        with self.assertRaisesRegex(validator.ValidationError, "insufficient Nasdaq-100"):
            self.validate(payload)

    def test_rejects_unknown_quota(self):
        payload = make_payload()
        payload["records"][0]["quota_status"] = "unknown"
        payload["records"][0]["quota_confidence"] = "low"
        payload["records"][0]["direct_limit"] = {
            "status": "unknown",
            "amount_cny": None,
        }
        with self.assertRaisesRegex(validator.ValidationError, "direct limit is unresolved"):
            self.validate(payload)

    def test_rejects_direct_limit_below_inclusive_threshold(self):
        payload = make_payload()
        payload["records"][0]["direct_limit"]["amount_cny"] = 199
        with self.assertRaisesRegex(validator.ValidationError, "inclusive direct-sale"):
            self.validate(payload)

    def test_accepts_direct_limit_equal_to_inclusive_threshold(self):
        payload = make_payload()
        payload["records"][0]["direct_limit"]["amount_cny"] = 200
        validated, _ = self.validate(payload)
        self.assertEqual(200, validated["records"][0]["direct_limit"]["amount_cny"])

    def test_accepts_unknown_agency_limit(self):
        payload = make_payload()
        payload["records"][0]["agency_limit"] = {
            "status": "unknown",
            "amount_cny": None,
            "effective_date": None,
            "source_url": None,
            "confidence": "low",
        }
        payload["records"][0]["quota_status"] = "unknown"
        validated, _ = self.validate(payload)
        self.assertEqual("unknown", validated["records"][0]["agency_limit"]["status"])

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

    def test_rejects_stale_benchmark(self):
        payload = make_payload()
        payload["benchmark"]["index_latest_date"] = "2026-08-01"
        with self.assertRaisesRegex(validator.ValidationError, "source is stale"):
            self.validate(payload)

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
            with self.assertRaisesRegex(validator.ValidationError, "US exposure confirmed_pct"):
                validator.validate_local_artifacts(output_dir, publish_dir, RUN_DATE)

    def test_rejects_csv_nasdaq_metric_difference(self):
        with TemporaryDirectory() as directory:
            output_dir, publish_dir = write_artifacts(Path(directory), make_payload())
            path = output_dir / "latest.csv"
            content = path.read_text(encoding="utf-8-sig").replace(
                ",0.99,1.01,", ",0.5,1.01,", 1
            )
            path.write_text(content, encoding="utf-8-sig")
            with self.assertRaisesRegex(validator.ValidationError, "nasdaq100_correlation"):
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


if __name__ == "__main__":
    unittest.main()
