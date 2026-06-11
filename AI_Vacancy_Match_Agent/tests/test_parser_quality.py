from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fetch_vacancies import main as fetch_main
from sources.quality import (
    deduplicate_vacancies,
    explain_quality_issues,
    is_noisy_vacancy,
    prepare_vacancies_for_output,
)


class ParserQualityTests(unittest.TestCase):
    def test_deduplicate_uses_title_company_city_link_signature(self) -> None:
        vacancies = [
            {"title": "Junior Analyst", "company": "Acme", "city": "Moscow", "link": "https://example.com/vacancy/1", "description": "SQL"},
            {"title": "Junior Analyst", "company": "Acme", "city": "Moscow", "link": "https://example.com/vacancy/1?query=x", "description": "SQL Python"},
            {"title": "Junior Analyst", "company": "Beta", "city": "Moscow", "link": "https://example.com/vacancy/1", "description": "SQL"},
        ]

        unique = deduplicate_vacancies(vacancies)

        self.assertEqual(len(unique), 2)
        self.assertEqual(unique[0]["description"], "SQL Python")

    def test_noisy_rows_are_dropped_or_marked(self) -> None:
        noisy = {
            "title": "Apply",
            "company": "",
            "link": "https://example.com/search",
            "description": 'class="x" data-id="1" svg path fill-rule onclick javascript filter sort login',
        }
        good = {
            "title": "Junior Data Analyst",
            "company": "Acme",
            "city": "Moscow",
            "salary_rub": "от 120 000 руб.",
            "link": "https://example.com/vacancy/2",
            "description": "SQL Python dashboards",
        }

        kept, report = prepare_vacancies_for_output([noisy, good])

        self.assertTrue(is_noisy_vacancy(noisy))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], "Junior Data Analyst")
        self.assertEqual(report["total_rows"], 2)
        self.assertEqual(report["kept_rows"], 1)
        self.assertEqual(report["dropped_rows"], 1)
        self.assertTrue(report["dropped_examples"])

    def test_quality_report_contains_requested_metrics(self) -> None:
        rows = [
            {"source": "hh-html", "title": "Junior Analyst", "company": "Acme", "link": "https://hh.ru/vacancy/1", "description": "SQL Python"},
            {"source": "hh-html", "title": "Apply", "link": "https://hh.ru/search/vacancy", "description": "filter sort cookie"},
        ]

        _, report = prepare_vacancies_for_output(rows)

        for key in (
            "total_rows",
            "kept_rows",
            "dropped_rows",
            "noisy_rows",
            "noisy_share",
            "quality_by_source",
            "field_quality",
            "examples_of_noise",
            "dropped_examples",
            "warnings",
        ):
            self.assertIn(key, report)
        self.assertIn("hh-html", report["quality_by_source"])

    def test_bad_salary_is_reported_before_cleaning(self) -> None:
        issues = explain_quality_issues(
            {
                "title": "Junior Analyst",
                "company": "Acme",
                "salary_rub": "12345 67890 tracking numbers",
                "link": "https://example.com/vacancy/1",
                "description": "SQL Python",
            }
        )

        self.assertIn("invalid_salary", issues)

    def test_fetch_pipeline_writes_quality_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = fetch_main(
                [
                    "--sources",
                    "unknown_source",
                    "--text",
                    "junior analyst",
                    "--max-vacancies",
                    "1",
                    "--output-dir",
                    tmp,
                    "--filename",
                    "vacancies.csv",
                    "--no-llm-html",
                ]
            )
            trace_path = Path(tmp) / "vacancies.trace.json"
            quality_path = Path(tmp) / "vacancies.quality.json"

            self.assertEqual(code, 0)
            self.assertTrue(trace_path.exists())
            self.assertTrue(quality_path.exists())
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertIn("quality_report", trace)
            self.assertEqual(trace["quality_report"]["total_rows"], 0)


if __name__ == "__main__":
    unittest.main()
