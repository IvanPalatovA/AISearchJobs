from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .base import FetchResult, clean_html, join_values, normalize_format, normalize_level, normalize_salary
from .html_source import merge_auto_and_llm_vacancies, parse_html_with_llm, parse_superjob_detail_html, parse_superjob_html

SUPERJOB_API_URL = "https://api.superjob.ru/2.0/vacancies/"
SUPERJOB_HTML_URL = "https://www.superjob.ru/vacancy/search/"
HTML_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36"


def fetch_superjob_vacancies(
    *,
    text: str,
    town: str | None = None,
    count: int = 20,
    pages: int = 1,
    timeout: int = 15,
    api_key: str | None = None,
    use_html: bool = True,
    llm_client: Any | None = None,
    llm_only_html: bool = False,
    allow_llm_fallback: bool = True,
    max_items: int = 50,
    start_page: int = 0,
    fetch_details: bool = True,
    hard_filters: dict[str, Any] | None = None,
) -> FetchResult:
    warnings: list[str] = []
    vacancies: list[dict[str, Any]] = []
    request_log: list[dict[str, Any]] = []
    count = max(1, min(count, 100))
    pages = max(1, min(pages, 20))
    max_items = max(1, min(max_items, 300))
    start_page = max(0, int(start_page or 0))
    hard_filters = hard_filters or {}

    if use_html:
        html_vacancies, html_warnings, html_log = fetch_superjob_html_vacancies(
            text=text,
            town=town,
            count=count,
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

    return FetchResult(source="superjob", vacancies=vacancies[:max_items], warnings=warnings, request_log=request_log)


def fetch_superjob_html_vacancies(
    *,
    text: str,
    town: str | None = None,
    count: int = 20,
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
    seen_links: set[str] = set()
    count = max(1, min(count, 50))
    pages = max(1, min(pages, 20))
    max_items = max(1, min(max_items, 300))
    start_page = max(0, int(start_page or 0))
    hard_filters = hard_filters or {}
    if llm_only_html and not getattr(llm_client, "enabled", False):
        warnings.append("superjob mixed html parsing is enabled but LLM client is disabled; automatic parser will still run.")

    if llm_only_html:
        tasks = []
        for query_index, query in enumerate(_query_variants(text)):
            for page in range(start_page, start_page + pages):
                params = _build_superjob_html_search_params(query=query, town=town, page=page + 1, hard_filters=hard_filters)
                tasks.append((query_index, page, query, f"{SUPERJOB_HTML_URL}?{urlencode(params)}"))
        if not tasks:
            return [], warnings, request_log

        def fetch_and_parse(query_index: int, page: int, query: str, url: str) -> tuple[int, int, list[dict[str, Any]], list[str], dict[str, Any]]:
            try:
                html, status = _get_text(url, timeout=timeout)
                log = {"source": "superjob", "method": "html", "url": url, "status": status, "ok": True}
            except Exception as error:  # noqa: BLE001
                return query_index, page, [], [f"superjob html request failed for page {page}: {_format_error(error)}"], {
                    "source": "superjob",
                    "method": "html",
                    "url": url,
                    "status": _error_status(error),
                    "ok": False,
                }
            with ThreadPoolExecutor(max_workers=2) as parser_executor:
                auto_future = parser_executor.submit(parse_superjob_html, html, query=query, max_items=min(count, max_items))
                llm_future = parser_executor.submit(
                    parse_html_with_llm,
                    source="superjob",
                    html=html,
                    query=query,
                    page_url=url,
                    max_items=min(count, max_items),
                    llm_client=llm_client,
                )
                auto_parsed = auto_future.result()
                llm_parsed = llm_future.result()
            parsed = merge_auto_and_llm_vacancies(
                source="superjob",
                auto_vacancies=auto_parsed,
                llm_vacancies=llm_parsed,
                max_items=min(count, max_items),
            )
            page_warnings = []
            if not auto_parsed:
                page_warnings.append(f"superjob automatic html parser found no vacancies on page {page}.")
            if not llm_parsed:
                page_warnings.append(f"superjob html LLM parsing found no vacancies on page {page}.")
            return query_index, page, parsed, page_warnings, log

        results = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(fetch_and_parse, query_index, page, query, url) for query_index, page, query, url in tasks]
            for future in as_completed(futures):
                results.append(future.result())

        for _, _, parsed, page_warnings, log in sorted(results, key=lambda item: (item[0], item[1])):
            request_log.append(log)
            warnings.extend(page_warnings)
            enriched, detail_warnings, detail_log = _enrich_superjob_html_details(parsed, timeout=timeout, fetch_details=fetch_details)
            warnings.extend(detail_warnings)
            request_log.extend(detail_log)
            for vacancy in enriched:
                link = str(vacancy.get("link") or vacancy.get("vacancy_id") or "").lower()
                if link and link in seen_links:
                    continue
                seen_links.add(link)
                vacancies.append(vacancy)
                if len(vacancies) >= max_items:
                    break
            if len(vacancies) >= max_items:
                break
        return vacancies[:max_items], warnings, request_log

    for query in _query_variants(text):
        for page in range(start_page, start_page + pages):
            if len(vacancies) >= max_items:
                break
            params = _build_superjob_html_search_params(query=query, town=town, page=page + 1, hard_filters=hard_filters)
            url = f"{SUPERJOB_HTML_URL}?{urlencode(params)}"
            try:
                html, status = _get_text(url, timeout=timeout)
                request_log.append({"source": "superjob", "method": "html", "url": url, "status": status, "ok": True})
            except Exception as error:  # noqa: BLE001
                warnings.append(f"superjob html request failed for page {page}: {_format_error(error)}")
                request_log.append({"source": "superjob", "method": "html", "url": url, "status": _error_status(error), "ok": False})
                break
            if llm_only_html:
                parsed = parse_html_with_llm(
                    source="superjob",
                    html=html,
                    query=query,
                    page_url=url,
                    max_items=max_items - len(vacancies),
                    llm_client=llm_client,
                )
                if not parsed:
                    warnings.append(f"superjob html LLM parsing found no vacancies on page {page}.")
            else:
                parsed = parse_superjob_html(html, query=query, max_items=max_items - len(vacancies))
                if not parsed:
                    if allow_llm_fallback:
                        llm_parsed = parse_html_with_llm(
                            source="superjob",
                            html=html,
                            query=query,
                            page_url=url,
                            max_items=max_items - len(vacancies),
                            llm_client=llm_client,
                        )
                        if llm_parsed:
                            warnings.append(f"superjob html was parsed by LLM fallback for page {page}.")
                            parsed = llm_parsed
                        else:
                            warnings.append(f"superjob html parser found no vacancies on page {page}.")
                    else:
                        warnings.append(f"superjob html parser found no vacancies on page {page}.")
            enriched, detail_warnings, detail_log = _enrich_superjob_html_details(parsed, timeout=timeout, fetch_details=fetch_details)
            warnings.extend(detail_warnings)
            request_log.extend(detail_log)
            for vacancy in enriched:
                link = str(vacancy.get("link") or vacancy.get("vacancy_id") or "").lower()
                if link and link in seen_links:
                    continue
                seen_links.add(link)
                vacancies.append(vacancy)
        if len(vacancies) >= max_items:
            break
    return vacancies[:max_items], warnings, request_log


def _build_superjob_html_search_params(
    *,
    query: str,
    town: str | None,
    page: int,
    hard_filters: dict[str, Any] | None,
) -> list[tuple[str, str | int]]:
    filters = hard_filters or {}
    params: list[tuple[str, str | int]] = [("keywords", query), ("page", page)]

    town_ids = _superjob_town_ids(filters)
    if town_ids:
        params.extend((f"geo[t][{index}]", town_id) for index, town_id in enumerate(town_ids))
    elif town:
        params.append(("geo[t][0]", town))

    stop_words = _filter_text(filters, "stop_words", "excluded")
    if stop_words:
        params.append(("excluded", stop_words))

    min_salary = _filter_int(filters, "min_salary")
    if min_salary:
        params.append(("payment_value", min_salary))
    if min_salary or _filter_bool(filters, "salary_defined", "income_specified", "payment_defined"):
        params.append(("payment_defined", "1"))

    if _superjob_profession_only(filters):
        params.append(("profession_only", "1"))

    for index, tag in enumerate(_superjob_tags(filters)):
        params.append((f"tag[{index}]", tag))
    for index, tag in enumerate(_superjob_work_format_tags(filters)):
        params.append((f"workFormatTag[{index}]", tag))
    for index, tag in enumerate(_superjob_employment_type_tags(filters)):
        params.append((f"employmentTypeTag[{index}]", tag))
    for index, tag in enumerate(_superjob_part_time_tags(filters)):
        params.append((f"partTimeJobTag[{index}]", tag))
    return params


def _has_superjob_service_filters(filters: dict[str, Any]) -> bool:
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
    )
    return any(_filter_has_value(filters, key) for key in keys)


def _superjob_town_ids(filters: dict[str, Any]) -> list[str]:
    values = _split_filter_values(filters.get("preferred_cities") or filters.get("town") or filters.get("geo"))
    if not values:
        return []
    town_map = {
        "москва": "4",
        "moscow": "4",
        "мск": "4",
        "санкт-петербург": "14",
        "санкт петербург": "14",
        "спб": "14",
        "saint petersburg": "14",
        "казань": "137",
        "новосибирск": "33",
        "екатеринбург": "33",
        "красногорск": "559",
        "апрелевка": "1476",
        "андреевка": "2656",
    }
    result: list[str] = []
    for value in values:
        normalized = _normalize_filter_token(value)
        if not normalized or normalized in {"удаленно", "remote"}:
            continue
        if re.fullmatch(r"\d+", normalized):
            result.append(normalized)
            continue
        mapped = town_map.get(normalized)
        if mapped:
            result.append(mapped)
    return _dedupe(result)


def _superjob_profession_only(filters: dict[str, Any]) -> bool:
    values = {_normalize_filter_token(value) for value in _split_filter_values(filters.get("search_fields") or filters.get("search_field"))}
    if not values:
        return False
    title_values = {"name", "title", "vacancy name", "название", "название вакансии"}
    description_values = {"description", "описание", "описание вакансии"}
    return bool(values & title_values) and not bool(values & description_values)


def _superjob_tags(filters: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in _split_filter_values(filters.get("preferred_levels") or filters.get("tags")):
        normalized = _normalize_filter_token(value)
        if normalized in {"internship", "intern", "стажировка", "стажер", "entry", "junior", "джуниор", "старт карьеры"}:
            result.append("18")
    return _dedupe(result)


def _superjob_work_format_tags(filters: dict[str, Any]) -> list[str]:
    format_map = {
        "onsite": "80",
        "office": "80",
        "офис": "80",
        "на объекте": "80",
        "remote": "81",
        "удаленно": "81",
        "удаленка": "81",
        "удаленная работа": "81",
        "hybrid": "82",
        "гибрид": "82",
        "field": "83",
        "field work": "83",
        "разъездной": "83",
        "разъездная": "83",
    }
    return _dedupe(format_map.get(_normalize_filter_token(value), "") for value in _split_filter_values(filters.get("preferred_formats") or filters.get("work_format")))


def _superjob_employment_type_tags(filters: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in _split_filter_values(filters.get("employment_contract") or filters.get("employment_type")):
        normalized = _normalize_filter_token(value)
        if normalized in {"labor contract", "employment contract", "трудовой договор", "тк", "full"}:
            result.append("72")
        if normalized in {"part", "part time", "частичная занятость"}:
            result.append("75")
        if normalized in {"gph", "gph or part time", "гпх", "совместительство"}:
            result.extend(["73", "114"])
        if normalized in {"internship", "стажировка"}:
            result.append("74")
        if normalized in {"practice", "практика"}:
            result.append("115")
    for value in _split_filter_values(filters.get("preferred_levels")):
        normalized = _normalize_filter_token(value)
        if normalized in {"internship", "intern", "стажировка", "стажер"}:
            result.extend(["74", "115"])
    return _dedupe(result)


def _superjob_part_time_tags(filters: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in _split_filter_values(filters.get("working_hours") or filters.get("working_hours_per_day")):
        normalized = _normalize_filter_token(value)
        digits = re.findall(r"\d+", normalized)
        if digits and int(digits[0]) <= 4:
            result.append("86")
        if normalized in {"evening", "night", "вечер", "вечером", "ночь", "ночью", "вечерние смены", "ночные смены"}:
            result.append("87")
    return _dedupe(result)


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


def _enrich_superjob_html_details(
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
        link = str(vacancy.get("link") or "")
        if not link:
            return index, vacancy, None, {}
        vacancy_id = _extract_superjob_vacancy_id(vacancy)
        try:
            html, status = _get_text(link, timeout=timeout)
            log = {"source": "superjob", "method": "html-detail", "url": link, "status": status, "ok": True}
            detail_vacancy = parse_superjob_detail_html(html, page_url=link, vacancy_id=vacancy_id)
        except Exception as error:  # noqa: BLE001
            return index, vacancy, f"superjob html detail failed for {vacancy_id or link}: {_format_error(error)}", {"source": "superjob", "method": "html-detail", "url": link, "status": _error_status(error), "ok": False}
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


def _extract_superjob_vacancy_id(vacancy: dict[str, Any]) -> str:
    for value in (vacancy.get("vacancy_id"), vacancy.get("link")):
        match = re.search(r"(\d+)", str(value or ""))
        if match:
            return match.group(1)
    return ""


def _merge_detail_over_search(search_vacancy: dict[str, Any], detail_vacancy: dict[str, Any]) -> dict[str, Any]:
    merged = dict(search_vacancy)
    for key, value in detail_vacancy.items():
        if value not in (None, ""):
            merged[key] = value
    detail_raw = str(detail_vacancy.get("raw_detail_text") or detail_vacancy.get("description") or "")
    if detail_raw:
        merged["raw_detail_text"] = detail_raw
    return merged


def _query_variants(text: str) -> list[str]:
    query = " ".join(str(text or "").split())
    variants = [query] if query else []
    translated = query
    for source_word, target_word in (
        ("developer", "разработчик"),
        ("engineer", "инженер"),
        ("analyst", "аналитик"),
        ("junior", "младший"),
        ("middle", ""),
    ):
        translated = re.sub(rf"\b{re.escape(source_word)}\b", target_word, translated, flags=re.IGNORECASE)
    translated = " ".join(translated.split())
    if translated and translated.lower() not in {item.lower() for item in variants}:
        variants.append(translated)
    return variants[:2]


def _get_json(url: str, *, headers: dict[str, str], timeout: int) -> tuple[Any, int]:
    request = Request(url, headers=headers)
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


def _map_superjob_item(item: dict[str, Any]) -> dict[str, Any]:
    town = item.get("town") or {}
    work_type = item.get("type_of_work") or item.get("place_of_work") or {}
    catalogues = item.get("catalogues") or []
    catalogue_titles = []
    for catalogue in catalogues:
        if isinstance(catalogue, dict):
            catalogue_titles.append(catalogue.get("title"))
    published = item.get("date_published") or item.get("date_pub_to") or ""
    if isinstance(published, int):
        published = datetime.fromtimestamp(published).strftime("%Y-%m-%d")
    title = str(item.get("profession") or item.get("title") or "")
    requirements = clean_html(item.get("candidat"))
    responsibilities = clean_html(item.get("work"))
    conditions = clean_html(item.get("compensation"))
    description = clean_html(join_values([requirements, responsibilities, conditions]))
    return {
        "vacancy_id": f"superjob:{item.get('id') or ''}",
        "source": "superjob",
        "title": title,
        "company": item.get("firm_name") or item.get("client", {}).get("title") if isinstance(item.get("client"), dict) else item.get("firm_name") or "",
        "role": title,
        "level": normalize_level(join_values([title, description])),
        "format": normalize_format(join_values([work_type.get("title") if isinstance(work_type, dict) else work_type, item.get("place_of_work")])) ,
        "city": town.get("title") if isinstance(town, dict) else str(town or ""),
        "relocation_possible": "",
        "published_at": published,
        "deadline": "",
        "salary_rub": normalize_salary({
            "from": item.get("payment_from"),
            "to": item.get("payment_to"),
            "currency": item.get("currency") or "RUB",
        }),
        "stack": description,
        "key_skills": join_values(catalogue_titles),
        "english_level": "",
        "link": item.get("link") or "",
        "description": description,
        "requirements": requirements,
        "responsibilities": responsibilities,
        "conditions": conditions,
        "employment_type": work_type.get("title") if isinstance(work_type, dict) else str(work_type or ""),
    }
