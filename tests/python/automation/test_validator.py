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

def make_otc_payload():
    payload = make_payload()
    record = make_record(1, "us_main")
    record.update(
        {
            "rank": 1,
            "ranking_list": "nasdaq100_otc",
            "routing_reason": "name_match",
            "name": "纳斯达克100联接人民币A",
            "two_year_return_pct": 48.2,
            "two_year_max_drawdown_pct": -19.4,
            "common_period_return_pct": 51.2,
            "common_period_max_drawdown_pct": -20.4,
            "common_period_performance_start_date": "2024-03-22",
            "common_period_performance_end_date": "2026-08-18",
            "common_period_error": None,
            "nasdaq100_fit_2y": {
                "correlation": 0.99,
                "beta": 0.95,
                "tracking_error_pct": 2.1,
                "observations": 103,
                "start_date": "2024-08-19",
                "end_date": RUN_DATE,
            },
            "nasdaq100_fit_common_period": {
                "correlation": 0.99,
                "beta": 0.95,
                "tracking_error_pct": 2.0,
                "observations": 120,
                "start_date": "2024-03-22",
                "end_date": "2026-08-18",
            },
        }
    )
    payload["nasdaq100_otc"] = {
        "ranking_method": validator.EXPECTED_NASDAQ100_OTC_RANKING_METHOD,
        "candidate_count": 1,
        "missing_fields": {
            "two_year_return": 0,
            "holding_cost": 0,
            "nasdaq100_fit_2y": 0,
            "common_period_return": 0,
            "common_period_max_drawdown": 0,
            "nasdaq100_fit_common_period": 0,
            "inception_date": 0,
        },
        "comparison_window": {
            "status": "available",
            "minimum_anchor_age_years": 1,
            "max_boundary_delay_days": 7,
            "anchor_inception_date": "2024-03-22",
            "anchor_funds": [{"code": record["code"], "name": record["name"]}],
            "start_date": "2024-03-22",
            "end_date": "2026-08-18",
            "comparable_count": 1,
            "error": None,
        },
        "records": [record],
    }
    return payload


class NasdaqValidatorTests(unittest.TestCase):
    def test_accepts_and_renders_otc_nasdaq100_section(self):
        payload = make_otc_payload()
        with TemporaryDirectory() as directory:
            output_dir, publish_dir = write_artifacts(Path(directory), payload)
            validated, _ = validator.validate_local_artifacts(
                output_dir, publish_dir, RUN_DATE
            )
        self.assertEqual(
            "纳斯达克100联接人民币A",
            validated["nasdaq100_otc"]["records"][0]["name"],
        )


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
        self.assertEqual(25, len(payload["exchange_premium"]["records"]))
        self.assertEqual(3, len(warnings))

    @staticmethod
    def boundary_payload():
        payload = make_payload()
        record = payload["records"][0]
        record.update({
            "inception_date": "2023-08-19", "nav_history_start_date": "2023-08-19",
            "three_year_performance_start_date": "2023-08-19",
            "three_year_boundary_shortfall_days": 1,
        })
        for prefix in ("five_year", "ten_year"):
            for suffix in ("return_pct", "performance_start_date", "performance_end_date"):
                record[f"{prefix}_{suffix}"] = None
        payload["warnings"].append(
            "三年边界容差 000001：成立日 2023-08-19；"
            "2023-08-19 至 2026-08-18，距完整三年少 1 天。"
        )
        return payload

    def test_accepts_boundary_artifacts_and_discloses_excluded_candidate(self):
        payload = self.boundary_payload()
        payload["warnings"].append(
            "三年边界容差 000099：成立日 2023-08-19；"
            "2023-08-19 至 2026-08-12，距完整三年少 7 天。"
        )
        _, warnings = self.validate(payload)
        self.assertEqual(5, len(warnings))
        self.assertIn(payload["warnings"][-1], mailer.material_notes(payload))

    def test_rejects_boundary_metadata_tampering(self):
        for value in (None, -1, 0, 2, 8, True, 1.0):
            with self.subTest(value=value):
                payload = self.boundary_payload()
                payload["records"][0]["three_year_boundary_shortfall_days"] = value
                with self.assertRaisesRegex(validator.ValidationError, "boundary"):
                    self.validate(payload)
        payload = self.boundary_payload()
        del payload["records"][0]["three_year_boundary_shortfall_days"]
        with self.assertRaisesRegex(validator.ValidationError, "boundary"):
            self.validate(payload)

    def test_rejects_missing_boundary_warning_and_inception_mismatch(self):
        payload = self.boundary_payload()
        payload["warnings"].pop()
        with self.assertRaisesRegex(validator.ValidationError, "boundary warning"):
            self.validate(payload)
        payload = self.boundary_payload()
        payload["records"][0]["inception_date"] = "2023-08-18"
        with self.assertRaisesRegex(validator.ValidationError, "boundary must start"):
            self.validate(payload)

    def test_rejects_invalid_boundary_warnings_for_excluded_candidates(self):
        valid = "三年边界容差 000099：成立日 2023-08-19；2023-08-19 至 2026-08-18，距完整三年少 1 天。"
        for warning in (
            valid.replace("少 1 天", "少 7 天"),
            valid.replace("少 1 天", "少 8 天"),
            valid.replace("成立日 2023-08-19", "成立日 2023-08-18"),
            valid.replace("2023-08-19", "2023-08-20").replace("少 1 天", "少 2 天"),
            "三年边界容差 000099：无法获取净值。",
        ):
            with self.subTest(warning=warning):
                payload = make_payload()
                payload["warnings"].append(warning)
                with self.assertRaises(validator.ValidationError):
                    self.validate(payload)

    def test_rejects_boundary_csv_or_html_omission(self):
        for artifact in ("latest.csv", "latest.html"):
            with self.subTest(artifact=artifact), TemporaryDirectory() as directory:
                payload = self.boundary_payload()
                output, public = write_artifacts(Path(directory), payload)
                path = output / artifact
                document = path.read_text(encoding="utf-8-sig")
                if artifact.endswith("csv"):
                    document = document.replace("three_year_boundary_shortfall_days", "removed")
                else:
                    document = document.replace(mailer.format_three_year_boundary(payload["records"][0]), "removed")
                    (public / "index.html").write_text(document, encoding="utf-8")
                path.write_text(document, encoding="utf-8")
                with self.assertRaisesRegex(validator.ValidationError, "boundary"):
                    validator.validate_local_artifacts(output, public, RUN_DATE)

    def test_global_boundary_annualization_uses_actual_days(self):
        payload = self.boundary_payload()
        record = payload["global_supplement"]["records"][0]
        boundary = payload["records"][0]
        for field in ("inception_date", "nav_history_start_date", "three_year_performance_start_date",
                      "three_year_boundary_shortfall_days"):
            record[field] = boundary[field]
        for prefix in ("five_year", "ten_year"):
            for suffix in ("return_pct", "performance_start_date", "performance_end_date"):
                record[f"{prefix}_{suffix}"] = None
        ratio, annualized = ranking.calculate_return_drawdown_ratio(record)
        record["return_drawdown_ratio"] = round(ratio, 4)
        record["three_year_annualized_return_pct"] = round(annualized, 2)
        expected = ((1 + record["three_year_return_pct"] / 100) ** (365 / 1095) - 1) * 100
        self.assertAlmostEqual(expected, annualized)
        payload["warnings"].append(
            f"三年边界容差 {record['code']}：成立日 {record['inception_date']}；"
            f"{mailer.format_three_year_boundary(record)}。"
        )
        self.validate(payload)

    def test_rejects_boundary_warning_contradicting_complete_record(self):
        payload = make_payload()
        payload["warnings"].append(
            "三年边界容差 000001：成立日 2023-08-19；2023-08-19 至 2026-08-18，距完整三年少 1 天。"
        )
        with self.assertRaisesRegex(validator.ValidationError, "boundary warning contradicts"):
            self.validate(payload)

    def test_accepts_non_blocking_exchange_premium_warning(self):
        payload = make_payload()
        payload["warnings"].append(
            "场内溢价告警：行情刷新失败，使用缓存或空值：temporary outage"
        )
        _validated, warnings = self.validate(payload)
        self.assertTrue(any(item.startswith("场内溢价告警：") for item in warnings))

    def test_accepts_stale_exchange_holding_cost_and_warning(self):
        payload = make_payload()
        payload["exchange_premium"]["records"][0]["holding_cost"]["status"] = "stale"
        payload["warnings"].append(
            "场内费率告警 513500：公告索引无法读取，使用上次费率：temporary outage"
        )
        _validated, warnings = self.validate(payload)
        self.assertTrue(any(item.startswith("场内费率告警 ") for item in warnings))

    def test_rejects_invalid_exchange_holding_cost(self):
        payload = make_payload()
        payload["exchange_premium"]["records"][0]["holding_cost"]["annualized_pct"] = None
        with self.assertRaisesRegex(validator.ValidationError, "holding cost"):
            self.validate(payload)

    def test_rejects_exchange_premium_formula_difference(self):
        payload = make_payload()
        payload["exchange_premium"]["records"][0]["premium_pct"] = 20.0
        payload["exchange_premium"]["records"][0]["source_discount_pct"] = -20.0
        with self.assertRaisesRegex(validator.ValidationError, "price/IOPV"):
            self.validate(payload)

    def test_rejects_missing_exchange_premium_code(self):
        payload = make_payload()
        payload["exchange_premium"]["records"].pop()
        with self.assertRaisesRegex(validator.ValidationError, "25 records"):
            self.validate(payload)

    def test_rejects_non_qdii_record_in_dynamic_premium_catalog(self):
        payload = make_payload()
        section = payload["exchange_premium"]
        section["refresh_url"] = ranking.exchange_premium_market_url()
        section["discovered_count"] = 25
        section["filtered_unavailable_count"] = 0
        section["group_order"] = ["指数型-海外股票"]
        for record in section["records"]:
            record["category"] = "qdii"
            record["fund_type"] = "指数型-海外股票"
            record["benchmark_group"] = "指数型-海外股票"
        section["records"][0]["fund_type"] = "指数型-股票"
        with self.assertRaisesRegex(validator.ValidationError, "QDII fund-type scope"):
            self.validate(payload)

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
            ("five_year_return_pct", 39.99),
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
