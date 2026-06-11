from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import threading
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
	sys.path.insert(0, str(SRC_ROOT))

from web_app import (
	CRITERIA_DIR,
	_clean_keywords,
	_clean_sources,
	_clean_source_priorities,
	_coerce_generated_criteria,
	_criteria_columns,
	_criteria_file_metadata,
	_csv_file_metadata,
	_fetch_status,
	_base_source_name,
	_normalize_quick_plan,
	_quick_fetch_filters,
	_sanitize_quick_keywords,
	_list_criteria_files,
	_list_filter_files,
	_list_vacancy_files,
	_run_fetch,
	_run_command,
	_run_email_digest,
	_run_relaxed_fetch_completion,
	_run_quick_search,
	_quick_search_plan,
	_run_staged_quick_fetch,
	_start_job,
	_job_status,
	_normalize_resend_from,
	_remember_telegram_user,
	_resolve_telegram_chat_id,
	_safe_criteria_path,
	_safe_filter_path,
	_safe_project_path,
	_sanitize_metadata_text,
	_staged_fetch_empty_error,
	_send_digest_email,
	_telegram_chat_subscriptions,
	_telegram_delete_subscription,
	_telegram_bot_info,
	_validate_criteria_csv_text,
)
import web_app
from llm_client import LLMClient


class WebAppTests(unittest.TestCase):
	def test_dropdown_lists_vacancy_files_only(self) -> None:
		files = _list_vacancy_files()
		paths = [item["path"] for item in files]

		self.assertIn("vacancies.csv", paths)
		self.assertNotIn("criteria.csv", paths)
		self.assertTrue(all(Path(path).name.startswith("vacancies") for path in paths))

	def test_vacancy_file_metadata_uses_filename_and_description(self) -> None:
		files = _list_vacancy_files()
		root_file = next(item for item in files if item["path"] == "vacancies.csv")

		self.assertEqual(root_file["filename"], "vacancies.csv")
		self.assertTrue(root_file["name"])
		self.assertIn("CSV-файл вакансий", root_file["description"])
		self.assertIn("created", root_file)

	def test_criteria_file_metadata_is_separate(self) -> None:
		metadata = _criteria_file_metadata()

		self.assertEqual(metadata["path"], "criteria.csv")
		self.assertEqual(metadata["filename"], "criteria.csv")
		self.assertTrue(metadata["name"])
		self.assertIn("CSV-файл критериев", metadata["description"])

	def test_custom_metadata_overrides_display_name_and_description(self) -> None:
		metadata = _csv_file_metadata(
			PROJECT_ROOT / "vacancies.csv",
			description="Default description",
			metadata={"vacancies.csv": {"name": "Мой список", "description": "Мое описание"}},
		)

		self.assertEqual(metadata["name"], "Мой список")
		self.assertEqual(metadata["filename"], "vacancies.csv")
		self.assertEqual(metadata["description"], "Мое описание")

	def test_metadata_text_is_sanitized(self) -> None:
		self.assertEqual(_sanitize_metadata_text("  First\nSecond   Third  ", limit=20), "First Second Third")
		self.assertEqual(_sanitize_metadata_text("abcdef", limit=3), "abc")

	def test_ui_jobs_are_processed_fifo_on_backend(self) -> None:
		started = threading.Event()
		release = threading.Event()
		calls: list[str] = []

		def slow_rank(payload, *, job_id=None):
			calls.append("rank")
			started.set()
			self.assertTrue(release.wait(2))
			return {"ok": True, "card_vacancies": []}

		def quick(payload, *, job_id=None):
			calls.append("quick")
			return {"ok": True, "card_vacancies": []}

		with web_app.JOBS_LOCK:
			web_app.JOBS.clear()
			web_app.JOB_QUEUE.clear()
			web_app.JOB_WORKER_RUNNING = False
		try:
			with patch("web_app._run_rank", side_effect=slow_rank), patch("web_app._run_quick_search", side_effect=quick):
				first = _start_job("rank", {})
				self.assertTrue(started.wait(2))
				second = _start_job("quick", {})
				self.assertEqual(_job_status(second["job_id"])["status"], "queued")
				self.assertEqual(_job_status(second["job_id"])["queue_position"], 2)
				release.set()
				deadline = time.monotonic() + 2
				while time.monotonic() < deadline and _job_status(second["job_id"])["status"] != "done":
					time.sleep(0.02)

			self.assertEqual(calls, ["rank", "quick"])
			self.assertEqual(_job_status(first["job_id"])["status"], "done")
			self.assertEqual(_job_status(second["job_id"])["status"], "done")
		finally:
			release.set()
			with web_app.JOBS_LOCK:
				web_app.JOBS.clear()
				web_app.JOB_QUEUE.clear()
				web_app.JOB_WORKER_RUNNING = False

	def test_email_jobs_share_backend_queue(self) -> None:
		started = threading.Event()
		release = threading.Event()
		calls: list[str] = []

		def slow_rank(payload, *, job_id=None):
			calls.append("rank")
			started.set()
			self.assertTrue(release.wait(2))
			return {"ok": True, "card_vacancies": []}

		def email_job(subscription_id):
			calls.append(f"email:{subscription_id}")
			return {"ok": True, "sent": 0}

		with web_app.JOBS_LOCK:
			web_app.JOBS.clear()
			web_app.JOB_QUEUE.clear()
			web_app.JOB_WORKER_RUNNING = False
		try:
			with patch("web_app._run_rank", side_effect=slow_rank), patch("web_app._run_email_subscription_job", side_effect=email_job):
				first = _start_job("rank", {})
				self.assertTrue(started.wait(2))
				second = _start_job("email", {"subscription_id": "sub-1"})
				self.assertEqual(_job_status(second["job_id"])["status"], "queued")
				self.assertEqual(_job_status(second["job_id"])["queue_position"], 2)
				release.set()
				deadline = time.monotonic() + 2
				while time.monotonic() < deadline and _job_status(second["job_id"])["status"] != "done":
					time.sleep(0.02)

			self.assertEqual(calls, ["rank", "email:sub-1"])
			self.assertEqual(_job_status(first["job_id"])["status"], "done")
			self.assertEqual(_job_status(second["job_id"])["status"], "done")
		finally:
			release.set()
			with web_app.JOBS_LOCK:
				web_app.JOBS.clear()
				web_app.JOB_QUEUE.clear()
				web_app.JOB_WORKER_RUNNING = False

	def test_llm_client_default_timeout_is_900_seconds(self) -> None:
		with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
			client = LLMClient.from_env()

		self.assertTrue(client.enabled)
		self.assertEqual(client.timeout, 900)

	def test_criteria_dropdown_lists_criteria_files(self) -> None:
		files = _list_criteria_files()
		paths = [item["path"] for item in files]

		self.assertIn("criteria.csv", paths)
		self.assertTrue(all(Path(path).name.startswith("criteria") for path in paths))

	def test_filter_dropdown_lists_filter_files_only(self) -> None:
		files = _list_filter_files()
		paths = [item["path"] for item in files]

		self.assertIn("filters.csv", paths)
		self.assertNotIn("criteria.csv", paths)
		self.assertTrue(all(Path(path).name.startswith("filters") for path in paths))

	def test_safe_project_path_allows_vacancies_not_criteria(self) -> None:
		self.assertIsNotNone(_safe_project_path("vacancies.csv"))
		self.assertIsNone(_safe_project_path("criteria.csv"))

	def test_safe_criteria_path_allows_criteria_not_vacancies(self) -> None:
		self.assertIsNotNone(_safe_criteria_path("criteria.csv"))
		self.assertIsNotNone(_safe_criteria_path("data/criteria/criteria_generated.csv"))
		self.assertIsNone(_safe_criteria_path("vacancies.csv"))

	def test_safe_filter_path_allows_filters_not_criteria(self) -> None:
		self.assertIsNotNone(_safe_filter_path("filters.csv"))
		self.assertIsNotNone(_safe_filter_path("data/filters/filters_generated.csv"))
		self.assertIsNone(_safe_filter_path("criteria.csv"))

	def test_generated_criteria_file_appears_in_dropdown(self) -> None:
		CRITERIA_DIR.mkdir(parents=True, exist_ok=True)
		path = CRITERIA_DIR / "criteria_test_web_app.csv"
		path.write_text(
			"target_roles,target_roles_use_description,preferred_levels,preferred_formats,preferred_cities,skills,min_salary,salary_missing_penalty,english_level,stop_words,criterion_importance\n"
			"Data Analyst,,Junior,remote,Москва,SQL,80000,yes,A2,Senior,min_salary:low\n",
			encoding="utf-8-sig",
		)
		try:
			paths = [item["path"] for item in _list_criteria_files()]
		finally:
			path.unlink(missing_ok=True)

		self.assertIn("data/criteria/criteria_test_web_app.csv", paths)

	def test_generated_criteria_unspecified_values_are_ignored(self) -> None:
		criteria = _coerce_generated_criteria(
			{
				"target_roles": "Data Analyst",
				"preferred_formats": "не указано",
				"preferred_cities": "без предпочтений",
				"min_salary": "от 80 000 рублей",
				"salary_missing_penalty": "да",
			}
		)

		self.assertEqual(criteria["target_roles"], "Data Analyst")
		self.assertEqual(criteria["preferred_formats"], "")
		self.assertEqual(criteria["preferred_cities"], "")
		self.assertEqual(criteria["min_salary"], "80000")
		self.assertEqual(criteria["salary_missing_penalty"], "yes")

	def test_filter_columns_include_service_hard_filters(self) -> None:
		columns = _criteria_columns("filter")

		self.assertIn("search_fields", columns)
		self.assertIn("salary_defined", columns)
		self.assertIn("working_hours", columns)
		self.assertIn("employment_contract", columns)
		self.assertIn("accredited_it", columns)
		self.assertNotIn("target_roles", columns)
		self.assertNotIn("target_roles_use_description", columns)
		self.assertNotIn("skills", columns)
		self.assertNotIn("salary_missing_penalty", columns)

	def test_criteria_csv_validation_requires_expected_columns(self) -> None:
		self.assertEqual(
			_validate_criteria_csv_text(
				"target_roles,preferred_levels,preferred_formats,preferred_cities,skills,min_salary,english_level,stop_words\n"
				"Data Analyst,Junior,remote,Москва,SQL,80000,A2,Senior\n"
			),
			"",
		)
		self.assertIn("не хватает колонок", _validate_criteria_csv_text("target_roles\nData Analyst\n"))

	def test_fetch_status_reports_html_fallback_as_partial(self) -> None:
		status = _fetch_status(
			returncode=0,
			rows=10,
			trace_summary={
				"request_log": [
					{"source": "hh", "method": "api", "status": 403, "ok": False},
					{"source": "hh", "method": "html", "status": 200, "ok": True},
				]
			},
		)

		self.assertTrue(status["ok"])
		self.assertEqual(status["status"], "partial")
		self.assertEqual(status["status_label"], "HTML fallback")

	def test_fetch_status_is_success_when_target_rows_reached_despite_failed_requests(self) -> None:
		status = _fetch_status(
			returncode=0,
			rows=50,
			trace_summary={
				"query_limits": {"junior analyst": 50},
				"request_log": [
					{"source": "hh", "method": "html", "status": 200, "ok": True},
					{"source": "rabota_ru", "method": "html", "status": "TimeoutError", "ok": False},
				],
			},
		)

		self.assertTrue(status["ok"])
		self.assertEqual(status["status"], "success")
		self.assertEqual(status["status_label"], "Готово")
		self.assertEqual(status["failed_requests"], 1)

	def test_base_source_name_removes_parse_suffixes(self) -> None:
		self.assertEqual(_base_source_name("hh-html"), "hh")
		self.assertEqual(_base_source_name("geekjob-llm-html"), "geekjob")
		self.assertEqual(_base_source_name("geekjob-json"), "geekjob")
		self.assertEqual(_base_source_name("rabota_ru"), "rabota_ru")

	def test_fetch_status_treats_zero_rows_as_error(self) -> None:
		status = _fetch_status(returncode=0, rows=0, trace_summary={"request_log": []})

		self.assertFalse(status["ok"])
		self.assertEqual(status["status"], "empty")

	def test_clean_sources_allows_configured_job_boards_without_fallback(self) -> None:
		sources = _clean_sources(["hh", "superjob", "rabota_ru", "avito", "zarplata", "gorodrabot", "jooble", "habr", "geekjob", "trudvsem"])

		self.assertEqual(
			sources,
			["hh", "superjob", "rabota_ru", "avito", "zarplata", "gorodrabot", "jooble", "habr", "geekjob", "trudvsem"],
		)
		self.assertEqual(_clean_sources([]), [])
		self.assertEqual(_clean_sources(None), [])
		self.assertEqual(_clean_sources("hh,superjob"), ["hh", "superjob"])
		self.assertEqual(_clean_sources(["unknown"]), [])

	def test_clean_source_priorities_keeps_selected_sources_only(self) -> None:
		priorities = _clean_source_priorities({"hh": "high", "jooble": "low", "habr": "bad"}, ["hh", "habr"])

		self.assertEqual(priorities, {"hh": "high", "habr": "medium"})

	def test_clean_keywords_deduplicates_and_trims(self) -> None:
		keywords = _clean_keywords([" junior analyst ", "Junior Analyst", "data intern"])

		self.assertEqual(keywords, ["junior analyst", "data intern"])

	def test_digest_email_uses_resend_even_when_gmail_env_exists(self) -> None:
		class FakeResponse:
			def __enter__(self):
				return self

			def __exit__(self, exc_type, exc, tb):
				return False

			def read(self) -> bytes:
				return b'{"id":"email_test"}'

		requests = []

		def fake_urlopen(request, timeout=30):
			requests.append(request)
			return FakeResponse()

		env = {
			"RESEND_API": "resend_test_key",
			"RESEND_FROM": "Jobs <jobs@example.com>",
			"GMAIL_REFRESH_TOKEN": "gmail_token_that_must_be_ignored",
		}
		with patch.dict(os.environ, env, clear=True), patch("web_app.urlopen", fake_urlopen):
			result = _send_digest_email(
				{"emails": ["user@example.com"], "text": "junior python", "email_theme": "dark"},
				[{"title": "Python Intern", "company": "Example", "score": 90}],
			)

		self.assertTrue(result["ok"])
		self.assertEqual(requests[0].full_url, "https://api.resend.com/emails")
		payload = json.loads(requests[0].data.decode("utf-8"))
		self.assertIn("#0f1722", payload["html"])
		self.assertIn("supported-color-schemes", payload["html"])

	def test_resend_from_accepts_verified_domain_shortcut(self) -> None:
		self.assertEqual(_normalize_resend_from("mail.superjobsearch.ru"), "Vacancy Finder <digest@mail.superjobsearch.ru>")
		self.assertEqual(_normalize_resend_from("Jobs <jobs@example.com>"), "Jobs <jobs@example.com>")
		self.assertEqual(_normalize_resend_from("jobs@example.com"), "jobs@example.com")

	def test_telegram_bot_info_keeps_configured_link_when_api_fails(self) -> None:
		env = {
			"TELEGRAM_BOT_TOKEN": "invalid-token",
			"TELEGRAM_BOT_URL": "https://t.me/AISearchJobBot",
		}
		with patch.dict(os.environ, env, clear=True), patch("web_app._telegram_api_request", return_value={"ok": False, "error": "HTTPError: HTTP Error 404: Not Found"}):
			result = _telegram_bot_info()

		self.assertTrue(result["ok"])
		self.assertEqual(result["username"], "AISearchJobBot")
		self.assertEqual(result["bot_url"], "https://t.me/AISearchJobBot")

	def test_telegram_chat_subscriptions_filters_by_chat_id(self) -> None:
		subscriptions = [
			{"id": "own", "text": "own", "telegram_recipients": ["12345"]},
			{"id": "other", "text": "other", "telegram_recipients": ["67890"]},
			{"id": "username", "text": "username", "telegram_recipients": ["@alice"]},
		]
		with patch("web_app._public_email_subscriptions", return_value=subscriptions):
			result = _telegram_chat_subscriptions("12345", username="alice")

		self.assertEqual([item["id"] for item in result], ["own", "username"])

	def test_telegram_username_resolution_survives_restart_cache(self) -> None:
		data: dict[str, object] = {"subscriptions": []}

		def fake_load() -> dict[str, object]:
			return dict(data)

		def fake_write(next_data: dict[str, object]) -> None:
			data.clear()
			data.update(next_data)

		with patch("web_app._load_email_subscription_data_unlocked", side_effect=fake_load), \
			patch("web_app._write_email_subscription_data_unlocked", side_effect=fake_write), \
			patch("web_app._telegram_api_request") as api_request:
			_remember_telegram_user("12345", username="PalatovIvan")
			self.assertEqual(_resolve_telegram_chat_id("token", "@PalatovIvan"), "12345")

		api_request.assert_not_called()

	def test_telegram_delete_subscription_does_not_delete_other_chat_subscription(self) -> None:
		subscriptions = [{"id": "other", "text": "other", "telegram_recipients": ["67890"]}]
		with patch("web_app._public_email_subscriptions", return_value=subscriptions), \
			patch("web_app._delete_email_subscription") as delete_subscription, \
			patch("web_app._telegram_send_control_panel"):
			_telegram_delete_subscription("token", "12345", "other")

		delete_subscription.assert_not_called()

	def test_fetch_payload_llm_mode_uses_llm_only_and_caps_limit(self) -> None:
		class FakeResult:
			returncode = 0
			stdout = "Created: data/collected/vacancies.csv\n"
			stderr = ""

		with patch("web_app._run_command", return_value=FakeResult()) as run_command:
			result = _run_fetch(
				{
					"sources": ["hh"],
					"keywords": ["analyst"],
					"max_vacancies": 120,
					"use_llm_html": True,
					"hard_filters": False,
				}
			)

		cmd = run_command.call_args.args[0]
		self.assertIn("--llm-only-html", cmd)
		self.assertNotIn("--no-llm-html", cmd)
		self.assertEqual(cmd[cmd.index("--max-vacancies") + 1], "50")
		self.assertIn("data/collected/vacancies.csv", result["created_path"])

	def test_fetch_payload_auto_mode_disables_llm_and_caps_limit(self) -> None:
		class FakeResult:
			returncode = 0
			stdout = "Created: data/collected/vacancies.csv\n"
			stderr = ""

		with patch("web_app._run_command", return_value=FakeResult()) as run_command:
			_run_fetch(
				{
					"sources": ["hh"],
					"keywords": ["analyst"],
					"max_vacancies": 500,
					"use_llm_html": False,
					"hard_filters": False,
				}
			)

		cmd = run_command.call_args.args[0]
		self.assertIn("--no-llm-html", cmd)
		self.assertNotIn("--llm-only-html", cmd)
		self.assertEqual(cmd[cmd.index("--max-vacancies") + 1], "50")

	def test_fetch_payload_defaults_to_auto_without_llm_html(self) -> None:
		class FakeResult:
			returncode = 0
			stdout = "Created: data/collected/vacancies.csv\n"
			stderr = ""

		with patch("web_app._run_command", return_value=FakeResult()) as run_command:
			_run_fetch(
				{
					"sources": ["hh"],
					"keywords": ["analyst"],
					"hard_filters": False,
				}
			)

		cmd = run_command.call_args.args[0]
		self.assertIn("--no-llm-html", cmd)
		self.assertNotIn("--llm-only-html", cmd)

	def test_run_command_times_out_silent_process(self) -> None:
		result = _run_command(
			[sys.executable, "-c", "import time; time.sleep(2)"],
			timeout=1,
		)

		self.assertEqual(result.returncode, 124)
		self.assertIn("Process timed out", result.stdout)

	def test_run_command_streams_progress_before_process_exit(self) -> None:
		updates: list[float] = []
		started = time.monotonic()

		with patch("web_app._update_job_from_output", side_effect=lambda *args, **kwargs: updates.append(time.monotonic())):
			result = _run_command(
				[
					sys.executable,
					"-u",
					"-c",
					"import time; print('[####--------------------] 20% Rank Vacancies', flush=True); time.sleep(1.2)",
				],
				job_id="stream-test",
				timeout=3,
			)

		self.assertEqual(result.returncode, 0)
		self.assertTrue(updates)
		self.assertLess(updates[0] - started, 1.0)

	def test_fetch_payload_quick_llm_mode_caps_at_50(self) -> None:
		class FakeResult:
			returncode = 0
			stdout = "Created: data/collected/vacancies.csv\n"
			stderr = ""

		with patch("web_app._run_command", return_value=FakeResult()) as run_command:
			_run_fetch(
				{
					"sources": ["hh"],
					"keywords": ["analyst"],
					"max_vacancies": 100,
					"use_llm_html": True,
					"llm_max_limit": 100,
					"hard_filters": False,
				}
			)

		cmd = run_command.call_args.args[0]
		self.assertIn("--llm-only-html", cmd)
		self.assertIn("--llm-max-cap", cmd)
		self.assertEqual(cmd[cmd.index("--max-vacancies") + 1], "50")
		self.assertEqual(cmd[cmd.index("--llm-max-cap") + 1], "100")

	def test_quick_search_runs_fetch_then_rank_with_generated_files(self) -> None:
		plan = {
			"keywords": ["junior analyst"],
			"hard_filters": {},
			"criteria": {},
			"max_vacancies": 100,
			"sources": ["hh"],
			"source_priorities": {"hh": "high"},
			"use_llm_html": True,
			"rank_mode": "llm",
			"top_k": 15,
			"filter_name": "filter",
			"filter_description": "filter",
			"criteria_name": "criteria",
			"criteria_description": "criteria",
		}
		with patch("web_app._quick_search_plan", return_value={"ok": True, "plan": plan}), \
			patch("web_app._write_quick_csv", side_effect=[PROJECT_ROOT / "data/filters/filters_test.csv", PROJECT_ROOT / "data/criteria/criteria_test.csv"]), \
			patch("web_app._run_staged_quick_fetch", return_value={"ok": True, "created_path": "data/collected/vacancies_staged.csv", "rows": 12, "trace_summary": {}, "source_breakdown": {}, "staged_fetch": []}) as staged_fetch, \
			patch("web_app._run_rank", return_value={"ok": True, "trace_summary": {"llm_used": True}, "card_vacancies": []}) as run_rank:
			result = _run_quick_search({"text": "junior analyst"})

		self.assertTrue(result["ok"])
		self.assertEqual(result["created_path"], "data/collected/vacancies_staged.csv")
		self.assertEqual(staged_fetch.call_count, 1)
		self.assertFalse(result["quick_plan"]["use_llm_html"])
		self.assertEqual(run_rank.call_args.args[0]["criteria"], "data/criteria/criteria_test.csv")
		self.assertEqual(run_rank.call_args.args[0]["mode"], "llm")
		self.assertEqual(run_rank.call_args.args[0]["top_k"], 15)

	def test_quick_plan_is_limited_to_hh_and_superjob(self) -> None:
		plan = _normalize_quick_plan(
			{
				"sources": ["hh", "superjob", "rabota_ru", "avito"],
				"source_priorities": {"hh": "low", "superjob": "high", "rabota_ru": "high"},
				"use_llm_html": True,
			}
		)

		self.assertEqual(plan["sources"], ["hh", "superjob"])
		self.assertEqual(plan["source_priorities"], {"hh": "low", "superjob": "high"})
		self.assertFalse(plan["use_llm_html"])
		self.assertEqual(plan["rank_mode"], "llm")

	def test_quick_keywords_remove_values_already_expressed_as_filters(self) -> None:
		keywords = _sanitize_quick_keywords(
			["фронтенд разработчик middle senior", "frontend developer Москва офис"],
			{"preferred_levels": "middle,senior", "preferred_formats": "office", "preferred_cities": "Москва"},
			{"target_roles": "фронтенд разработчик,frontend developer"},
			fallback="",
		)

		self.assertEqual(keywords, ["фронтенд разработчик", "frontend developer"])

	def test_quick_plan_prompt_tells_llm_not_to_duplicate_filters_in_keywords(self) -> None:
		class FakeLLM:
			enabled = True
			reason = "test"

			def __init__(self) -> None:
				self.system_prompt = ""
				self.payload = {}

			def json_task(self, *, stage, system_prompt, payload):
				self.system_prompt = system_prompt
				self.payload = payload
				return {
					"keywords": ["фронтенд разработчик middle senior"],
					"hard_filters": {"preferred_levels": "middle; senior", "preferred_cities": "Москва"},
					"criteria": {"target_roles": "фронтенд разработчик", "preferred_levels": "middle; senior"},
				}

		client = FakeLLM()
		with patch("web_app._load_env_file"), patch("web_app.LLMClient.from_env", return_value=client):
			result = _quick_search_plan("ищу фронтенд middle senior в Москве")

		self.assertTrue(result["ok"])
		self.assertIn("Never duplicate structured constraints inside keywords", client.system_prompt)
		self.assertIn("deduplication_rule", client.payload["planning_rules"])

	def test_quick_fetch_filters_keep_all_llm_filters_for_staged_collection(self) -> None:
		filters = _quick_fetch_filters(
			{
				"english_level": "B1",
				"preferred_formats": "remote",
				"preferred_levels": "middle; senior",
				"preferred_cities": "Москва",
				"min_salary": "160000",
				"salary_defined": "yes",
				"search_fields": "name",
				"stop_words": "тимлид",
			}
		)

		self.assertEqual(filters["english_level"], "B1")
		self.assertEqual(filters["preferred_formats"], "remote")
		self.assertEqual(filters["preferred_levels"], "middle; senior")
		self.assertEqual(filters["preferred_cities"], "Москва")
		self.assertEqual(filters["min_salary"], "160000")
		self.assertEqual(filters["salary_defined"], "yes")
		self.assertEqual(filters["search_fields"], "name")
		self.assertEqual(filters["stop_words"], "тимлид")

	def test_quick_search_uses_staged_fetch_result_for_ranking(self) -> None:
		plan = {
			"keywords": ["frontend developer"],
			"hard_filters": {"stop_words": "стажер"},
			"criteria": {},
			"max_vacancies": 100,
			"sources": ["hh"],
			"source_priorities": {"hh": "high"},
			"use_llm_html": False,
			"rank_mode": "dry_run",
			"top_k": 15,
			"filter_name": "filter",
			"filter_description": "filter",
			"criteria_name": "criteria",
			"criteria_description": "criteria",
		}
		with patch("web_app._quick_search_plan", return_value={"ok": True, "plan": plan}), \
			patch("web_app._write_quick_csv", side_effect=[PROJECT_ROOT / "data/filters/filters_test.csv", PROJECT_ROOT / "data/criteria/criteria_test.csv"]), \
			patch("web_app._run_staged_quick_fetch", return_value={"ok": True, "created_path": "data/collected/vacancies_staged.csv", "rows": 30, "trace_summary": {}, "source_breakdown": {}, "staged_fetch": []}) as staged_fetch, \
			patch("web_app._run_rank", return_value={"ok": True, "trace_summary": {}, "card_vacancies": []}) as run_rank:
			result = _run_quick_search({"text": "frontend developer"})

		self.assertTrue(result["ok"])
		self.assertEqual(staged_fetch.call_count, 1)
		self.assertEqual(run_rank.call_args.args[0]["vacancies"], "data/collected/vacancies_staged.csv")
		self.assertEqual(run_rank.call_args.args[0]["mode"], "llm")
		self.assertEqual(run_rank.call_args.args[0]["command_timeout"], 900)

	def test_quick_search_failure_returns_detailed_error_log(self) -> None:
		plan = {
			"keywords": ["frontend developer"],
			"hard_filters": {"preferred_formats": "office"},
			"criteria": {},
			"max_vacancies": 50,
			"sources": ["hh"],
			"source_priorities": {"hh": "high"},
			"use_llm_html": False,
			"rank_mode": "llm",
			"top_k": 15,
			"filter_name": "filter",
			"filter_description": "filter",
			"criteria_name": "criteria",
			"criteria_description": "criteria",
		}
		fetch_result = {
			"ok": False,
			"status": "empty",
			"status_label": "Данных нет",
			"error": "Staged fetch produced no unique vacancies. Request failures: hh/api: timed out",
			"trace_summary": {"warnings": ["hh timed out"], "request_log": [{"source": "hh", "method": "api", "ok": False, "error": "timed out"}]},
			"staged_fetch": [{"stage": "all", "filter_count": 1, "rows": 0, "added_rows": 0, "cumulative_rows": 0, "status": "empty", "error": "timed out"}],
		}
		with patch("web_app._quick_search_plan", return_value={"ok": True, "plan": plan}), \
			patch("web_app._write_quick_csv", side_effect=[PROJECT_ROOT / "data/filters/filters_test.csv", PROJECT_ROOT / "data/criteria/criteria_test.csv"]), \
			patch("web_app._run_staged_quick_fetch", return_value=fetch_result):
			result = _run_quick_search({"text": "frontend developer"})

		self.assertFalse(result["ok"])
		self.assertIn("debug_log", result)
		self.assertIn("quick fetch failed", "\n".join(result["debug_log"]))
		self.assertIn("Request failures", result["error"])
		self.assertIn("fetch_result", result["error_details"])

	def test_staged_quick_fetch_merges_unique_rows_and_stops_at_target(self) -> None:
		tmp_dir = PROJECT_ROOT / "data" / "collected"
		tmp_dir.mkdir(parents=True, exist_ok=True)
		first = tmp_dir / "vacancies_stage_test_1.csv"
		second = tmp_dir / "vacancies_stage_test_2.csv"
		header = "vacancy_id,source,title,company,role,level,format,city,relocation_possible,published_at,deadline,salary_rub,salary_text,payment_frequency,stack,key_skills,english_level,link,description,requirements,responsibilities,conditions,employment_type,employment_form,experience,schedule,working_hours,work_format,address,metro_stations,employer_name,agency_company,company_description,category,published_at_text,views_count,detail_source,raw_detail_text\n"
		first.write_text(header + "1,hh,Frontend A,Acme,,,,,,,,,,,,,,https://hh.ru/vacancy/1,,,,,,,,,,,,,,,,,,,,\n", encoding="utf-8-sig")
		second.write_text(
			header
			+ "1,hh,Frontend A,Acme,,,,,,,,,,,,,,https://hh.ru/vacancy/1,,,,,,,,,,,,,,,,,,,,\n"
			+ "2,hh,Frontend B,Beta,,,,,,,,,,,,,,https://hh.ru/vacancy/2,,,,,,,,,,,,,,,,,,,,\n",
			encoding="utf-8-sig",
		)
		results = [
			{"ok": True, "created_path": "data/collected/vacancies_stage_test_1.csv", "rows": 1, "trace_summary": {"request_log": [], "warnings": []}},
			{"ok": True, "created_path": "data/collected/vacancies_stage_test_2.csv", "rows": 2, "trace_summary": {"request_log": [], "warnings": []}},
		]
		try:
			with patch("web_app._run_fetch", side_effect=results) as run_fetch, \
				patch("web_app._write_quick_csv", return_value=PROJECT_ROOT / "data/filters/filters_stage_test.csv"):
				result = _run_staged_quick_fetch(
					plan={
						"keywords": ["frontend"],
						"max_vacancies": 2,
						"sources": ["hh"],
						"source_priorities": {"hh": "high"},
						"filter_name": "filter",
					},
					all_filters={"preferred_cities": "Москва", "preferred_levels": "Middle", "preferred_formats": "office"},
					all_filter_path=PROJECT_ROOT / "data/filters/filters_stage_test.csv",
				)
		finally:
			first.unlink(missing_ok=True)
			second.unlink(missing_ok=True)
			created = _safe_project_path(result.get("created_path", "")) if "result" in locals() else None
			if created:
				created.unlink(missing_ok=True)
				created.with_suffix(".trace.json").unlink(missing_ok=True)

		self.assertTrue(result["ok"])
		self.assertEqual(result["rows"], 2)
		self.assertEqual(run_fetch.call_count, 2)
		self.assertTrue(all("--no-details" not in call.args[0] for call in run_fetch.call_args_list))
		self.assertEqual(result["staged_fetch"][0]["added_rows"], 1)
		self.assertEqual(result["staged_fetch"][1]["added_rows"], 1)
		self.assertTrue(result["staged_fetch"][2]["skipped"])

	def test_staged_quick_fetch_writes_trace_and_reason_when_empty(self) -> None:
		with patch(
			"web_app._run_fetch",
			return_value={
				"ok": False,
				"status": "error",
				"status_label": "Ошибка запуска",
				"error": "source timeout",
				"created_path": "",
				"rows": 0,
				"trace_summary": {
					"request_log": [{"source": "hh", "method": "api", "ok": False, "error": "timed out"}],
					"warnings": ["hh fetch timed out"],
					"source_stats": {},
					"fetch_metrics": {},
				},
			},
		), patch("web_app._write_quick_csv", return_value=PROJECT_ROOT / "data/filters/filters_stage_test.csv"):
			result = _run_staged_quick_fetch(
				plan={"keywords": ["frontend"], "max_vacancies": 2, "sources": ["hh"], "source_priorities": {"hh": "high"}, "filter_name": "filter"},
				all_filters={"preferred_formats": "office"},
				all_filter_path=PROJECT_ROOT / "data/filters/filters_stage_test.csv",
			)
		trace_path = _safe_project_path(result.get("trace_path", "")) if result.get("trace_path") else None
		try:
			self.assertFalse(result["ok"])
			self.assertIn("Request failures", result["error"])
			self.assertTrue(trace_path and trace_path.exists())
		finally:
			if trace_path:
				trace_path.unlink(missing_ok=True)

	def test_run_fetch_returns_subprocess_error_details(self) -> None:
		with patch("web_app._run_command", return_value=subprocess.CompletedProcess(["python"], 1, stdout="ImportError: broken dependency", stderr="")):
			result = _run_fetch(
				{
					"sources": ["hh"],
					"keywords": ["frontend"],
					"max_vacancies": 1,
					"hard_filters": False,
				}
			)

		self.assertFalse(result["ok"])
		self.assertIn("fetch_vacancies failed with code 1", result["error"])
		self.assertIn("ImportError: broken dependency", result["error"])

	def test_staged_fetch_empty_error_prefers_request_failures(self) -> None:
		message = _staged_fetch_empty_error(
			request_log=[{"source": "hh", "method": "api", "ok": False, "error": "timed out"}],
			warnings=["hh fetch timed out"],
			last_error="",
		)

		self.assertIn("Request failures", message)

	def test_relaxed_fetch_completion_adds_rows_and_reports_notice(self) -> None:
		tmp_dir = PROJECT_ROOT / "data" / "collected"
		filter_dir = PROJECT_ROOT / "data" / "filters"
		tmp_dir.mkdir(parents=True, exist_ok=True)
		filter_dir.mkdir(parents=True, exist_ok=True)
		strict = tmp_dir / "vacancies_relaxed_test_strict.csv"
		relaxed = tmp_dir / "vacancies_relaxed_test_more.csv"
		filters = filter_dir / "filters_relaxed_test.csv"
		header = "vacancy_id,source,title,company,role,level,format,city,relocation_possible,published_at,deadline,salary_rub,salary_text,payment_frequency,stack,key_skills,english_level,link,description,requirements,responsibilities,conditions,employment_type,employment_form,experience,schedule,working_hours,work_format,address,metro_stations,employer_name,agency_company,company_description,category,published_at_text,views_count,detail_source,raw_detail_text\n"
		strict.write_text(header + "1,hh,Frontend A,Acme,,,,,,,,,,,,,,https://hh.ru/vacancy/1,,,,,,,,,,,,,,,,,,,,\n", encoding="utf-8-sig")
		relaxed.write_text(
			header
			+ "1,hh,Frontend A,Acme,,,,,,,,,,,,,,https://hh.ru/vacancy/1,,,,,,,,,,,,,,,,,,,,\n"
			+ "2,hh,Frontend B,Beta,,,,,,,,,,,,,,https://hh.ru/vacancy/2,,,,,,,,,,,,,,,,,,,,\n",
			encoding="utf-8-sig",
		)
		filters.write_text(
			"preferred_levels,preferred_formats,preferred_cities,min_salary,salary_defined,search_fields\n"
			"Middle,office,Москва,160000,yes,name\n",
			encoding="utf-8-sig",
		)
		try:
			with patch("web_app._run_fetch", return_value={"ok": True, "created_path": "data/collected/vacancies_relaxed_test_more.csv", "rows": 2, "trace_summary": {"request_log": [], "warnings": []}}) as run_fetch, \
				patch("web_app._write_quick_csv", return_value=filters):
				result = _run_relaxed_fetch_completion(
					payload={
						"sources": ["hh"],
						"source_priorities": {"hh": "high"},
						"keywords": ["frontend"],
						"max_vacancies": 2,
						"use_llm_html": False,
					},
					base_result={"ok": True, "status": "success", "created_path": "data/collected/vacancies_relaxed_test_strict.csv", "rows": 1, "trace_summary": {"request_log": [], "warnings": []}},
					criteria_path=filters,
					target_rows=2,
				)
		finally:
			strict.unlink(missing_ok=True)
			relaxed.unlink(missing_ok=True)
			filters.unlink(missing_ok=True)
			created = _safe_project_path(result.get("created_path", "")) if "result" in locals() else None
			if created:
				created.unlink(missing_ok=True)
				created.with_suffix(".trace.json").unlink(missing_ok=True)

		self.assertTrue(result["ok"])
		self.assertTrue(result["relaxed_hard_filters"])
		self.assertIn("Недобор по жестким фильтрам", result["relaxed_notice"])
		self.assertEqual(result["rows"], 2)
		self.assertTrue(run_fetch.call_args.args[0]["_disable_relaxed_completion"])
		self.assertNotIn("--no-details", run_fetch.call_args.args[0])

	def test_email_digest_disables_llm_html_even_when_plan_enables_it(self) -> None:
		plan = {
			"keywords": ["junior analyst"],
			"hard_filters": {},
			"criteria": {},
			"max_vacancies": 100,
			"sources": ["hh"],
			"source_priorities": {"hh": "high"},
			"use_llm_html": True,
			"rank_mode": "dry_run",
			"top_k": 15,
			"filter_name": "filter",
			"filter_description": "filter",
			"criteria_name": "criteria",
			"criteria_description": "criteria",
		}
		subscription = {"id": "test", "text": "junior analyst", "k": 5, "sent_vacancy_keys": []}
		with patch("web_app._quick_search_plan", return_value={"ok": True, "plan": plan}), \
			patch("web_app._write_quick_csv", side_effect=[PROJECT_ROOT / "data/filters/filters_test.csv", PROJECT_ROOT / "data/criteria/criteria_test.csv"]), \
			patch("web_app._run_staged_quick_fetch", return_value={"ok": True, "created_path": "data/collected/vacancies.csv", "rows": 12, "trace_summary": {}, "source_breakdown": {}, "staged_fetch": []}) as staged_fetch, \
			patch("web_app._run_rank", return_value={"ok": True, "trace_path": "output/trace.json"}), \
			patch("web_app._rank_card_vacancies", return_value=[]):
			result = _run_email_digest(subscription)

		self.assertTrue(result["ok"])
		self.assertEqual(staged_fetch.call_count, 1)
		self.assertFalse(result["plan"]["use_llm_html"])


if __name__ == "__main__":
	unittest.main()
