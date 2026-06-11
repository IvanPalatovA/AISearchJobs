from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sources.html_source import parse_generic_search_html
from sources.quality import clean_text, clean_vacancy_fields, explain_quality_issues


class HTMLCleaningTests(unittest.TestCase):
    def test_html_noise_is_removed_from_text(self) -> None:
        cleaned = clean_text(
            """
            <div class="card" data-id="1">
              <svg><path fill-rule="evenodd" /></svg>
              <button onclick="apply()">Apply</button>
              Junior Analyst SQL Python
            </div>
            """
        )

        self.assertIn("Junior Analyst", cleaned)
        self.assertNotIn("fill-rule", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("class=", cleaned)

    def test_navigation_title_is_not_kept(self) -> None:
        vacancy = clean_vacancy_fields(
            {
                "title": "Apply",
                "company": "Acme",
                "link": "https://example.com/vacancy/1",
                "description": "SQL analytics",
            }
        )

        self.assertEqual(vacancy["title"], "")
        self.assertIn("empty_title", explain_quality_issues(vacancy))

    def test_salary_is_kept_only_when_it_looks_like_salary(self) -> None:
        valid = clean_vacancy_fields({"title": "Analyst", "salary_rub": "от 120 000 руб.", "link": "https://example.com/vacancy/1"})
        invalid = clean_vacancy_fields({"title": "Analyst", "salary_rub": "123456 campaign id 78910", "link": "https://example.com/vacancy/2"})

        self.assertEqual(valid["salary_rub"], "от 120 000 руб.")
        self.assertEqual(invalid["salary_rub"], "")

    def test_parser_uses_card_boundary_not_whole_page(self) -> None:
        html = """
        <html><body>
        <nav>Filters Login Create resume</nav>
        <article class="vacancy-card">
          <a href="/vacancies/1">Junior Product Analyst</a>
          <a href="/companies/acme">Acme</a>
          <p>SQL Python dashboards</p>
        </article>
        <footer>Cookie Subscribe Footer Noise</footer>
        </body></html>
        """

        vacancies = parse_generic_search_html(
            html,
            source="habr",
            base_url="https://career.habr.com",
            link_patterns=(r'<a[^>]+href="(?P<href>/vacancies/(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.*?)</a>',),
            company_href_parts=("/companies/",),
            query="junior analyst",
            max_items=5,
        )

        self.assertEqual(len(vacancies), 1)
        self.assertIn("SQL Python", vacancies[0]["description"])
        self.assertNotIn("Create resume", vacancies[0]["description"])
        self.assertNotIn("Footer Noise", vacancies[0]["description"])


if __name__ == "__main__":
    unittest.main()
