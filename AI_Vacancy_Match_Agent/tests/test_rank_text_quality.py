from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
	sys.path.insert(0, str(SRC_ROOT))

from agent import explain_vacancies
from scorer import score_vacancies


class RankTextQualityTests(unittest.TestCase):
	def test_score_breakdown_uses_compact_evidence_for_dirty_format(self) -> None:
		criteria = {
			"target_roles": "Analyst",
			"preferred_levels": "Junior",
			"preferred_formats": "remote",
			"preferred_cities": "Москва",
			"skills": "SQL",
		}
		vacancies = [
			{
				"vacancy_id": "test-1",
				"title": "Analyst",
				"role": "Analyst",
				"level": "Junior",
				"format": "Сейчас смотрят Analyst до 90 000 ₽ за месяц, до вычета налогов Опыт 1- Компания Example 3.6 • Москва и еще 3",
				"city": "Москва",
				"salary_rub": "90 000 ₽",
				"description": "Junior analyst role with SQL and reporting tasks.",
				"link": "https://example.com",
			}
		]

		result = score_vacancies(vacancies, criteria)
		breakdown = result.scored_vacancies[0]["score_breakdown"]
		format_item = next(item for item in breakdown if item["criterion"] == "work_format_mismatch")

		self.assertLess(len(str(format_item["evidence"])), 150)
		self.assertNotIn("vacancyId", str(format_item["evidence"]))
		self.assertNotIn("Apply", str(format_item["evidence"]))

	def test_matched_criteria_are_localized_and_short(self) -> None:
		criteria = {
			"target_roles": "Analyst",
			"preferred_levels": "Junior",
			"preferred_formats": "remote",
			"preferred_cities": "Москва",
			"skills": "SQL",
		}
		vacancies = [
			{
				"vacancy_id": "test-2",
				"title": "Analyst",
				"role": "Analyst",
				"level": "Junior",
				"format": "remote",
				"city": "Москва",
				"salary_rub": "90 000 ₽",
				"description": "Junior analyst role with SQL and reporting tasks.",
				"link": "https://example.com",
			}
		]

		result = score_vacancies(vacancies, criteria)
		explanations = explain_vacancies(result.scored_vacancies, criteria, limit=1).explanations
		matched_criteria = explanations[0]["matched_criteria"]

		self.assertTrue(any(item.startswith("Совпадение роли:") for item in matched_criteria))
		self.assertTrue(any(item.startswith("Подходящий уровень:") for item in matched_criteria))
		self.assertTrue(any(item.startswith("Совпадение навыков:") for item in matched_criteria))
		self.assertFalse(any("role_match" in item for item in matched_criteria))


if __name__ == "__main__":
	unittest.main()
