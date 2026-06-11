from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .base import FetchResult, clean_html, join_values, normalize_format, normalize_level, normalize_salary
from .html_source import merge_auto_and_llm_vacancies, parse_hh_detail_html, parse_hh_html, parse_html_with_llm, split_ru_vacancy_sections

HH_API_URL = "https://api.hh.ru/vacancies"
HH_HTML_URL = "https://hh.ru/search/vacancy"
DEFAULT_HH_USER_AGENT = "AISearchJob/1.0 (set-HH_USER_AGENT-env-with-real-contact-email)"
HTML_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36"


def fetch_hh_vacancies(
    *,
    text: str,
    area: str | None = None,
    per_page: int = 20,
    pages: int = 1,
    fetch_details: bool = True,
    timeout: int = 15,
    use_html: bool = True,
    llm_client: Any | None = None,
    llm_only_html: bool = False,
    allow_llm_fallback: bool = True,
    max_items: int = 50,
    start_page: int = 0,
    hard_filters: dict[str, Any] | None = None,
) -> FetchResult:
    warnings: list[str] = []
    vacancies: list[dict[str, Any]] = []
    request_log: list[dict[str, Any]] = []
    per_page = max(1, min(per_page, 100))
    pages = max(1, min(pages, 20))
    max_items = max(1, min(max_items, 300))
    start_page = max(0, int(start_page or 0))

    hard_filters = hard_filters or {}

    if use_html:
        html_vacancies, html_warnings, html_log = fetch_hh_html_vacancies(
            text=text,
            area=area,
            per_page=per_page,
            pages=pages,
            timeout=timeout,
            max_items=max_items - len(vacancies),
            llm_client=llm_client,
            llm_only_html=llm_only_html,
            allow_llm_fallback=allow_llm_fallback,
            start_page=start_page,
            fetch_details=fetch_details,
            hard_filters=hard_filters,
        )
        vacancies.extend(html_vacancies)
        warnings.extend(html_warnings)
        request_log.extend(html_log)

    return FetchResult(source="hh", vacancies=vacancies[:max_items], warnings=warnings, request_log=request_log)


def fetch_hh_html_vacancies(
    *,
    text: str,
    area: str | None = None,
    per_page: int = 20,
    pages: int = 1,
    timeout: int = 15,
    max_items: int = 50,
    llm_client: Any | None = None,
    llm_only_html: bool = False,
    allow_llm_fallback: bool = True,
    start_page: int = 0,
    fetch_details: bool = True,
    hard_filters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []
    vacancies: list[dict[str, Any]] = []
    per_page = max(1, min(per_page, 50))
    pages = max(1, min(pages, 20))
    max_items = max(1, min(max_items, 300))
    start_page = max(0, int(start_page or 0))
    hard_filters = hard_filters or {}
    if llm_only_html and not getattr(llm_client, "enabled", False):
        warnings.append("hh mixed html parsing is enabled but LLM client is disabled; automatic parser will still run.")

    if llm_only_html:
        tasks = []
        for page in range(start_page, start_page + pages):
            params = _build_hh_html_search_params(
                text=text,
                area=area,
                items_on_page=min(per_page, max_items),
                page=page,
                hard_filters=hard_filters,
            )
            tasks.append((page, f"{HH_HTML_URL}?{urlencode(params)}"))
        if not tasks:
            return [], warnings, request_log

        def fetch_and_parse(page: int, url: str) -> tuple[int, list[dict[str, Any]], list[str], dict[str, Any]]:
            try:
                html, status = _get_text(url, timeout=timeout)
                log = {"source": "hh", "method": "html", "url": url, "status": status, "ok": True}
            except Exception as error:  # noqa: BLE001
                return page, [], [f"hh html request failed for page {page}: {_format_error(error)}"], {
                    "source": "hh",
                    "method": "html",
                    "url": url,
                    "status": _error_status(error),
                    "ok": False,
                }
            with ThreadPoolExecutor(max_workers=2) as parser_executor:
                auto_future = parser_executor.submit(parse_hh_html, html, query=text, max_items=min(per_page, max_items))
                llm_future = parser_executor.submit(
                    parse_html_with_llm,
                    source="hh",
                    html=html,
                    query=text,
                    page_url=url,
                    max_items=min(per_page, max_items),
                    llm_client=llm_client,
                )
                auto_parsed = auto_future.result()
                llm_parsed = llm_future.result()
            parsed = merge_auto_and_llm_vacancies(
                source="hh",
                auto_vacancies=auto_parsed,
                llm_vacancies=llm_parsed,
                max_items=min(per_page, max_items),
            )
            page_warnings = []
            if not auto_parsed:
                page_warnings.append(f"hh automatic html parser found no vacancies on page {page}.")
            if not llm_parsed:
                page_warnings.append(f"hh html LLM parsing found no vacancies on page {page}.")
            return page, parsed, page_warnings, log

        results = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(fetch_and_parse, page, url) for page, url in tasks]
            for future in as_completed(futures):
                results.append(future.result())

        for _, parsed, page_warnings, log in sorted(results, key=lambda item: item[0]):
            request_log.append(log)
            warnings.extend(page_warnings)
            vacancies.extend(parsed)
        return vacancies[:max_items], warnings, request_log

    for page in range(start_page, start_page + pages):
        if len(vacancies) >= max_items:
            break
        params = _build_hh_html_search_params(
            text=text,
            area=area,
            items_on_page=min(per_page, max_items - len(vacancies)),
            page=page,
            hard_filters=hard_filters,
        )
        url = f"{HH_HTML_URL}?{urlencode(params)}"
        try:
            html, status = _get_text(url, timeout=timeout)
            request_log.append({"source": "hh", "method": "html", "url": url, "status": status, "ok": True})
        except Exception as error:  # noqa: BLE001
            warnings.append(f"hh html request failed for page {page}: {_format_error(error)}")
            request_log.append({"source": "hh", "method": "html", "url": url, "status": _error_status(error), "ok": False})
            break
        if llm_only_html:
            parsed = parse_html_with_llm(
                source="hh",
                html=html,
                query=text,
                page_url=url,
                max_items=max_items - len(vacancies),
                llm_client=llm_client,
            )
            if not parsed:
                warnings.append(f"hh html LLM parsing found no vacancies on page {page}.")
        else:
            parsed = parse_hh_html(html, query=text, max_items=max_items - len(vacancies))
            if not parsed:
                if allow_llm_fallback:
                    llm_parsed = parse_html_with_llm(
                        source="hh",
                        html=html,
                        query=text,
                        page_url=url,
                        max_items=max_items - len(vacancies),
                        llm_client=llm_client,
                    )
                    if llm_parsed:
                        warnings.append(f"hh html was parsed by LLM fallback for page {page}.")
                        parsed = llm_parsed
                    else:
                        warnings.append(f"hh html parser found no vacancies on page {page}.")
                else:
                    warnings.append(f"hh html parser found no vacancies on page {page}.")
        enriched, detail_warnings, detail_log = _enrich_hh_html_details(parsed, timeout=timeout, fetch_details=fetch_details)
        vacancies.extend(enriched)
        warnings.extend(detail_warnings)
        request_log.extend(detail_log)
    return vacancies[:max_items], warnings, request_log


def _build_hh_html_search_params(
    *,
    text: str,
    area: str | None,
    items_on_page: int,
    page: int,
    hard_filters: dict[str, Any] | None,
) -> list[tuple[str, str | int]]:
    filters = hard_filters or {}
    params: list[tuple[str, str | int]] = [
        ("text", text),
        ("items_on_page", items_on_page),
        ("page", page),
    ]

    filter_areas = _hh_area_ids(filters)
    if filter_areas:
        params.extend(("area", area_id) for area_id in filter_areas)
    elif area:
        params.extend(("area", area_id) for area_id in _split_filter_values(area))

    min_salary = _filter_int(filters, "min_salary")
    if min_salary:
        params.append(("salary_mode", "MONTH"))
        params.append(("salary", min_salary))

    stop_words = _filter_text(filters, "stop_words", "excluded_text")
    if stop_words:
        params.append(("excluded_text", stop_words))

    for search_field in _hh_search_fields(filters):
        params.append(("search_field", search_field))
    for experience in _hh_experience_values(filters):
        params.append(("experience", experience))
    for label in _hh_label_values(filters):
        params.append(("label", label))
    for work_format in _hh_work_formats(filters):
        params.append(("work_format", work_format))
    for working_hours in _hh_working_hours(filters):
        params.append(("working_hours", working_hours))
    for employment_form in _hh_employment_forms(filters):
        params.append(("employment_form", employment_form))

    if _filter_bool(filters, "temporary_contract", "accept_temporary") or _hh_accept_temporary(filters):
        params.append(("accept_temporary", "true"))
    return params


def _has_hh_service_filters(filters: dict[str, Any]) -> bool:
    keys = (
        "min_salary",
        "preferred_levels",
        "preferred_formats",
        "preferred_cities",
        "stop_words",
        "search_fields",
        "salary_defined",
        "income_specified",
        "working_hours",
        "employment_contract",
        "temporary_contract",
        "accept_temporary",
        "accredited_it",
    )
    return any(_filter_has_value(filters, key) for key in keys)


def _hh_area_ids(filters: dict[str, Any]) -> list[str]:
    values = _split_filter_values(filters.get("preferred_cities") or filters.get("areas") or filters.get("area"))
    if not values:
        return []
    city_map = {
        "москва": "1",
        "moscow": "1",
        "мск": "1",
        "санкт-петербург": "2",
        "санкт петербург": "2",
        "спб": "2",
        "saint petersburg": "2",
        "казань": "88",
        "новосибирск": "4",
        "екатеринбург": "3",
        "московская область": "2019",
        "подмосковье": "2019",
        "красногорск": "2034",
        "апрелевка": "2042",
        "андреевка": "6339",
        "чебоксары": "107",
    }
    result: list[str] = []
    for value in values:
        normalized = _normalize_filter_token(value)
        if not normalized or normalized in {"удаленно", "remote"}:
            continue
        if re.fullmatch(r"\d+", normalized):
            result.append(normalized)
            continue
        mapped = city_map.get(normalized)
        if mapped:
            result.append(mapped)
    return _dedupe(result)


def _hh_search_fields(filters: dict[str, Any]) -> list[str]:
    field_map = {
        "name": "name",
        "title": "name",
        "vacancy_name": "name",
        "название": "name",
        "название вакансии": "name",
        "description": "description",
        "описание": "description",
        "описание вакансии": "description",
        "company": "company_name",
        "company_name": "company_name",
        "название компании": "company_name",
        "компания": "company_name",
    }
    values = _split_filter_values(filters.get("search_fields") or filters.get("search_field"))
    return _dedupe(field_map.get(_normalize_filter_token(value), "") for value in values)


def _hh_experience_values(filters: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in _split_filter_values(filters.get("preferred_levels") or filters.get("experience")):
        normalized = _normalize_filter_token(value)
        if normalized in {"internship", "intern", "стажировка", "стажер", "entry", "junior", "джуниор", "без опыта", "no experience"}:
            result.append("noExperience")
        if normalized in {"junior", "джуниор", "middle", "мидл", "1-3", "1 3", "от 1 года до 3 лет"}:
            result.append("between1And3")
        if normalized in {"middle", "мидл", "senior", "сеньор", "3-6", "3 6", "от 3 до 6 лет"}:
            result.append("between3And6")
        if normalized in {"senior", "lead", "сеньор", "лид", "тимлид", "более 6 лет", "6+"}:
            result.append("moreThan6")
    return _dedupe(result)


def _hh_label_values(filters: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    if _filter_bool(filters, "salary_defined", "income_specified", "with_salary"):
        labels.append("with_salary")
    if _filter_bool(filters, "accredited_it", "accredited_it_company"):
        labels.append("accredited_it")
    for value in _split_filter_values(filters.get("preferred_levels") or filters.get("labels")):
        normalized = _normalize_filter_token(value)
        if normalized in {"internship", "intern", "стажировка", "стажер"}:
            labels.append("internship")
    return _dedupe(labels)


def _hh_work_formats(filters: dict[str, Any]) -> list[str]:
    format_map = {
        "remote": "REMOTE",
        "удаленно": "REMOTE",
        "удаленка": "REMOTE",
        "удаленная работа": "REMOTE",
        "hybrid": "HYBRID",
        "гибрид": "HYBRID",
        "onsite": "ON_SITE",
        "office": "ON_SITE",
        "офис": "ON_SITE",
        "на месте работодателя": "ON_SITE",
        "field": "FIELD_WORK",
        "field work": "FIELD_WORK",
        "разъездной": "FIELD_WORK",
        "разъездная": "FIELD_WORK",
    }
    return _dedupe(format_map.get(_normalize_filter_token(value), "") for value in _split_filter_values(filters.get("preferred_formats") or filters.get("work_format")))


def _hh_working_hours(filters: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in _split_filter_values(filters.get("working_hours") or filters.get("working_hours_per_day")):
        normalized = _normalize_filter_token(value)
        if normalized in {"flexible", "гибко", "гибкие", "гибкий"}:
            result.append("FLEXIBLE")
            continue
        if normalized in {"other", "другое", "другие"}:
            result.append("OTHER")
            continue
        digits = re.findall(r"\d+", normalized)
        if digits:
            hours = int(digits[0])
            if hours in {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 24}:
                result.append(f"HOURS_{hours}")
    return _dedupe(result)


def _hh_employment_forms(filters: dict[str, Any]) -> list[str]:
    values = _split_filter_values(filters.get("employment_contract") or filters.get("employment_form"))
    result: list[str] = []
    for value in values:
        normalized = _normalize_filter_token(value)
        if normalized in {"labor contract", "employment contract", "трудовой договор", "тк", "full"}:
            result.append("FULL")
        if normalized in {"part", "part time", "частичная занятость"}:
            result.append("PART")
        if normalized in {"gph", "gph or part time", "гпх", "совместительство", "проект", "project"}:
            result.extend(["PART", "PROJECT"])
    return _dedupe(result)


def _hh_accept_temporary(filters: dict[str, Any]) -> bool:
    for value in _split_filter_values(filters.get("employment_contract") or filters.get("employment_form")):
        if _normalize_filter_token(value) in {"gph", "gph or part time", "гпх", "совместительство", "проект", "project"}:
            return True
    return False


def _filter_text(filters: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = filters.get(key)
        if isinstance(value, list):
            text = ", ".join(str(item).strip() for item in value if str(item).strip())
        else:
            text = str(value or "").strip()
        if text:
            return text
    return ""


def _filter_int(filters: dict[str, Any], key: str) -> int:
    digits = re.findall(r"\d+", str(filters.get(key) or ""))
    return int("".join(digits)) if digits else 0


def _filter_bool(filters: dict[str, Any], *keys: str) -> bool:
    truthy = {"1", "true", "yes", "y", "on", "да", "истина", "указан", "указана", "есть", "with salary"}
    falsy = {"0", "false", "no", "n", "off", "нет", "ложь", "не указан", "не указана"}
    for key in keys:
        value = filters.get(key)
        if isinstance(value, bool):
            return value
        values = _split_filter_values(value)
        if not values and value not in (None, ""):
            values = [str(value)]
        for item in values:
            normalized = _normalize_filter_token(item)
            if normalized in truthy:
                return True
            if normalized in falsy:
                return False
    return False


def _filter_has_value(filters: dict[str, Any], key: str) -> bool:
    value = filters.get(key)
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return bool(str(value or "").strip())


def _split_filter_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_values = [str(item) for item in value]
    else:
        raw_values = re.split(r"[;|,]", str(value or ""))
    values: list[str] = []
    for raw in raw_values:
        text = " ".join(str(raw or "").split()).strip()
        if text:
            values.append(text)
    return values


def _normalize_filter_token(value: Any) -> str:
    return " ".join(str(value or "").replace("_", " ").strip().lower().replace("ё", "е").split())


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _enrich_hh_html_details(
    vacancies: list[dict[str, Any]],
    *,
    timeout: int,
    fetch_details: bool,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    if not vacancies:
        return [], [], []
    if not fetch_details:
        return vacancies, [], []

    def fetch_detail(index: int, vacancy: dict[str, Any]) -> tuple[int, dict[str, Any], str | None, dict[str, Any]]:
        vacancy_id = _extract_hh_vacancy_id(vacancy)
        link = str(vacancy.get("link") or "")
        if not link:
            return index, vacancy, None, {}
        detail_url = link if "hh.ru/vacancy/" in link else f"https://hh.ru/vacancy/{vacancy_id}" if vacancy_id else link
        try:
            html, status = _get_text(detail_url, timeout=timeout)
            log = {"source": "hh", "method": "html-detail", "url": detail_url, "status": status, "ok": True}
            detail_vacancy = parse_hh_detail_html(html, page_url=detail_url)
        except Exception as error:  # noqa: BLE001
            return index, vacancy, f"hh html detail failed for {vacancy_id or link}: {_format_error(error)}", {
                "source": "hh",
                "method": "html-detail",
                "url": detail_url,
                "status": _error_status(error),
                "ok": False,
            }
        return index, _merge_detail_over_search(vacancy, detail_vacancy) if detail_vacancy else vacancy, None, log

    results: list[tuple[int, dict[str, Any], str | None, dict[str, Any]]] = []
    max_workers = min(8, len(vacancies))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_detail, index, vacancy) for index, vacancy in enumerate(vacancies)]
        for future in as_completed(futures):
            results.append(future.result())

    enriched: list[dict[str, Any]] = []
    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []
    for _, vacancy, warning, log in sorted(results, key=lambda item: item[0]):
        enriched.append(vacancy)
        if warning:
            warnings.append(warning)
        if log:
            request_log.append(log)
    return enriched, warnings, request_log


def _enrich_hh_api_details(
    items: list[dict[str, Any]],
    *,
    timeout: int,
    fetch_details: bool,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    if not items:
        return [], [], []
    if not fetch_details:
        return [_map_hh_item(item, {}) for item in items], [], []

    def fetch_detail(index: int, item: dict[str, Any]) -> tuple[int, dict[str, Any], str, dict[str, Any]]:
        detail: dict[str, Any] = {}
        warnings: list[str] = []
        log: dict[str, Any] = {}
        if item.get("url"):
            url = str(item["url"])
            try:
                detail, detail_status = _get_json(url, timeout=timeout)
                log = {"source": "hh", "method": "api-detail", "url": url, "status": detail_status, "ok": True}
                time.sleep(0.05)
            except Exception as error:  # noqa: BLE001
                warnings.append(f"hh detail failed for {item.get('id')}: {error}")
                log = {"source": "hh", "method": "api-detail", "url": url, "status": _error_status(error), "ok": False}
        return index, _map_hh_item(item, detail if isinstance(detail, dict) else {}), "\n".join(warnings), log

    results: list[tuple[int, dict[str, Any], str, dict[str, Any]]] = []
    max_workers = min(8, len(items))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_detail, index, item) for index, item in enumerate(items)]
        for future in as_completed(futures):
            results.append(future.result())

    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []
    enriched: list[dict[str, Any]] = []
    for _, vacancy, warning_text, log in sorted(results, key=lambda item: item[0]):
        enriched.append(vacancy)
        if warning_text:
            warnings.extend(part for part in warning_text.splitlines() if part.strip())
        if log:
            request_log.append(log)
    return enriched, warnings, request_log


def _extract_hh_vacancy_id(vacancy: dict[str, Any]) -> str:
    for value in (vacancy.get("vacancy_id"), vacancy.get("link")):
        match = re.search(r"(\d{5,})", str(value or ""))
        if match:
            return match.group(1)
    return ""


def _hh_item_stub_from_html(vacancy: dict[str, Any], vacancy_id: str) -> dict[str, Any]:
    return {
        "id": vacancy_id,
        "name": vacancy.get("title"),
        "alternate_url": vacancy.get("link") or f"https://hh.ru/vacancy/{vacancy_id}",
        "employer": {"name": vacancy.get("company")},
        "area": {"name": vacancy.get("city")},
        "salary": vacancy.get("salary_rub") or vacancy.get("salary_text"),
        "snippet": {
            "requirement": vacancy.get("requirements"),
            "responsibility": vacancy.get("responsibilities") or vacancy.get("description"),
        },
    }


def _merge_detail_over_search(search_vacancy: dict[str, Any], detail_vacancy: dict[str, Any]) -> dict[str, Any]:
    merged = dict(search_vacancy)
    for key, value in detail_vacancy.items():
        if value not in (None, ""):
            merged[key] = value
    search_company = str(search_vacancy.get("company") or "").strip()
    detail_company = str(detail_vacancy.get("company") or "").strip()
    if search_company and detail_company and detail_company.lower() in search_company.lower() and len(search_company) > len(detail_company):
        merged["company"] = search_company
        merged["employer_name"] = search_company
    detail_raw = str(detail_vacancy.get("raw_detail_text") or detail_vacancy.get("description") or "")
    if detail_raw:
        if search_company and detail_company and detail_company in detail_raw and detail_company.lower() in search_company.lower() and len(search_company) > len(detail_company):
            detail_raw = detail_raw.replace(detail_company, search_company, 1)
        merged["raw_detail_text"] = detail_raw
    return merged


def _get_json(url: str, *, timeout: int) -> tuple[Any, int]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "HH-User-Agent": os.getenv("HH_USER_AGENT", DEFAULT_HH_USER_AGENT),
            "User-Agent": os.getenv("HH_HTTP_USER_AGENT", "Python-urllib/AI-Vacancy-Match-Agent"),
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - official API endpoint, user-controlled only by query params
        return json.loads(response.read().decode("utf-8")), response.status


def _get_text(url: str, *, timeout: int) -> tuple[str, int]:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": HTML_USER_AGENT,
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - legal search page, no captcha bypass.
        return response.read().decode("utf-8", errors="replace"), response.status


def _format_error(error: Exception) -> str:
    if isinstance(error, HTTPError):
        body = error.read().decode("utf-8", errors="replace").strip()
        return f"HTTP {error.code}: {body[:300]}"
    return f"{type(error).__name__}: {error}"


def _error_status(error: Exception) -> int | str:
    return error.code if isinstance(error, HTTPError) else type(error).__name__


def _map_hh_item(item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    employer = detail.get("employer") or item.get("employer") or {}
    area = detail.get("area") or item.get("area") or {}
    salary = detail.get("salary") if detail.get("salary") is not None else item.get("salary")
    schedule = detail.get("schedule") or item.get("schedule") or {}
    experience = detail.get("experience") or item.get("experience") or {}
    employment = detail.get("employment") or item.get("employment") or {}
    employment_form = detail.get("employment_form") or item.get("employment_form") or {}
    snippet = item.get("snippet") or {}
    key_skills = [skill.get("name") for skill in detail.get("key_skills", []) if isinstance(skill, dict)]
    requirements = clean_html(snippet.get("requirement"))
    responsibilities = clean_html(snippet.get("responsibility"))
    description = clean_html(detail.get("description") or join_values([requirements, responsibilities]))
    sections = split_ru_vacancy_sections(description)
    responsibilities = sections.get("responsibilities") or responsibilities
    requirements = sections.get("requirements") or requirements
    conditions = sections.get("conditions") or clean_html(detail.get("branded_description"))
    skills_text = join_values(key_skills) or clean_html(snippet.get("requirement"))
    title = str(item.get("name") or detail.get("name") or "")
    level_source = join_values([experience.get("name"), title, description[:250]])
    working_time_modes = _hh_join_names(detail.get("working_time_modes"))
    working_time_intervals = _hh_join_names(detail.get("working_time_intervals"))
    working_hours = _hh_join_names(detail.get("working_hours")) or working_time_intervals
    work_format = _hh_join_names(detail.get("work_format")) or _hh_join_names(detail.get("work_formats"))
    format_source = join_values([schedule.get("id"), schedule.get("name"), working_time_modes, work_format])
    address = _hh_format_address(detail.get("address"))
    metro = _hh_format_metro(detail.get("address"))
    salary_text = _hh_salary_text(salary)
    payment_frequency = _hh_payment_frequency(detail)
    raw_detail_text = _hh_raw_detail_text(
        title=title,
        salary_text=salary_text,
        payment_frequency=payment_frequency,
        experience=experience.get("name") or "",
        employment_type=employment.get("name") if isinstance(employment, dict) else "",
        employment_form=employment_form.get("name") if isinstance(employment_form, dict) else "",
        schedule=schedule.get("name") if isinstance(schedule, dict) else "",
        working_hours=working_hours,
        work_format=work_format,
        company=employer.get("name") if isinstance(employer, dict) else "",
        description=description,
        key_skills=join_values(key_skills),
        address=address,
        published_at=detail.get("published_at") or item.get("published_at") or "",
    )
    return {
        "vacancy_id": f"hh:{item.get('id') or detail.get('id') or ''}",
        "source": "hh",
        "title": title,
        "company": employer.get("name") or "",
        "role": title,
        "level": normalize_level(level_source),
        "format": normalize_format(format_source),
        "city": area.get("name") or "",
        "relocation_possible": "",
        "published_at": item.get("published_at") or detail.get("published_at") or "",
        "deadline": "",
        "salary_rub": normalize_salary(salary),
        "salary_text": salary_text,
        "payment_frequency": payment_frequency,
        "stack": skills_text,
        "key_skills": join_values(key_skills),
        "english_level": "",
        "link": item.get("alternate_url") or detail.get("alternate_url") or "",
        "description": description,
        "requirements": requirements,
        "responsibilities": responsibilities,
        "conditions": conditions or clean_html(join_values([schedule.get("name"), experience.get("name")])),
        "employment_type": employment.get("name") if isinstance(employment, dict) else "",
        "employment_form": employment_form.get("name") if isinstance(employment_form, dict) else "",
        "experience": experience.get("name") if isinstance(experience, dict) else "",
        "schedule": schedule.get("name") if isinstance(schedule, dict) else "",
        "working_hours": working_hours,
        "work_format": work_format or normalize_format(format_source),
        "address": address,
        "metro_stations": metro,
        "employer_name": employer.get("name") or "",
        "agency_company": "",
        "company_description": clean_html((employer.get("description") or "") if isinstance(employer, dict) else ""),
        "category": _hh_professional_roles(detail),
        "published_at_text": item.get("published_at") or detail.get("published_at") or "",
        "views_count": "",
        "detail_source": "hh-api-detail" if detail else "hh-api-search",
        "raw_detail_text": raw_detail_text,
    }


def _hh_join_names(value: Any) -> str:
    if isinstance(value, list):
        return join_values(item.get("name") if isinstance(item, dict) else item for item in value)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or "").strip()
    return str(value or "").strip()


def _hh_salary_text(salary: Any) -> str:
    if not isinstance(salary, dict):
        return normalize_salary(salary)
    salary_from = salary.get("from")
    salary_to = salary.get("to")
    currency = str(salary.get("currency") or "").upper()
    symbol = "₽" if currency in {"RUR", "RUB"} else currency
    gross = salary.get("gross")
    if salary_from and salary_to and salary_from != salary_to:
        base = f"{_format_money(salary_from)} – {_format_money(salary_to)} {symbol}"
    elif salary_from:
        base = f"от {_format_money(salary_from)} {symbol}"
    elif salary_to:
        base = f"до {_format_money(salary_to)} {symbol}"
    else:
        return ""
    tax = "до вычета налогов" if gross else "на руки" if gross is False else ""
    return join_values([base, tax]).replace("; ", ", ")


def _format_money(value: Any) -> str:
    try:
        number = int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return str(value or "").strip()
    return f"{number:,}".replace(",", " ")


def _hh_payment_frequency(detail: dict[str, Any]) -> str:
    for key in ("salary_range", "compensation", "salary"):
        value = detail.get(key)
        if isinstance(value, dict):
            frequency = value.get("frequency") or value.get("payment_frequency")
            if isinstance(frequency, dict):
                return str(frequency.get("name") or frequency.get("id") or "").strip()
            if frequency:
                return str(frequency).strip()
    return ""


def _hh_format_address(address: Any) -> str:
    if not isinstance(address, dict):
        return ""
    raw = address.get("raw")
    if raw:
        return str(raw).strip()
    parts = [address.get("city"), address.get("street"), address.get("building")]
    return join_values(parts)


def _hh_format_metro(address: Any) -> str:
    if not isinstance(address, dict):
        return ""
    stations: list[str] = []
    metro = address.get("metro")
    if isinstance(metro, dict):
        stations.append(str(metro.get("station_name") or metro.get("name") or "").strip())
    for item in address.get("metro_stations") or []:
        if isinstance(item, dict):
            stations.append(str(item.get("station_name") or item.get("name") or "").strip())
    return join_values(dict.fromkeys(station for station in stations if station))


def _hh_professional_roles(detail: dict[str, Any]) -> str:
    roles = detail.get("professional_roles") or []
    return join_values(item.get("name") for item in roles if isinstance(item, dict))


def _hh_raw_detail_text(
    *,
    title: str,
    salary_text: str,
    payment_frequency: str,
    experience: str,
    employment_type: str,
    employment_form: str,
    schedule: str,
    working_hours: str,
    work_format: str,
    company: str,
    description: str,
    key_skills: str,
    address: str,
    published_at: str,
) -> str:
    lines = [
        title,
        salary_text,
        f"Выплаты: {payment_frequency}" if payment_frequency else "",
        f"Опыт работы: {experience}" if experience else "",
        employment_type,
        f"Оформление: {employment_form}" if employment_form else "",
        f"График: {schedule}" if schedule else "",
        f"Рабочие часы: {working_hours}" if working_hours else "",
        f"Формат работы: {work_format}" if work_format else "",
        company,
        description,
        f"Ключевые навыки: {key_skills}" if key_skills else "",
        f"Где предстоит работать: {address}" if address else "",
        f"Вакансия опубликована: {published_at}" if published_at else "",
    ]
    return "\n".join(str(line).strip() for line in lines if str(line or "").strip())
