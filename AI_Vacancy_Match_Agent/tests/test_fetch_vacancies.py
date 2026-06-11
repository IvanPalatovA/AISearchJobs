from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
	sys.path.insert(0, str(SRC_ROOT))

from sources import generic_html_source, hh_source, superjob_source
from sources.base import VACANCY_COLUMNS
from sources.generic_html_source import fetch_generic_html_vacancies
from sources.html_source import merge_auto_and_llm_vacancies, parse_generic_search_html, parse_hh_detail_html, parse_hh_html, parse_html_with_llm, parse_superjob_detail_html, parse_superjob_html
from sources.llm_extract_pipeline import HTMLCleaner, LLMVacancyExtractor, VacancyNormalizer, source_from_url
from fetch_vacancies import _allocate_query_limits, _allocate_source_limits, _fetch_parse_metrics, _parse_queries, _parse_source_priorities, _source_fetch_window


HH_HTML = """
<html><body>
<div class="vacancy-card">
  <a class="serp-item__title" href="https://hh.ru/vacancy/12345?query=junior">Junior Data Analyst</a>
  <a href="/employer/42">Data Company</a>
  <span>Москва</span>
  <span>от 80 000 руб.</span>
  <p>Python SQL Excel</p>
</div>
</body></html>
"""

HH_MAGRITTE_HTML = """
<html><body>
<div data-qa="vacancy-serp__vacancy" class="magritte-card">
  <a href="https://hh.ru/vacancy/111?query=junior">Previous Analyst</a>
  <a href="response?vacancyId=111&employerId=1"><span>Apply</span></a>
  <svg><path fill-rule="evenodd" clip-rule="evenodd" d="M6.294 14.873c.467.53.989 1.123 1.704 1.123"></path></svg>
</div>
<div data-qa="vacancy-serp__vacancy" class="magritte-card">
  <a href="https://hh.ru/vacancy/222?query=junior">Junior product analyst</a>
  <a href="/employer/3529">Acme Analytics</a>
  <span>remote up to 180 000 RUB. Experience 1-3 years.</span>
  <a href="response?vacancyId=222&employerId=3529"><span>Apply</span></a>
  <svg><path fill-rule="evenodd" clip-rule="evenodd" d="M6.294 14.873c.467.53.989 1.123 1.704 1.123"></path></svg>
</div>
</body></html>
"""

SUPERJOB_HTML = """
<html><body>
<div class="item">
  <a href="https://www.superjob.ru/vakansii/stazher-analitik-51961680.html">Стажер-аналитик</a>
  <a href="/clients/acme-1.html">Acme</a>
  <span>Москва</span>
  <span>60 000 ₽</span>
  <p>SQL, Excel, dashboards</p>
</div>
</body></html>
"""

HABR_HTML = """
<html><body>
<article class="vacancy-card">
  <a href="/vacancies/100500">Junior Product Analyst</a>
  <a href="/companies/acme">Acme Tech</a>
  <span>Москва</span>
  <span>120 000 ₽</span>
  <p>SQL Python dashboards hybrid</p>
</article>
</body></html>
"""

HABR_BACKDROP_HTML = """
<html><body>
<article class="vacancy-card">
  <a aria-label="Junior Product Analyst" class="vacancy-card__backdrop-link" href="/vacancies/100500"></a>
  <a class="vacancy-card__title-link" href="/vacancies/100500">Junior Product Analyst</a>
  <a href="/companies/acme">Acme Tech</a>
  <span>Москва</span>
  <p>SQL Python dashboards hybrid</p>
</article>
</body></html>
"""


class FakeLLMClient:
	enabled = True

	def __init__(self) -> None:
		self.call_trace = []

	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		self.call_trace.append({"stage": stage, "ok": True})
		return {
			"vacancies": [
				{
					"title": "LLM Parsed Analyst",
					"company": "LLM Company",
					"city": "Москва",
					"salary_rub": "70000 RUB",
					"link": "https://example.com/vacancy/1",
					"description": "SQL Python analytics",
					"experience_level": "junior",
					"work_format": "hybrid",
					"skills": ["SQL", "Python"],
				}
			]
		}


class ConcurrentFakeLLMClient(FakeLLMClient):
	def __init__(self) -> None:
		super().__init__()
		self._lock = threading.Lock()
		self._active = 0
		self.max_active = 0

	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		with self._lock:
			self._active += 1
			self.max_active = max(self.max_active, self._active)
		try:
			time.sleep(0.03)
			return super().json_task(stage=stage, system_prompt=system_prompt, payload=payload)
		finally:
			with self._lock:
				self._active -= 1


class EmptyThenStructuredLLMClient(FakeLLMClient):
	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		self.call_trace.append({"stage": stage, "ok": True, "payload": payload, "system_prompt": system_prompt})
		if len(self.call_trace) == 1:
			return {"vacancies": []}
		first_anchor = payload["anchor_inventory"][0]
		return {
			"vacancies": [
				{
					"title": first_anchor["title"],
					"company": "",
					"city": "",
					"salary_rub": "",
					"link": first_anchor["absolute_link"],
					"description": "SQL analytics",
				}
			]
		}


class MatchingHHLLMClient(FakeLLMClient):
	def json_task(self, *, stage: str, system_prompt: str, payload: dict) -> dict:
		self.call_trace.append({"stage": stage, "ok": True, "payload": payload, "system_prompt": system_prompt})
		return {
			"vacancies": [
				{
					"title": "Junior Data Analyst",
					"company": "Data Company",
					"city": "Москва",
					"salary_rub": "от 80 000 RUB",
					"link": "https://hh.ru/vacancy/12345",
					"description": "Подробно: Python SQL Excel, продуктовая аналитика, дашборды и A/B тесты.",
					"requirements": "SQL, Python, Excel",
					"responsibilities": "Готовить отчеты и анализировать метрики",
					"conditions": "Гибридный формат",
					"employment_type": "full-time",
					"skills": ["SQL", "Python", "Excel"],
					"work_format": "hybrid",
					"experience_level": "junior",
				}
			]
		}


class DisabledLLMClient:
	enabled = False
	call_trace: list[dict] = []


class FakeResponse:
	def __init__(self, body: str, status: int = 200) -> None:
		self.body = body.encode("utf-8")
		self.status = status

	def __enter__(self) -> "FakeResponse":
		return self

	def __exit__(self, exc_type, exc, tb) -> None:
		return None

	def read(self) -> bytes:
		return self.body


class DetailConcurrencyTracker:
	def __init__(self, detail_urls: set[str], *, search_html: str, detail_html: str) -> None:
		self.detail_urls = detail_urls
		self.search_html = search_html
		self.detail_html = detail_html
		self.lock = threading.Lock()
		self.active = 0
		self.max_active = 0
		self.detail_ready = threading.Event()

	def open(self, request, timeout):
		url = request.full_url
		if url in self.detail_urls:
			with self.lock:
				self.active += 1
				self.max_active = max(self.max_active, self.active)
				if self.active >= 2:
					self.detail_ready.set()
			self.detail_ready.wait(0.5)
			time.sleep(0.03)
			with self.lock:
				self.active -= 1
			return FakeResponse(self.detail_html, status=200)

		return FakeResponse(self.search_html, status=200)


class FetchVacanciesTests(unittest.TestCase):
	def test_parse_hh_html_extracts_vacancy(self) -> None:
		vacancies = parse_hh_html(HH_HTML, query="junior analyst", max_items=5)

		self.assertEqual(len(vacancies), 1)
		self.assertEqual(vacancies[0]["source"], "hh-html")
		self.assertEqual(vacancies[0]["title"], "Junior Data Analyst")
		self.assertEqual(vacancies[0]["company"], "Data Company")
		self.assertIn("hh.ru/vacancy/12345", vacancies[0]["link"])

	def test_parse_hh_html_keeps_modern_cards_clean(self) -> None:
		vacancies = parse_hh_html(HH_MAGRITTE_HTML, query="junior analyst", max_items=5)
		second = vacancies[1]

		self.assertEqual(len(vacancies), 2)
		self.assertEqual(second["title"], "Junior product analyst")
		self.assertEqual(second["company"], "Acme Analytics")
		self.assertEqual(second["format"], "remote")
		self.assertNotIn("Previous Analyst", second["description"])
		self.assertNotIn("response?vacancyId", second["description"])
		self.assertNotIn("fill-rule", second["description"])
		self.assertNotIn("magritte-card", second["format"])

	def test_parse_hh_html_extracts_clickme_card_with_direct_vacancy_link(self) -> None:
		html = """
		<html><body>
		<div data-qa="vacancy-serp__vacancy vacancy-serp-item_clickme">
		  <div id="133663948" class="vacancy-card">
		    <a data-qa="serp-item__title" href="https://adsrv.hh.ru/click?b=1&clickType=link_to_vacancy">
		      <span data-qa="serp-item__title-text">Менеджер по рекламе Яндекс Маркет, Wildberries/Аналитик роста продаж</span>
		    </a>
		    <span>от 110 000 ₽ за месяц, на руки</span>
		    <span data-qa="vacancy-serp__vacancy-work-experience-between1And3">Опыт 1-3 года</span>
		    <span data-qa="vacancy-serp__vacancy-compensation-frequency-TWICE_PER_MONTH">Выплаты: два раза в месяц</span>
		    <a href="/employer/42">ООО СанСтар</a>
		    <span>Москва</span>
		  </div>
		</div>
		</body></html>
		"""

		vacancies = parse_hh_html(html, query="аналитик", max_items=5)

		self.assertEqual(len(vacancies), 1)
		self.assertEqual(vacancies[0]["vacancy_id"], "hh-html:133663948")
		self.assertEqual(vacancies[0]["link"], "https://hh.ru/vacancy/133663948")
		self.assertEqual(vacancies[0]["payment_frequency"], "два раза в месяц")
		self.assertEqual(vacancies[0]["company"], "ООО СанСтар")
		self.assertNotIn("Выплаты:", vacancies[0]["description"])
		self.assertNotIn("Опыт 1-3", vacancies[0]["description"])

	def test_parse_hh_detail_html_keeps_structured_sections_and_raw_text(self) -> None:
		html = """
		<html><head><link rel="canonical" href="https://hh.ru/vacancy/133663948"/></head><body>
		  <h1 data-qa="vacancy-title">Менеджер по рекламе Яндекс Маркет, Wildberries/Аналитик роста продаж</h1>
		  <div data-qa="vacancy-salary">от 110 000 ₽ за месяц, на руки</div>
		  <a data-qa="vacancy-company-name" href="/employer/42">ООО СанСтар</a>
		  <p>Опыт работы: 1–3 года</p><p>Полная занятость</p><p>График: 5/2</p>
		  <p>Рабочие часы: 8</p><p>Формат работы: на месте работодателя</p>
		  <div data-qa="vacancy-description">
		    <p>Обязанности:</p><ul><li>Управление ДРР, ставками, бюджетами</li></ul>
		    <p>Требования:</p><ul><li>Excel / Google Таблицы — уверенный уровень</li></ul>
		    <p>Условия:</p><ul><li>Оформление по ТК РФ</li></ul>
		  </div>
		  <div>Ключевые навыки <span data-qa="bloko-tag__text">Аналитика продаж</span></div>
		  <div>Где предстоит работать Москва, Локомотив, Черкизовская, Щёлковское шоссе, 5с1</div>
		  <div>Вакансия опубликована 4 июня 2026 в Москве</div>
		</body></html>
		"""

		vacancy = parse_hh_detail_html(html, page_url="https://hh.ru/vacancy/133663948")

		self.assertIsNotNone(vacancy)
		assert vacancy is not None
		self.assertIn("Управление ДРР", vacancy["responsibilities"])
		self.assertIn("Excel", vacancy["requirements"])
		self.assertIn("Оформление по ТК РФ", vacancy["conditions"])
		self.assertEqual(vacancy["schedule"], "5/2")
		self.assertEqual(vacancy["working_hours"], "8")
		self.assertIn("Щёлковское шоссе", vacancy["raw_detail_text"])

	def test_parse_hh_detail_html_extracts_stack_and_sections_from_long_text(self) -> None:
		html = """
		<html><head><link rel="canonical" href="https://hh.ru/vacancy/133992398"/></head><body>
		  <h1 data-qa="vacancy-title">QA AI Engineer (Python)</h1>
		  <a data-qa="vacancy-company-name" href="/employer/42">Aston</a>
		  <div data-qa="vacancy-description">
		    <div><strong>Чего мы ждем от специалиста?</strong></div>
		    <ul>
		      <li>опыт работы в роли разработчика от 4 лет в проектах по разработке ПО с использованием инструментов и технологий Python.</li>
		      <li>опыт в AI/ML/NLP не менее 2 лет.</li>
		      <li>опыт работы с языковыми моделями (LLM) через API.</li>
		      <li>опыт использования библиотек для взаимодействия с LLM: LangChain, LangGraph, etc.</li>
		      <li>опыт работы с фреймворками Python: FastAPI/Django/Flask.</li>
		      <li>опыт разработки REST API.</li>
		      <li>опыт работы с реляционными БД PostgreSQL, нереляционными БД - к примеру, mongodb.</li>
		      <li>опыт работы с очередями сообщений (Kafka, RabbitMQ).</li>
		      <li>уверенный анализ логов, метрик (Prometheus/Grafana, ELK) и трейсинга (OpenTelemetry, Jaeger).</li>
		      <li>владение Python на уровне, достаточном для написания unit/load-тестов, скриптов автоматизации и работы с фреймворками (pytest, unittest, Locust и др.).</li>
		      <li>опыт работы с инструментами: Postman/Insomnia, curl, Charles/Wireshark, Docker, Git.</li>
		      <li>опыт работы с AI-инструментами разработки (GitHub Copilot, Cursor, Codeium).</li>
		    </ul>
		    <p><strong>Что мы предлагаем?</strong></p>
		    <ul>
		      <li>ДМС со стоматологией.</li>
		      <li>удаленный формат работы в офисе в Москве.</li>
		    </ul>
		    <div>Ключевые навыки</div>
		    <span data-qa="bloko-tag__text">Python</span>
		    <span data-qa="bloko-tag__text">SQL</span>
		  </div>
		</body></html>
		"""

		vacancy = parse_hh_detail_html(html, page_url="https://hh.ru/vacancy/133992398")

		self.assertIsNotNone(vacancy)
		assert vacancy is not None
		self.assertIn("опыт работы в роли разработчика от 4 лет", vacancy["requirements"])
		self.assertIn("ДМС со стоматологией", vacancy["conditions"])
		for needle in ("Python", "LangChain", "LangGraph", "FastAPI", "PostgreSQL", "Kafka", "RabbitMQ", "Prometheus", "OpenTelemetry", "pytest", "GitHub Copilot"):
			self.assertIn(needle, vacancy["stack"])
		self.assertIn("SQL", vacancy["stack"])

	def test_parse_superjob_html_extracts_vacancy(self) -> None:
		vacancies = parse_superjob_html(SUPERJOB_HTML, query="junior analyst", max_items=5)

		self.assertEqual(len(vacancies), 1)
		self.assertEqual(vacancies[0]["source"], "superjob-html")
		self.assertEqual(vacancies[0]["title"], "Стажер-аналитик")
		self.assertEqual(vacancies[0]["company"], "Acme")
		self.assertIn("superjob.ru/vakansii/stazher-analitik", vacancies[0]["link"])

	def test_parse_superjob_detail_html_extracts_app_state_vacancy(self) -> None:
		state = {
			"currentRoute": {"routeParams": {"vacancyId": "51999639"}, "pathname": "/vakansii/programmist-1s-51999639.html"},
			"entities": {
				"vacancy": {
					"51999639": {
						"id": "51999639",
						"type": "vacancy",
						"attributes": {},
						"relationships": {
							"mainInfo": {"data": {"id": "51999639", "type": "vacancyMainInfo"}},
							"detailInfo": {"data": {"id": "51999639", "type": "vacancyDetailInfo"}},
							"companyInfo": {"data": {"id": "51999639", "type": "vacancyCompanyInfo"}},
							"company": {"data": {"id": "4353589", "type": "company"}},
							"town": {"data": {"id": "4", "type": "town"}},
							"requiredExperience": {"data": {"id": "1", "type": "vacancyExperienceDictionary"}},
							"catalogues": {"data": [{"id": "48", "type": "catalogue"}]},
							"vacancyTags": {"data": [{"id": "18", "type": "vacancyTagType"}]},
						},
					}
				},
				"vacancyMainInfo": {
					"51999639": {
						"attributes": {"profession": "Программист 1С", "updatedAt": "2026-06-01T10:09:51+03:00", "minSalary": 100000, "maxSalary": 150000},
						"relationships": {"salary": {"data": {"id": "51999639", "type": "vacancySalary"}}},
					}
				},
				"vacancySalary": {
					"51999639": {
						"attributes": {"minSalary": 100000, "maxSalary": 150000, "paymentAgreement": False},
						"relationships": {"currency": {"data": {"id": "rub", "type": "currencyDictionary"}}, "salaryPeriod": {"data": {"id": "1", "type": "vacancyPaymentPeriodDictionary"}}},
					}
				},
				"currencyDictionary": {"rub": {"attributes": {"symbol": "₽"}}},
				"vacancyPaymentPeriodDictionary": {"1": {"attributes": {"defaultLabel": "Месяц"}}},
				"vacancyDetailInfo": {
					"51999639": {
						"attributes": {
							"fullTextPlain": "Что мы предлагаем:\\nКонкурентную заработную плату.\\nМы ждём от вас:\\nГотовность развиваться в сфере 1С.\\nВаши задачи:\\nДорабатывать конфигурации.\\nЗнание языка программирования Java обязательно",
							"isRemoteWork": False,
						},
						"relationships": {},
					}
				},
				"vacancyCompanyInfo": {"51999639": {"attributes": {"name": "ООО \"СЧТ\"", "description": "Описание Jobers"}}},
				"company": {"4353589": {"attributes": {"title": "Jobers", "createdAt": "2022-06-29T12:02:00+03:00"}, "relationships": {"countOfEmployee": {"data": {"id": "50", "type": "employeeCountDictionary"}}}}},
				"employeeCountDictionary": {"50": {"attributes": {"defaultLabel": "До 50 сотрудников"}}},
				"town": {"4": {"attributes": {"name": "Москва"}}},
				"vacancyExperienceDictionary": {"1": {"attributes": {"defaultLabel": "Опыт работы не требуется"}}},
				"catalogue": {"48": {"attributes": {"label": "Разработка, программирование"}}},
				"vacancyTagType": {"18": {"attributes": {"label": "Старт карьеры"}}},
			},
		}
		html = f"<html><body><script>window.APP_STATE={json.dumps(state, ensure_ascii=False)}</script></body></html>"

		vacancy = parse_superjob_detail_html(html, page_url="https://www.superjob.ru/vakansii/programmist-1s-51999639.html", vacancy_id="51999639")

		self.assertIsNotNone(vacancy)
		assert vacancy is not None
		self.assertEqual(vacancy["company"], "ООО \"СЧТ\"")
		self.assertEqual(vacancy["agency_company"], "Jobers")
		self.assertEqual(vacancy["salary_text"], "100 000 — 150 000 ₽/месяц")
		self.assertIn("Дорабатывать конфигурации", vacancy["responsibilities"])
		self.assertIn("Java обязательно", vacancy["raw_detail_text"])

	def test_hh_html_detail_enrichment_runs_in_parallel(self) -> None:
		detail_urls = {
			"https://hh.ru/vacancy/111",
			"https://hh.ru/vacancy/222",
		}
		lock = threading.Lock()
		active = 0
		max_active = 0
		ready = threading.Event()

		def fake_urlopen(request, timeout):
			nonlocal active, max_active
			url = request.full_url
			if "search/vacancy" in url:
				return FakeResponse("<html><body>search</body></html>", status=200)
			if url in detail_urls:
				with lock:
					active += 1
					max_active = max(max_active, active)
					if active >= 2:
						ready.set()
				ready.wait(0.5)
				time.sleep(0.03)
				with lock:
					active -= 1
				return FakeResponse(
					"""
					<html><head><title>Junior product analyst</title></head><body>
					<div data-qa="vacancy-description">
					  <p>Требования: SQL Python</p>
					  <p>Обязанности: Анализировать данные</p>
					</div>
					</body></html>
					""",
					status=200,
				)
			raise AssertionError(f"unexpected url: {url}")

		with patch.object(hh_source, "urlopen", side_effect=fake_urlopen), patch.object(
			hh_source,
			"parse_hh_html",
			return_value=[
				{"title": "Junior product analyst", "company": "Acme", "city": "Москва", "salary_rub": "80 000 ₽", "link": "https://hh.ru/vacancy/111", "source": "hh-html"},
				{"title": "Junior product analyst 2", "company": "Beta", "city": "Москва", "salary_rub": "90 000 ₽", "link": "https://hh.ru/vacancy/222", "source": "hh-html"},
			],
		):
			result = hh_source.fetch_hh_vacancies(
				text="junior analyst",
				area="1",
				per_page=5,
				pages=1,
				fetch_details=True,
				timeout=5,
				use_html=True,
				llm_client=None,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 2)
		self.assertGreater(max_active, 1)
		self.assertIn("SQL Python", result.vacancies[1]["description"])
		self.assertTrue(all("api" not in item["method"] for item in result.request_log))

	def test_superjob_html_detail_enrichment_runs_in_parallel(self) -> None:
		search_html = """
		<html><body>
		<div class="item">
		  <a href="https://www.superjob.ru/vakansii/stazher-analitik-51961680.html">Стажер-аналитик</a>
		  <a href="/clients/acme-1.html">Acme</a>
		  <span>Москва</span>
		  <span>60 000 ₽</span>
		</div>
		<div class="item">
		  <a href="https://www.superjob.ru/vakansii/analitik-51961681.html">Аналитик</a>
		  <a href="/clients/beta-2.html">Beta</a>
		  <span>Москва</span>
		  <span>90 000 ₽</span>
		</div>
		</body></html>
		"""
		tracker = DetailConcurrencyTracker(
			{
				"https://www.superjob.ru/vakansii/stazher-analitik-51961680.html",
				"https://www.superjob.ru/vakansii/analitik-51961681.html",
			},
			search_html=search_html,
			detail_html="""
			<html><head><title>Аналитик</title></head><body>
			Требования SQL Python. Задачи Анализировать данные.
			</body></html>
			""",
		)
		with patch.object(superjob_source, "urlopen", tracker.open):
			result = superjob_source.fetch_superjob_vacancies(
				text="analyst",
				town="4",
				count=5,
				pages=1,
				timeout=5,
				use_html=True,
				llm_client=DisabledLLMClient(),
				llm_only_html=True,
				max_items=5,
				fetch_details=True,
			)

		self.assertEqual(len(result.vacancies), 2)
		self.assertGreater(tracker.max_active, 1)
		self.assertIn("Москва", result.vacancies[0]["city"])

	def test_empty_html_detail_enrichment_does_not_create_zero_worker_pool(self) -> None:
		self.assertEqual(hh_source._enrich_hh_html_details([], timeout=5, fetch_details=True), ([], [], []))
		self.assertEqual(superjob_source._enrich_superjob_html_details([], timeout=5, fetch_details=True), ([], [], []))

	def test_parse_generic_search_html_extracts_habr_vacancy(self) -> None:
		vacancies = parse_generic_search_html(
			HABR_HTML,
			source="habr",
			base_url="https://career.habr.com",
			link_patterns=(r'<a[^>]+href="(?P<href>/vacancies/(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.*?)</a>',),
			company_href_parts=("/companies/",),
			query="junior analyst",
			max_items=5,
		)

		self.assertEqual(len(vacancies), 1)
		self.assertEqual(vacancies[0]["source"], "habr-html")
		self.assertEqual(vacancies[0]["title"], "Junior Product Analyst")
		self.assertEqual(vacancies[0]["company"], "Acme Tech")
		self.assertIn("career.habr.com/vacancies/100500", vacancies[0]["link"])

	def test_parse_generic_search_html_uses_aria_label_before_seen(self) -> None:
		vacancies = parse_generic_search_html(
			HABR_BACKDROP_HTML,
			source="habr",
			base_url="https://career.habr.com",
			link_patterns=(r'<a[^>]+href="(?P<href>/vacancies/(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.*?)</a>',),
			company_href_parts=("/companies/",),
			query="junior analyst",
			max_items=5,
		)

		self.assertEqual(len(vacancies), 1)
		self.assertEqual(vacancies[0]["title"], "Junior Product Analyst")

	def test_llm_fallback_extracts_vacancy_when_html_parser_fails(self) -> None:
		client = FakeLLMClient()
		vacancies = parse_html_with_llm(
			source="hh",
			html="<html><body>dynamic content</body></html>",
			query="junior analyst",
			page_url="https://hh.ru/search/vacancy",
			max_items=5,
			llm_client=client,
		)

		self.assertEqual(len(vacancies), 1)
		self.assertEqual(vacancies[0]["source"], "hh-llm-html")
		self.assertEqual(vacancies[0]["title"], "LLM Parsed Analyst")
		self.assertEqual(client.call_trace[0]["stage"], "hh_html_parse")

	def test_llm_search_parser_retries_empty_result_with_anchor_inventory(self) -> None:
		client = EmptyThenStructuredLLMClient()
		vacancies = parse_html_with_llm(
			source="hh",
			html=HH_HTML,
			query="junior analyst",
			page_url="https://hh.ru/search/vacancy",
			max_items=5,
			llm_client=client,
		)

		self.assertEqual(len(client.call_trace), 2)
		self.assertEqual(len(client.call_trace[0]["payload"]["anchor_inventory"]), 1)
		self.assertIn("SERP", client.call_trace[0]["payload"]["instruction"])
		self.assertEqual(len(vacancies), 1)
		self.assertEqual(vacancies[0]["title"], "Junior Data Analyst")

	def test_hh_fetch_uses_html_only(self) -> None:
		requested_urls: list[str] = []

		def fake_urlopen(request, timeout):
			self.assertNotIn("api.hh.ru", request.full_url)
			requested_urls.append(request.full_url)
			return FakeResponse(HH_HTML, status=200)

		with patch.object(hh_source, "urlopen", fake_urlopen):
			result = hh_source.fetch_hh_vacancies(
				text="junior analyst",
				area="1",
				per_page=5,
				pages=1,
				fetch_details=False,
				timeout=5,
				use_html=True,
				llm_client=None,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.vacancies[0]["source"], "hh-html")
		self.assertEqual(result.request_log[0]["method"], "html")
		self.assertTrue(result.request_log[0]["ok"])
		self.assertEqual(requested_urls, [result.request_log[0]["url"]])

	def test_hh_hard_filters_are_sent_to_html_search_url(self) -> None:
		requested_urls: list[str] = []

		def fake_urlopen(request, timeout):
			self.assertNotIn("api.hh.ru", request.full_url)
			requested_urls.append(request.full_url)
			return FakeResponse(HH_HTML, status=200)

		with patch.object(hh_source, "urlopen", fake_urlopen):
			result = hh_source.fetch_hh_vacancies(
				text="Backend разработчик python",
				area="1",
				per_page=5,
				pages=1,
				fetch_details=False,
				timeout=5,
				use_html=True,
				llm_client=None,
				max_items=5,
				hard_filters={
					"min_salary": 90000,
					"preferred_cities": ["Москва", "Красногорск", "Чебоксары"],
					"stop_words": ["английский", "тимлид"],
					"search_fields": ["name", "description"],
					"salary_defined": True,
					"working_hours": ["8", "4", "flexible"],
					"employment_contract": ["gph_or_part_time"],
					"preferred_formats": ["remote", "hybrid", "field", "onsite"],
					"preferred_levels": ["Internship", "Junior"],
					"accredited_it": True,
				},
			)

		query = parse_qs(urlparse(requested_urls[0]).query)
		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.request_log[0]["method"], "html")
		self.assertEqual(query["area"], ["1", "2034", "107"])
		self.assertEqual(query["salary"], ["90000"])
		self.assertEqual(query["excluded_text"], ["английский, тимлид"])
		self.assertEqual(set(query["search_field"]), {"name", "description"})
		self.assertTrue({"with_salary", "internship", "accredited_it"}.issubset(set(query["label"])))
		self.assertTrue({"REMOTE", "HYBRID", "FIELD_WORK", "ON_SITE"}.issubset(set(query["work_format"])))
		self.assertTrue({"HOURS_8", "HOURS_4", "FLEXIBLE"}.issubset(set(query["working_hours"])))
		self.assertTrue({"PART", "PROJECT"}.issubset(set(query["employment_form"])))
		self.assertEqual(query["accept_temporary"], ["true"])

	def test_superjob_hard_filters_are_sent_to_html_search_url(self) -> None:
		requested_urls: list[str] = []

		def fake_urlopen(request, timeout):
			self.assertNotIn("api.superjob.ru", request.full_url)
			requested_urls.append(request.full_url)
			return FakeResponse(SUPERJOB_HTML, status=200)

		with patch.object(superjob_source, "urlopen", fake_urlopen):
			result = superjob_source.fetch_superjob_vacancies(
				text="backend стажер, backend junior, analyst junior",
				town="4",
				count=5,
				pages=1,
				timeout=5,
				use_html=True,
				llm_client=None,
				max_items=5,
				fetch_details=False,
				hard_filters={
					"min_salary": 70000,
					"preferred_cities": ["Андреевка", "Апрелевка", "Красногорск", "Москва"],
					"stop_words": ["английский", "тимлид"],
					"search_fields": ["name"],
					"salary_defined": True,
					"working_hours": ["4", "evening"],
					"employment_contract": ["labor_contract", "gph_or_part_time"],
					"preferred_formats": ["onsite", "remote", "hybrid", "field"],
					"preferred_levels": ["Internship"],
				},
			)

		query = parse_qs(urlparse(requested_urls[0]).query)
		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.request_log[0]["method"], "html")
		self.assertEqual(query["payment_value"], ["70000"])
		self.assertEqual(query["payment_defined"], ["1"])
		self.assertEqual(query["profession_only"], ["1"])
		self.assertEqual(query["excluded"], ["английский, тимлид"])
		self.assertEqual([query[f"geo[t][{index}]"][0] for index in range(4)], ["2656", "1476", "559", "4"])
		self.assertEqual([query[f"workFormatTag[{index}]"][0] for index in range(4)], ["80", "81", "82", "83"])
		self.assertTrue({"72", "73", "114", "74", "115"}.issubset({value[0] for key, value in query.items() if key.startswith("employmentTypeTag[")}))
		self.assertTrue({"86", "87"}.issubset({value[0] for key, value in query.items() if key.startswith("partTimeJobTag[")}))
		self.assertIn("18", {value[0] for key, value in query.items() if key.startswith("tag[")})

	def test_hh_llm_only_skips_api_and_automatic_html_parser(self) -> None:
		def fake_urlopen(request, timeout):
			self.assertNotIn("api.hh.ru", request.full_url)
			return FakeResponse("<html><body>dynamic vacancies app</body></html>", status=200)

		client = FakeLLMClient()
		with patch.object(hh_source, "urlopen", fake_urlopen):
			result = hh_source.fetch_hh_vacancies(
				text="junior analyst",
				area="1",
				per_page=5,
				pages=1,
				fetch_details=True,
				timeout=5,
				use_html=True,
				llm_client=client,
				llm_only_html=True,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.vacancies[0]["source"], "hh-llm-html")
		self.assertEqual([item["method"] for item in result.request_log], ["html"])
		self.assertEqual(client.call_trace[0]["stage"], "hh_html_parse")

	def test_hh_mixed_html_merges_auto_and_llm_results(self) -> None:
		def fake_urlopen(request, timeout):
			self.assertNotIn("api.hh.ru", request.full_url)
			return FakeResponse(HH_HTML, status=200)

		client = MatchingHHLLMClient()
		with patch.object(hh_source, "urlopen", fake_urlopen):
			result = hh_source.fetch_hh_vacancies(
				text="junior analyst",
				area="1",
				per_page=5,
				pages=1,
				fetch_details=True,
				timeout=5,
				use_html=True,
				llm_client=client,
				llm_only_html=True,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.vacancies[0]["source"], "hh-mixed-html")
		self.assertIn("Python SQL Excel", result.vacancies[0]["description"])
		self.assertIn("A/B тесты", result.vacancies[0]["description"])
		self.assertIn("Готовить отчеты", result.vacancies[0]["responsibilities"])
		self.assertEqual([item["method"] for item in result.request_log], ["html"])

	def test_hh_mixed_html_keeps_auto_results_when_llm_disabled(self) -> None:
		def fake_urlopen(request, timeout):
			self.assertNotIn("api.hh.ru", request.full_url)
			return FakeResponse(HH_HTML, status=200)

		with patch.object(hh_source, "urlopen", fake_urlopen):
			result = hh_source.fetch_hh_vacancies(
				text="junior analyst",
				area="1",
				per_page=5,
				pages=1,
				fetch_details=True,
				timeout=5,
				use_html=True,
				llm_client=DisabledLLMClient(),
				llm_only_html=True,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.vacancies[0]["source"], "hh-html")
		self.assertIn("automatic parser will still run", result.warnings[0])

	def test_hh_llm_only_parses_pages_in_parallel(self) -> None:
		def fake_urlopen(request, timeout):
			self.assertNotIn("api.hh.ru", request.full_url)
			return FakeResponse("<html><body>dynamic vacancies app</body></html>", status=200)

		client = ConcurrentFakeLLMClient()
		with patch.object(hh_source, "urlopen", fake_urlopen):
			result = hh_source.fetch_hh_vacancies(
				text="junior analyst",
				area="1",
				per_page=1,
				pages=3,
				fetch_details=True,
				timeout=5,
				use_html=True,
				llm_client=client,
				llm_only_html=True,
				max_items=3,
			)

		self.assertEqual(len(result.request_log), 3)
		self.assertGreater(client.max_active, 1)

	def test_generic_html_source_uses_llm_when_parser_fails(self) -> None:
		def fake_urlopen(request, timeout):
			return FakeResponse("<html><body>dynamic vacancies app</body></html>", status=200)

		client = FakeLLMClient()
		with patch.object(generic_html_source, "urlopen", fake_urlopen):
			result = fetch_generic_html_vacancies(
				source="jooble",
				text="junior analyst",
				per_page=5,
				pages=1,
				timeout=5,
				llm_client=client,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.vacancies[0]["source"], "jooble-llm-html")
		self.assertEqual(result.request_log[0]["method"], "html")
		self.assertTrue(result.request_log[0]["ok"])
		self.assertEqual(client.call_trace[0]["stage"], "jooble_html_parse")

	def test_geekjob_llm_only_skips_public_json_parser(self) -> None:
		def fake_urlopen(request, timeout):
			self.assertNotIn("json/find/vacancy", request.full_url)
			return FakeResponse("<html><body>dynamic vacancies app</body></html>", status=200)

		client = FakeLLMClient()
		with patch.object(generic_html_source, "urlopen", fake_urlopen):
			result = fetch_generic_html_vacancies(
				source="geekjob",
				text="junior analyst",
				per_page=5,
				pages=1,
				timeout=5,
				llm_client=client,
				llm_only_html=True,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.vacancies[0]["source"], "geekjob-llm-html")
		self.assertTrue(result.request_log)
		self.assertTrue(all(item["method"] == "html" for item in result.request_log))
		self.assertEqual(client.call_trace[0]["stage"], "geekjob_html_parse")

	def test_geekjob_source_uses_public_json_endpoint(self) -> None:
		def fake_urlopen(request, timeout):
			return FakeResponse(
				'{"page":1,"nextpage":0,"data":[{"position":"Middle Java developer","salary":"до 250K ₽","country":"Россия","city":"Москва","jobFormat":{"remote":true},"company":{"name":"Acme"},"id":"abc123","log":{"modify":"4 июня"}}]}',
				status=200,
			)

		with patch.object(generic_html_source, "urlopen", fake_urlopen):
			result = fetch_generic_html_vacancies(
				source="geekjob",
				text="middle Java developer",
				per_page=5,
				pages=1,
				timeout=5,
				llm_client=None,
				max_items=5,
			)

		self.assertEqual(len(result.vacancies), 1)
		self.assertEqual(result.vacancies[0]["source"], "geekjob-html")
		self.assertEqual(result.vacancies[0]["company"], "Acme")
		self.assertEqual(result.request_log[0]["method"], "json")

	def test_source_limits_follow_priority_weights(self) -> None:
		priorities = _parse_source_priorities("hh:high,rabota_ru:medium,jooble:low")
		limits = _allocate_source_limits(["hh", "rabota_ru", "jooble"], priorities, 70)

		self.assertEqual(priorities, {"hh": "high", "rabota_ru": "medium", "jooble": "low"})
		self.assertEqual(sum(limits.values()), 70)
		self.assertGreater(limits["hh"], limits["rabota_ru"])
		self.assertGreater(limits["rabota_ru"], limits["jooble"])

	def test_source_fetch_window_is_calculated_from_source_limit(self) -> None:
		self.assertEqual(_source_fetch_window(17), (17, 5))
		self.assertEqual(_source_fetch_window(120), (20, 6))
		self.assertEqual(_source_fetch_window(120, per_page_override=30), (30, 5))

	def test_query_limits_are_split_evenly(self) -> None:
		queries = _parse_queries('["junior analyst", "data intern", "Junior Analyst"]', "fallback")
		limits = _allocate_query_limits(queries, 10)

		self.assertEqual(queries, ["junior analyst", "data intern"])
		self.assertEqual(limits, {"junior analyst": 5, "data intern": 5})

	def test_fetch_parse_metrics_split_html_and_llm_html(self) -> None:
		metrics = _fetch_parse_metrics([
			{"source": "hh"},
			{"source": "hh-html"},
			{"source": "geekjob-llm-html"},
			{"source": "superjob-html"},
			{"source": "hh-mixed-html"},
		])

		self.assertEqual(metrics, {"total_considered": 5, "html_without_llm": 2, "html_with_llm": 1, "html_mixed": 1})

	def test_mixed_merge_prioritizes_matched_vacancies_and_keeps_uniques(self) -> None:
		merged = merge_auto_and_llm_vacancies(
			source="habr",
			auto_vacancies=[
				{"source": "habr-html", "title": "Matched", "company": "Acme", "link": "https://career.habr.com/vacancies/1?query=x", "description": "Auto text"},
				{"source": "habr-html", "title": "Auto only", "company": "Acme", "link": "https://career.habr.com/vacancies/2", "description": "Auto only text"},
			],
			llm_vacancies=[
				{"source": "habr-llm-html", "title": "Matched", "company": "Acme", "link": "https://career.habr.com/vacancies/1", "description": "LLM richer text"},
				{"source": "habr-llm-html", "title": "LLM only", "company": "Beta", "link": "https://career.habr.com/vacancies/3", "description": "LLM only text"},
			],
			max_items=10,
		)

		self.assertEqual([item["title"] for item in merged], ["Matched", "Auto only", "LLM only"])
		self.assertEqual(merged[0]["source"], "habr-mixed-html")
		self.assertIn("Auto text", merged[0]["description"])
		self.assertIn("LLM richer text", merged[0]["description"])

	def test_vacancy_csv_schema_keeps_context_fields_for_ranking(self) -> None:
		self.assertIn("description", VACANCY_COLUMNS)
		self.assertIn("requirements", VACANCY_COLUMNS)
		self.assertIn("responsibilities", VACANCY_COLUMNS)
		self.assertIn("conditions", VACANCY_COLUMNS)
		self.assertIn("employment_type", VACANCY_COLUMNS)

	def test_html_cleaner_removes_noise(self) -> None:
		cleaner = HTMLCleaner(max_chars=500)
		cleaned = cleaner.clean(
			"""
			<html>
			  <script>window.secret = 1</script>
			  <nav>menu</nav>
			  <main><h1>Junior Analyst</h1><p>SQL Python dashboards</p></main>
			  <footer>footer</footer>
			</html>
			"""
		)

		self.assertIn("Junior Analyst", cleaned)
		self.assertIn("SQL Python", cleaned)
		self.assertNotIn("window.secret", cleaned)
		self.assertNotIn("menu", cleaned)

	def test_url_llm_extractor_and_normalizer_return_project_schema(self) -> None:
		client = FakeLLMClient()
		extractor = LLMVacancyExtractor(client)
		raw, error = extractor.extract(
			text="Junior analyst vacancy. SQL Python. Hybrid Moscow.",
			url="https://hh.ru/vacancy/123",
			source_site="hh-url",
		)
		normalizer = VacancyNormalizer()
		vacancy = normalizer.normalize(raw or {}, source_site="hh-url", url="https://hh.ru/vacancy/123")

		self.assertEqual(error, "")
		self.assertEqual(vacancy["source"], "hh-url")
		self.assertEqual(vacancy["title"], "LLM Parsed Analyst")
		self.assertEqual(vacancy["level"], "Junior")
		self.assertIn("SQL", vacancy["stack"])
		self.assertEqual(source_from_url("https://www.superjob.ru/vakansii/test-1.html"), "superjob-url")


if __name__ == "__main__":
	unittest.main()
