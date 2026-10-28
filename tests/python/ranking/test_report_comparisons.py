import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import update_qdii_ranking as ranking


def index_payload():
    return {
        "schema_version": 1,
        "funds": {
            "000043": {
                "name": "嘉实美国成长股票人民币",
                "list_url": "report-comparisons/000043/",
                "comparisons": [
                    {
                        "id": "2026-q2-vs-q3",
                        "title": "2026年二季度 vs 三季度",
                        "generated_at": "2026-10-27T12:00:00+08:00",
                        "summary": "组合和收益变化摘要 <需要转义>",
                        "periods": [
                            {
                                "label": "2026年二季度报告",
                                "report_date": "2026-06-30",
                                "published_date": "2026-07-21",
                                "source_url": "https://example.test/q2.pdf",
                            },
                            {
                                "label": "2026年三季度报告",
                                "report_date": "2026-09-30",
                                "published_date": "2026-10-27",
                                "source_url": "https://example.test/q3.pdf",
                            },
                        ],
                        "detail_url": "report-comparisons/000043/2026-q2-vs-q3.html",
                    }
                ],
            }
        },
    }


class ReportComparisonContractTests(unittest.TestCase):
    def test_missing_index_is_empty(self):
        with TemporaryDirectory() as directory:
            index = ranking.load_report_comparison_index(Path(directory) / "index.json")
        self.assertEqual({"schema_version": 1, "funds": {}}, index)

    def test_comparisons_are_sorted_by_latest_report_date(self):
        payload = index_payload()
        older = dict(payload["funds"]["000043"]["comparisons"][0])
        older["id"] = "2026-q1-vs-q2"
        older["title"] = "2026年一季度 vs 二季度"
        older["periods"] = [
            {**period, "report_date": "2026-03-31" if index == 0 else "2026-06-30"}
            for index, period in enumerate(older["periods"])
        ]
        payload["funds"]["000043"]["comparisons"].append(older)
        normalized = ranking.validate_report_comparison_index(payload)
        self.assertEqual(
            ["2026-q2-vs-q3", "2026-q1-vs-q2"],
            [item["id"] for item in normalized["funds"]["000043"]["comparisons"]],
        )

    def test_rejects_duplicate_id_and_unsafe_paths(self):
        payload = index_payload()
        duplicate = dict(payload["funds"]["000043"]["comparisons"][0])
        payload["funds"]["000043"]["comparisons"].append(duplicate)
        with self.assertRaises(ranking.ReportComparisonError):
            ranking.validate_report_comparison_index(payload)

        payload = index_payload()
        payload["funds"]["000043"]["list_url"] = "https://evil.example/"
        with self.assertRaises(ranking.ReportComparisonError):
            ranking.validate_report_comparison_index(payload)

        payload = index_payload()
        payload["funds"]["000043"]["comparisons"][0]["detail_url"] = "report-comparisons/000043/../evil.html"
        with self.assertRaises(ranking.ReportComparisonError):
            ranking.validate_report_comparison_index(payload)

        payload = index_payload()
        payload["funds"]["000043"]["comparisons"][0]["periods"][0]["source_url"] = "http://example.test/q2.pdf"
        with self.assertRaises(ranking.ReportComparisonError):
            ranking.validate_report_comparison_index(payload)

    def test_list_page_escapes_content_and_links_sources(self):
        normalized = ranking.validate_report_comparison_index(index_payload())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "index.html"
            ranking.render_report_comparison_list(path, normalized["funds"]["000043"])
            document = path.read_text(encoding="utf-8")
        self.assertIn("2026年二季度 vs 三季度", document)
        self.assertIn("组合和收益变化摘要 &lt;需要转义&gt;", document)
        self.assertIn("https://example.test/q2.pdf", document)
        self.assertIn("target=\"_blank\" rel=\"noopener noreferrer\"", document)
        self.assertNotIn("<需要转义>", document)

    def test_render_pages_writes_only_nonempty_funds(self):
        payload = index_payload()
        payload["funds"]["016701"] = {
            "name": "空记录基金",
            "list_url": "report-comparisons/016701/",
            "comparisons": [],
        }
        normalized = ranking.validate_report_comparison_index(payload)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ranking.render_report_comparison_pages(root, normalized)
            self.assertTrue((root / "report-comparisons" / "000043" / "index.html").is_file())
            self.assertFalse((root / "report-comparisons" / "016701" / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
