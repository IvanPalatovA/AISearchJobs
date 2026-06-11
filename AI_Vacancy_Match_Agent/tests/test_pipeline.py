from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
	sys.path.insert(0, str(SRC_ROOT))

from data_loader import load_data
from agent import explain_vacancies
from main import _apply_filters, _normalize_vacancies, _parse_args
from scorer import rank_vacancies, score_vacancies
from validator import validate_data


class FakeLLMClient:
	enabled = True
	mode = "llm"
	model = "fake-model"
	reason = "test"

	def __init__(self) -> None:
		self.call_trace: list[dict[str, str]] = []

	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		self.call_trace.append({"stage": stage})
		if stage == "llm_match_score":
			first = payload["vacancies"][0]
			return {
				"items": [
					{
						"vacancy_id": first["vacancy_id"],
						"role_match": True,
						"role_points": 30,
						"matched_roles": ["Data Analyst"],
						"role_comment": "должность аналитика",
						"skills_points": 10,
						"matched_skills": ["SQL", "Python"],
						"missing_target_skills": ["Excel"],
						"skills_comment": "есть SQL и Python",
					}
				]
			}
		if stage == "calculate_score":
			first = payload["vacancies"][0]
			return {
				"items": [
					{
						"vacancy_id": first["vacancy_id"],
						"title": first["title"],
						"llm_adjustment": -3,
						"llm_comment": "LLM audit comment",
						"llm_score_risks": ["Проверить стек"],
					}
				]
			}
		if stage == "rank_vacancies":
			first = payload["ranked_vacancies"][0]
			return {
				"items": [
					{
						"vacancy_id": first["vacancy_id"],
						"title": first["title"],
						"llm_rank_comment": "LLM rank comment",
						"llm_rank_group": "top priority",
					}
				],
				"top5_summary": "Top-5 выглядит согласованно с критериями.",
			}
		if stage == "agent_explanation":
			first = payload["top_vacancies"][0]
			return {
				"explanations": [
					{
						"vacancy_id": first["vacancy_id"],
						"why_fit": "LLM why fit",
						"risks": ["LLM risk"],
						"what_to_improve": "LLM improve",
						"next_step": "LLM next step",
						"questions_to_employer": ["LLM question?"],
						"llm_comment": "LLM explanation comment",
					}
				]
			}
		return {}


class SlowAgentExplanationLLM:
	enabled = True
	mode = "llm"
	model = "slow-fake"
	base_url = "https://example.test"

	def __init__(self) -> None:
		self.call_trace: list[dict[str, object]] = []

	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		time.sleep(0.5)
		return {"explanations": []}


class RoleDenyingLLMClient(FakeLLMClient):
	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		self.call_trace.append({"stage": stage})
		if stage == "llm_match_score":
			first = payload["vacancies"][0]
			return {
				"items": [
					{
						"vacancy_id": first["vacancy_id"],
						"role_match": False,
						"role_points": -80,
						"matched_roles": [],
						"role_comment": "ошибочно не распознано",
						"skills_points": 0,
						"matched_skills": [],
						"missing_target_skills": [],
						"skills_comment": "",
					}
				]
			}
		return super().json_task(stage=stage, system_prompt=system_prompt, payload=payload)


class RoleOverclaimingLLMClient(FakeLLMClient):
	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		self.call_trace.append({"stage": stage})
		if stage == "llm_match_score":
			first = payload["vacancies"][0]
			return {
				"items": [
					{
						"vacancy_id": first["vacancy_id"],
						"role_match": True,
						"role_points": 40,
						"matched_roles": ["фронтенд разработчик", "frontend developer"],
						"role_comment": "ошибочно засчитано по React в описании",
						"skills_points": 0,
						"matched_skills": [],
						"missing_target_skills": [],
						"skills_comment": "",
					}
				]
			}
		return super().json_task(stage=stage, system_prompt=system_prompt, payload=payload)


class PipelineTests(unittest.TestCase):
	def setUp(self) -> None:
		self.data = load_data(PROJECT_ROOT / "vacancies.csv", PROJECT_ROOT / "criteria.csv")

	def test_criteria_profile_row_is_loaded(self) -> None:
		self.assertEqual(len(self.data.vacancies), 50)
		self.assertIn("Data Analyst", self.data.criteria["target_roles"])
		self.assertEqual(self.data.criteria["salary_missing_penalty"], "yes")

	def test_loader_sanitizes_existing_dirty_collected_vacancies(self) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			vacancies_path = Path(tmp) / "vacancies.csv"
			with vacancies_path.open("w", encoding="utf-8-sig", newline="") as file:
				writer = csv.DictWriter(
					file,
					fieldnames=[
						"vacancy_id",
						"title",
						"company",
						"role",
						"level",
						"format",
						"city",
						"relocation_possible",
						"published_at",
						"deadline",
						"salary_rub",
						"stack",
						"key_skills",
						"english_level",
						"link",
						"description",
					],
				)
				writer.writeheader()
				writer.writerow(
					{
						"vacancy_id": "hh-html:1",
						"title": "Junior product analyst",
						"company": "Acme",
						"role": "Junior product analyst",
						"level": "Junior",
						"format": 'onse?vacancyId=1&employerId=2"><div class="magritte-card">Apply</div>',
						"salary_rub": "180000",
						"link": "https://hh.ru/vacancy/1",
						"description": 'Откликнуться Сейчас смотрят 13 человек remote work <svg><path fill-rule="evenodd" d="M6.294 14.873c.467.53.989 1.123 1.704 1.123" /></svg> SQL analytics',
					}
				)
				writer.writerow(
					{
						"vacancy_id": "superjob-html:2",
						"title": "Business / Product Analyst для FinTech / Trading SaaS",
						"company": "Trading",
						"role": "Business / Product Analyst для FinTech / Trading SaaS",
						"level": "Junior",
						"format": "hybrid",
						"city": "Москва",
						"salary_rub": "от 150 000 ₽",
						"link": "https://www.superjob.ru/vakansii/business-51988828.html",
						"description": 'Syq scvrH f-test-badge undefined UheZe">Стандартный график Гибрид Исследовать поведение соискателей и работодателей. Откликнуться +7 915 235•••• 31 мая Business / Product Analyst для FinTech / Trading SaaS Добавить в избранное',
					}
				)
				writer.writerow(
					{
						"vacancy_id": "generic-html:3",
						"title": "Data analyst/ engineer",
						"company": "",
						"role": "Data analyst/ engineer",
						"level": "Junior",
						"format": '9C10.1556 6.49269 10.25 6.25264 10.25 6C10.25 5.74736 10.1556 5.50731 9.98039 5.33211C9.8146 5.16631 9.51708 5 9 5H7V7Z"> 1 зарплата 122 отзыва Перейти в каталог компаний Data analyst/ engineer от 200 000 руб.',
						"salary_rub": "от 200 000 руб.",
						"link": "https://example.com/vacancy/3",
						"description": '2 10C15.12 11.72 13.72 13.12 12 13.12C10.27 13.12 8.87 11.72 8.87 10Z" fill="currentColor" fill-opacity="0.500000" > Центральный федеральный округ,Москва Откликнуться Data analyst/ engineer от 200 000 руб.',
					}
				)

			data = load_data(vacancies_path, PROJECT_ROOT / "criteria.csv")

		self.assertEqual(data.vacancies[0]["format"], "remote")
		self.assertNotIn("vacancyId", data.vacancies[0]["format"])
		self.assertNotIn("Откликнуться", data.vacancies[0]["description"])
		self.assertNotIn("fill-rule", data.vacancies[0]["description"])
		self.assertIn("SQL analytics", data.vacancies[0]["description"])
		self.assertNotIn("Syq", data.vacancies[1]["description"])
		self.assertNotIn("f-test-badge", data.vacancies[1]["description"])
		self.assertNotIn("+7 915", data.vacancies[1]["description"])
		self.assertNotIn("Добавить в избранное", data.vacancies[1]["description"])
		self.assertIn("Исследовать поведение", data.vacancies[1]["description"])
		self.assertNotIn("9C10", data.vacancies[2]["format"])
		self.assertNotIn("fill=", data.vacancies[2]["description"])
		self.assertNotIn("Перейти в каталог компаний", data.vacancies[2]["format"])

	def test_validation_uses_dataset_aliases(self) -> None:
		valid_vacancies, report = validate_data(self.data.vacancies, self.data.criteria, self.data.load_warnings)
		self.assertEqual(len(valid_vacancies), 49)
		self.assertEqual(report.duplicates, 1)
		self.assertEqual(report.missing_links, 1)
		self.assertEqual(report.empty_fields, 1)
		self.assertEqual(report.broken_rows, 0)

	def test_scoring_runs_for_every_valid_vacancy(self) -> None:
		valid_vacancies, _ = validate_data(self.data.vacancies, self.data.criteria, self.data.load_warnings)
		normalized = _normalize_vacancies(valid_vacancies)
		filtered = _apply_filters(normalized, self.data.criteria)
		result = score_vacancies(filtered, self.data.criteria)

		self.assertEqual(len(result.scored_vacancies), 49)
		self.assertEqual(sum(1 for vacancy in filtered if vacancy["filter_passed"]), 39)
		self.assertEqual(result.scored_vacancies[0]["title"], "Стажер Data Analyst")
		self.assertGreaterEqual(result.scored_vacancies[0]["score"], 100)

	def test_senior_role_is_penalized(self) -> None:
		valid_vacancies, _ = validate_data(self.data.vacancies, self.data.criteria, self.data.load_warnings)
		normalized = _normalize_vacancies(valid_vacancies)
		result = score_vacancies(normalized, self.data.criteria)
		senior = next(vacancy for vacancy in result.scored_vacancies if vacancy["title"] == "Senior Data Scientist")

		self.assertLess(senior["score"], 0)
		self.assertIn("Senior / Lead / Middle", senior["concerns"])

	def test_allowed_middle_senior_levels_are_not_penalized(self) -> None:
		vacancies = [
			{
				"vacancy_id": "frontend-senior",
				"title": "Senior Frontend Developer",
				"role": "Senior Frontend Developer",
				"level": "Senior",
				"format": "onsite",
				"city": "Москва",
				"salary_rub": "275000",
				"link": "https://example.com/frontend-senior",
			},
			{
				"vacancy_id": "frontend-junior",
				"title": "Frontend-разработчик",
				"role": "Frontend-разработчик",
				"level": "Junior",
				"format": "onsite",
				"city": "Москва",
				"salary_rub": "160000",
				"link": "https://example.com/frontend-junior",
			},
		]
		criteria = {
			"target_roles": "фронтенд разработчик; frontend developer",
			"preferred_levels": "Middle; Senior",
			"preferred_formats": "office",
			"preferred_cities": "Москва",
			"min_salary": "160000",
		}

		result = score_vacancies(vacancies, criteria)
		senior = next(vacancy for vacancy in result.scored_vacancies if vacancy["vacancy_id"] == "frontend-senior")
		junior = next(vacancy for vacancy in result.scored_vacancies if vacancy["vacancy_id"] == "frontend-junior")

		self.assertNotIn("Senior / Lead / Middle", senior["concerns"])
		self.assertIn("level_match", [item["criterion"] for item in senior["score_breakdown"]])
		self.assertIn("role_match", [item["criterion"] for item in junior["score_breakdown"]])
		self.assertIn("level_mismatch", [item["criterion"] for item in junior["score_breakdown"]])

	def test_llm_match_scoring_cannot_drop_obvious_frontend_title_match(self) -> None:
		vacancies = [
			{
				"vacancy_id": "frontend-react",
				"title": "Frontend разработчик (React, TypeScript, Next.js)",
				"role": "Frontend разработчик (React, TypeScript, Next.js)",
				"level": "Junior",
				"work_format": "remote",
				"city": "Удаленно",
				"salary": "230000 ₽",
				"description": "React TypeScript Next.js",
				"filter_reasons": ["Формат работы вне предпочтений", "Формат работы вне предпочтений"],
				"link": "https://example.com/frontend-react",
			}
		]
		criteria = {
			"target_roles": "фронтенд разработчик; frontend developer",
			"preferred_levels": "Middle; Senior",
			"preferred_formats": "office",
			"preferred_cities": "Москва",
			"min_salary": "160000",
		}
		fake_llm = RoleDenyingLLMClient()

		result = score_vacancies(
			vacancies,
			criteria,
			llm_client=fake_llm,
			use_llm_match_scoring=True,
			apply_llm_review=False,
		)
		vacancy = result.scored_vacancies[0]

		self.assertIn("фронтенд разработчик", vacancy["matched_roles"])
		self.assertIn("llm_role_match", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertNotIn("LLM: должность не соответствует целевым ролям", " ".join(vacancy["concerns"]))
		self.assertEqual(vacancy["concerns"].count("Формат работы вне предпочтений"), 1)

	def test_non_frontend_management_title_is_not_treated_as_frontend(self) -> None:
		vacancies = [
			{
				"vacancy_id": "brand-monitor-head",
				"title": "Руководитель группы разработки",
				"role": "Руководитель группы разработки",
				"level": "Senior",
				"format": "onsite",
				"city": "Москва",
				"salary_rub": "385000",
				"description": "Команда работает над продуктом с React Native, TypeScript и REST API, но роль управленческая.",
				"requirements": "React Native, TypeScript, REST API",
				"link": "https://example.com/brand-monitor-head",
			}
		]
		criteria = {
			"target_roles": "фронтенд разработчик; frontend developer",
			"preferred_levels": "Middle; Senior",
			"preferred_formats": "office",
			"preferred_cities": "Москва",
			"min_salary": "160000",
		}

		result = score_vacancies(vacancies, criteria)
		vacancy = result.scored_vacancies[0]

		self.assertNotIn("role_match", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertIn("irrelevant_role", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertLess(vacancy["score"], 0)

	def test_llm_cannot_overclaim_frontend_role_from_stack_only(self) -> None:
		vacancies = [
			{
				"vacancy_id": "brand-monitor-head",
				"title": "Руководитель группы разработки",
				"role": "Руководитель группы разработки",
				"level": "Senior",
				"format": "onsite",
				"city": "Москва",
				"salary_rub": "385000",
				"description": "Руководить командой разработки. В продукте встречаются React Native, TypeScript и REST API.",
				"requirements": "React Native, TypeScript, REST API",
				"link": "https://example.com/brand-monitor-head",
			}
		]
		criteria = {
			"target_roles": "фронтенд разработчик; frontend developer",
			"preferred_levels": "Middle; Senior",
			"preferred_formats": "office",
			"preferred_cities": "Москва",
			"min_salary": "160000",
		}

		result = score_vacancies(
			vacancies,
			criteria,
			llm_client=RoleOverclaimingLLMClient(),
			use_llm_match_scoring=True,
			apply_llm_review=False,
		)
		vacancy = result.scored_vacancies[0]

		self.assertEqual(vacancy["matched_roles"], [])
		self.assertIn("llm_role_mismatch", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertNotIn("llm_role_match", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertLess(vacancy["score"], 0)

	def test_moscow_city_is_not_penalized_for_moscow_preference(self) -> None:
		vacancies = [
			{
				"vacancy_id": "ai-intern-moscow",
				"title": "AI Engineer Intern",
				"role": "AI Engineer Intern",
				"level": "Internship",
				"format": "hybrid",
				"city": "Москва",
				"salary_rub": "70000",
				"link": "https://example.com/ai-intern-moscow",
			}
		]
		criteria = {
			"target_roles": "AI engineer; NLP engineer; AI agent; CD engineer; LLM engineer",
			"preferred_levels": "Internship",
			"preferred_formats": "remote; hybrid",
			"preferred_cities": "Москва",
			"min_salary": "70000",
		}

		result = score_vacancies(vacancies, criteria)
		criteria_names = [item["criterion"] for item in result.scored_vacancies[0]["score_breakdown"]]

		self.assertIn("city", criteria_names)
		self.assertNotIn("city_mismatch", criteria_names)

	def test_clear_city_mismatch_gets_strong_penalty(self) -> None:
		vacancies = [
			{
				"vacancy_id": "ai-intern-chelyabinsk",
				"title": "AI Engineer Intern",
				"role": "AI Engineer Intern",
				"level": "Internship",
				"format": "hybrid",
				"city": "Челябинск",
				"link": "https://example.com/ai-intern-chelyabinsk",
			}
		]
		criteria = {
			"target_roles": "AI engineer",
			"preferred_levels": "Internship",
			"preferred_formats": "remote; hybrid",
			"preferred_cities": "Москва",
		}

		result = score_vacancies(vacancies, criteria)
		city_item = next(item for item in result.scored_vacancies[0]["score_breakdown"] if item["criterion"] == "city_mismatch")

		self.assertEqual(city_item["points"], -30)

	def test_senior_is_far_mismatch_for_internship_request(self) -> None:
		vacancies = [
			{
				"vacancy_id": "senior-ai",
				"title": "Senior AI Engineer",
				"role": "Senior AI Engineer",
				"level": "Senior",
				"format": "remote",
				"city": "Москва",
				"description": "Разработка LLM-сервисов и менторство стажеров.",
				"link": "https://example.com/senior-ai",
			}
		]
		criteria = {
			"target_roles": "AI engineer; LLM engineer",
			"preferred_levels": "Internship",
			"preferred_formats": "remote; hybrid",
			"preferred_cities": "Москва",
		}

		result = score_vacancies(vacancies, criteria)
		breakdown = result.scored_vacancies[0]["score_breakdown"]
		level_item = next(item for item in breakdown if item["criterion"] == "level_mismatch")

		self.assertEqual(level_item["points"], -80)
		self.assertNotIn("level_match", [item["criterion"] for item in breakdown])

	def test_llm_cannot_overclaim_ai_role_for_sales_enablement(self) -> None:
		vacancies = [
			{
				"vacancy_id": "sales-enablement",
				"title": "Стажер в команду Sales Enablement & Customer Service",
				"role": "Стажер в команду Sales Enablement & Customer Service",
				"level": "Internship",
				"format": "hybrid",
				"city": "Москва",
				"description": "Поддержка процессов продаж и customer service.",
				"link": "https://example.com/sales-enablement",
			}
		]
		criteria = {
			"target_roles": "AI engineer; NLP engineer; AI agent; CD engineer; LLM engineer",
			"preferred_levels": "Internship",
			"preferred_formats": "remote; hybrid",
			"preferred_cities": "Москва",
		}

		result = score_vacancies(
			vacancies,
			criteria,
			llm_client=RoleOverclaimingLLMClient(),
			use_llm_match_scoring=True,
			apply_llm_review=False,
		)
		vacancy = result.scored_vacancies[0]
		criteria_names = [item["criterion"] for item in vacancy["score_breakdown"]]

		self.assertIn("llm_role_mismatch", criteria_names)
		self.assertNotIn("llm_role_match", criteria_names)
		self.assertEqual(vacancy["matched_roles"], [])

	def test_frontend_skills_are_extracted_from_detail_text(self) -> None:
		vacancies = [
			{
				"vacancy_id": "frontend-geo",
				"title": "Frontend-разработчик",
				"role": "Frontend-разработчик",
				"level": "Senior",
				"format": "onsite",
				"city": "Москва",
				"salary_rub": "250000",
				"description": "Разработка интерфейсов для геоинформационных систем.",
				"requirements": "GeoJSON, OpenLayers, React, JavaScript/TypeScript, Hooks, Router, React-Effector, RxJS.",
				"link": "https://example.com/frontend-geo",
			}
		]
		criteria = {
			"target_roles": "фронтенд разработчик; frontend developer",
			"preferred_levels": "Senior",
			"preferred_formats": "office",
			"preferred_cities": "Москва",
		}

		result = score_vacancies(vacancies, criteria)
		skills = set(result.scored_vacancies[0]["vacancy_skills"])

		self.assertIn("GeoJSON", skills)
		self.assertIn("OpenLayers", skills)
		self.assertIn("React", skills)
		self.assertIn("TypeScript", skills)
		self.assertIn("RxJS", skills)

	def test_llm_layer_adds_metadata_without_changing_score(self) -> None:
		valid_vacancies, _ = validate_data(self.data.vacancies, self.data.criteria, self.data.load_warnings)
		normalized = _normalize_vacancies(valid_vacancies)
		filtered = _apply_filters(normalized, self.data.criteria)
		fake_llm = FakeLLMClient()

		rule_result = score_vacancies(filtered, self.data.criteria)
		rule_top_score = rule_result.scored_vacancies[0]["score"]
		llm_result = score_vacancies(filtered, self.data.criteria, llm_client=fake_llm)
		ranked = rank_vacancies(llm_result, self.data.criteria, llm_client=fake_llm)
		agent_output = explain_vacancies(ranked, self.data.criteria, limit=5, llm_client=fake_llm)

		self.assertEqual(llm_result.scored_vacancies[0]["score"], rule_top_score)
		self.assertEqual(llm_result.scored_vacancies[0]["llm_adjustment"], -3)
		self.assertEqual(ranked[0]["llm_rank_comment"], "LLM rank comment")
		self.assertTrue(agent_output.explanations[0]["llm_used"])
		self.assertEqual(agent_output.top_vacancies[0]["llm_risks"], ["LLM risk"])
		self.assertEqual(agent_output.top_vacancies[0]["llm_explanation_comment"], "LLM explanation comment")
		self.assertEqual(
			[call["stage"] for call in fake_llm.call_trace],
			["calculate_score", "rank_vacancies", "agent_explanation"],
		)

	def test_agent_explanation_deadline_falls_back_when_llm_hangs(self) -> None:
		old_deadline = os.environ.get("AGENT_EXPLANATION_DEADLINE_SECONDS")
		os.environ["AGENT_EXPLANATION_DEADLINE_SECONDS"] = "0.1"
		progress: list[tuple[int, int, str]] = []
		try:
			started_at = time.monotonic()
			output = explain_vacancies(
				[
					{
						"vacancy_id": "slow-1",
						"title": "Frontend Developer",
						"company": "Example",
						"score": 80,
						"reasons": ["Подходит роль"],
					}
				],
				self.data.criteria,
				limit=1,
				llm_client=SlowAgentExplanationLLM(),
				llm_progress_callback=lambda done, total, detail="": progress.append((done, total, detail)),
			)
		finally:
			if old_deadline is None:
				os.environ.pop("AGENT_EXPLANATION_DEADLINE_SECONDS", None)
			else:
				os.environ["AGENT_EXPLANATION_DEADLINE_SECONDS"] = old_deadline

		self.assertLess(time.monotonic() - started_at, 0.45)
		self.assertEqual(len(output.explanations), 1)
		self.assertEqual(progress[-1][0], 1)
		self.assertIn("таймаут", progress[-1][2])

	def test_llm_match_scoring_replaces_rule_based_role_and_skills(self) -> None:
		vacancies = [
			{
				"vacancy_id": "1",
				"title": "Data Analyst Intern",
				"role": "Data Analyst Intern",
				"level": "Internship",
				"format": "remote",
				"city": "Москва",
				"salary": "80000",
				"skills": "SQL",
				"link": "https://example.com/1",
			}
		]
		criteria = {
			"target_roles": "Data Analyst",
			"skills": "SQL; Python; Excel",
			"preferred_levels": "Internship",
			"preferred_formats": "remote",
			"preferred_cities": "Москва",
			"min_salary": "50000",
		}
		fake_llm = FakeLLMClient()

		result = score_vacancies(vacancies, criteria, llm_client=fake_llm, use_llm_match_scoring=True, apply_llm_review=False)
		vacancy = result.scored_vacancies[0]

		self.assertTrue(vacancy["llm_match_score_used"])
		self.assertIn("llm_role_match", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertIn("llm_skills_match", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertNotIn("role_match", [item["criterion"] for item in vacancy["score_breakdown"]])
		self.assertEqual([call["stage"] for call in fake_llm.call_trace], ["llm_match_score"])

	def test_missing_salary_can_be_penalized(self) -> None:
		vacancies = [
			{
				"vacancy_id": "salary-missing",
				"title": "Data Analyst Intern",
				"role": "Data Analyst Intern",
				"level": "Internship",
				"format": "remote",
				"city": "Москва",
				"skills": "SQL",
				"link": "https://example.com/salary-missing",
			}
		]
		criteria = {
			"target_roles": "Data Analyst",
			"skills": "SQL",
			"min_salary": "50000",
			"salary_missing_penalty": "yes",
			"criterion_importance": "min_salary:high",
		}

		result = score_vacancies(vacancies, criteria)
		breakdown = result.scored_vacancies[0]["score_breakdown"]

		self.assertIn({"criterion": "salary_missing", "points": -16, "evidence": "empty salary"}, breakdown)

	def test_top_k_cli_argument_is_supported(self) -> None:
		args = _parse_args(["--top-k", "7", "--dry-run"])

		self.assertEqual(args.top_k, 7)


if __name__ == "__main__":
	unittest.main()
