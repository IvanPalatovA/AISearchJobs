from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import ssl
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import FetchResult, join_values, normalize_format, normalize_level, normalize_salary
from .html_source import merge_auto_and_llm_vacancies, parse_generic_search_html, parse_html_with_llm

HTML_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36"


@dataclass(frozen=True, slots=True)
class HTMLSourceConfig:
    source: str
    base_url: str
    build_url: Callable[[str, int, int], str]
    link_patterns: tuple[str, ...]
    company_href_parts: tuple[str, ...] = ()


def _url(base: str, params: dict[str, Any] | list[tuple[str, Any]]) -> str:
    return f"{base}?{urlencode(params)}"


HTML_SOURCE_CONFIGS: dict[str, HTMLSourceConfig] = {
    "rabota_ru": HTMLSourceConfig(
        source="rabota_ru",
        base_url="https://www.rabota.ru",
        build_url=lambda text, page, per_page: _url("https://www.rabota.ru/vacancy/", {"query": text, "page": page + 1}),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://www\.rabota\.ru)?/vacancy/(?P<id>[^"#?]+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/company/", "/companies/", "/employer/"),
    ),
    "avito": HTMLSourceConfig(
        source="avito",
        base_url="https://www.avito.ru",
        build_url=lambda text, page, per_page: _url("https://www.avito.ru/rossiya/vakansii", {"q": text, "p": page + 1}),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://www\.avito\.ru)?/[^"]*/vakansii/[^"]*_(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/brands/", "/company/", "/rabotodateli/"),
    ),
    "zarplata": HTMLSourceConfig(
        source="zarplata",
        base_url="https://www.zarplata.ru",
        build_url=lambda text, page, per_page: _url("https://www.zarplata.ru/search/vacancy", {"text": text, "page": page + 1}),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://(?:www\.)?zarplata\.ru)?/vacancy/(?P<id>[^"#?]+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/employer/", "/company/"),
    ),
    "gorodrabot": HTMLSourceConfig(
        source="gorodrabot",
        base_url="https://russia.gorodrabot.ru",
        build_url=lambda text, page, per_page: _url(
            "https://russia.gorodrabot.ru/",
            {"q": text, "p": page + 1},
        ),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://(?:russia\.)?gorodrabot\.ru)?/vacancy/(?P<id>[^"#?]+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
            r'<a[^>]+href="(?P<href>(?:https?://(?:russia\.)?gorodrabot\.ru)?/[^"]*?(?P<id>\d+)[^"]*)"[^>]*(?:class="[^"]*(?:vacancy|snippet|result)[^"]*"[^>]*)?>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/company/", "/companies/"),
    ),
    "jooble": HTMLSourceConfig(
        source="jooble",
        base_url="https://ru.jooble.org",
        build_url=lambda text, page, per_page: _url("https://ru.jooble.org/SearchResult", {"ukw": text, "p": page + 1}),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://ru\.jooble\.org)?/desc/(?P<id>[^"#?]+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
            r'<a[^>]+href="(?P<href>(?:https?://ru\.jooble\.org)?/vacancies/[^"#?]+(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/company/", "/companies/"),
    ),
    "habr": HTMLSourceConfig(
        source="habr",
        base_url="https://career.habr.com",
        build_url=lambda text, page, per_page: _url("https://career.habr.com/vacancies", {"q": text, "page": page + 1}),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://career\.habr\.com)?/vacancies/(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/companies/",),
    ),
    "geekjob": HTMLSourceConfig(
        source="geekjob",
        base_url="https://geekjob.ru",
        build_url=lambda text, page, per_page: _url("https://geekjob.ru/vacancies", {"qs": text, "page": page + 1}),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://geekjob\.ru)?/vacancy/(?P<id>[^"#?]+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/company/", "/companies/"),
    ),
    "trudvsem": HTMLSourceConfig(
        source="trudvsem",
        base_url="https://trudvsem.ru",
        build_url=lambda text, page, per_page: _url(
            "https://trudvsem.ru/vacancy/search",
            [("_title", text), ("page", page), ("titleType", "VACANCY_NAME"), ("salary", "0"), ("salary", "999999")],
        ),
        link_patterns=(
            r'<a[^>]+href="(?P<href>(?:https?://trudvsem\.ru)?/vacancy/card/(?P<id>[^"#?]+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
            r'<a[^>]+href="(?P<href>(?:https?://trudvsem\.ru)?/vacancy/(?P<id>[^"#?/]+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        ),
        company_href_parts=("/employer/", "/company/"),
    ),
}

SOURCE_ALIASES = {
    "rabota": "rabota_ru",
    "rabota.ru": "rabota_ru",
    "работа.ру": "rabota_ru",
    "avito_work": "avito",
    "avito-rabota": "avito",
    "zarplata.ru": "zarplata",
    "gorodrabot.ru": "gorodrabot",
    "habr_career": "habr",
    "habr-career": "habr",
    "career_habr": "habr",
    "geekjob.ru": "geekjob",
    "rabota_russia": "trudvsem",
    "rabota-rossii": "trudvsem",
    "rabota_rosii": "trudvsem",
    "работа-россии": "trudvsem",
    "trudvsem.ru": "trudvsem",
}


def supported_html_sources() -> set[str]:
    return set(HTML_SOURCE_CONFIGS)


def canonical_html_source(source: str) -> str:
    normalized = str(source or "").strip().lower()
    return SOURCE_ALIASES.get(normalized, normalized)


def fetch_generic_html_vacancies(
    *,
    source: str,
    text: str,
    per_page: int = 20,
    pages: int = 1,
    timeout: int = 15,
    llm_client: Any | None = None,
    llm_only_html: bool = False,
    allow_llm_fallback: bool = True,
    max_items: int = 50,
    start_page: int = 0,
) -> FetchResult:
    source = canonical_html_source(source)
    config = HTML_SOURCE_CONFIGS.get(source)
    if not config:
        return FetchResult(source=source, vacancies=[], warnings=[f"Unknown HTML source skipped: {source}"])
    if source == "geekjob" and not llm_only_html:
        return _fetch_geekjob_vacancies(
            text=text,
            per_page=per_page,
            pages=pages,
            timeout=timeout,
            max_items=max_items,
            start_page=start_page,
        )

    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []
    vacancies: list[dict[str, Any]] = []
    seen_links: set[str] = set()
    per_page = max(1, min(per_page, 50))
    pages = max(1, min(pages, 20))
    max_items = max(1, min(max_items, 300))

    if llm_only_html and not getattr(llm_client, "enabled", False):
        warnings.append(f"{source} mixed html parsing is enabled but LLM client is disabled; automatic parser will still run.")

    start_page = max(0, int(start_page or 0))

    if llm_only_html:
        tasks = []
        for query_index, query in enumerate(_query_variants(source, text)):
            for page in range(start_page, start_page + pages):
                url = config.build_url(query, page, min(per_page, max_items))
                tasks.append((query_index, page, query, url))
        if not tasks:
            return FetchResult(source=source, vacancies=[], warnings=warnings, request_log=request_log)

        def fetch_and_parse(query_index: int, page: int, query: str, url: str) -> tuple[int, int, list[dict[str, Any]], list[str], dict[str, Any]]:
            try:
                html, status = _get_text(url, timeout=timeout)
                blocked_reason = _blocked_page_reason(source, html)
                log = {"source": source, "method": "html", "url": url, "status": blocked_reason or status, "ok": not blocked_reason}
                if blocked_reason:
                    return query_index, page, [], [f"{source} html is blocked for page {page}: {blocked_reason}"], log
            except Exception as error:  # noqa: BLE001
                return query_index, page, [], [f"{source} html request failed for page {page}: {_format_error(error)}"], {
                    "source": source,
                    "method": "html",
                    "url": url,
                    "status": _error_status(error),
                    "ok": False,
                }
            with ThreadPoolExecutor(max_workers=2) as parser_executor:
                auto_future = parser_executor.submit(
                    parse_generic_search_html,
                    html,
                    source=source,
                    base_url=config.base_url,
                    link_patterns=config.link_patterns,
                    query=query,
                    max_items=min(per_page, max_items),
                    company_href_parts=config.company_href_parts,
                )
                llm_future = parser_executor.submit(
                    parse_html_with_llm,
                    source=source,
                    html=html,
                    query=query,
                    page_url=url,
                    max_items=min(per_page, max_items),
                    llm_client=llm_client,
                )
                auto_parsed = auto_future.result()
                llm_parsed = llm_future.result()
            parsed = merge_auto_and_llm_vacancies(
                source=source,
                auto_vacancies=auto_parsed,
                llm_vacancies=llm_parsed,
                max_items=min(per_page, max_items),
            )
            page_warnings = []
            if not auto_parsed:
                page_warnings.append(f"{source} automatic html parser found no vacancies on page {page}.")
            if not llm_parsed:
                page_warnings.append(f"{source} html LLM parsing found no vacancies on page {page}.")
            return query_index, page, parsed, page_warnings, log

        results = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(fetch_and_parse, query_index, page, query, url) for query_index, page, query, url in tasks]
            for future in as_completed(futures):
                results.append(future.result())

        for _, _, parsed, page_warnings, log in sorted(results, key=lambda item: (item[0], item[1])):
            request_log.append(log)
            warnings.extend(page_warnings)
            for vacancy in parsed:
                link = str(vacancy.get("link") or vacancy.get("vacancy_id") or "").lower()
                if link and link in seen_links:
                    continue
                seen_links.add(link)
                vacancies.append(vacancy)
                if len(vacancies) >= max_items:
                    break
            if len(vacancies) >= max_items:
                break
        return FetchResult(source=source, vacancies=vacancies[:max_items], warnings=warnings, request_log=request_log)

    for query in _query_variants(source, text):
        for page in range(start_page, start_page + pages):
            if len(vacancies) >= max_items:
                break
            url = config.build_url(query, page, min(per_page, max_items - len(vacancies)))
            try:
                html, status = _get_text(url, timeout=timeout)
                blocked_reason = _blocked_page_reason(source, html)
                request_log.append({"source": source, "method": "html", "url": url, "status": blocked_reason or status, "ok": not blocked_reason})
                if blocked_reason:
                    warnings.append(f"{source} html is blocked for page {page}: {blocked_reason}")
                    break
            except Exception as error:  # noqa: BLE001
                warnings.append(f"{source} html request failed for page {page}: {_format_error(error)}")
                request_log.append({"source": source, "method": "html", "url": url, "status": _error_status(error), "ok": False})
                break

            if llm_only_html:
                parsed = parse_html_with_llm(
                    source=source,
                    html=html,
                    query=query,
                    page_url=url,
                    max_items=max_items - len(vacancies),
                    llm_client=llm_client,
                )
                if not parsed:
                    warnings.append(f"{source} html LLM parsing found no vacancies on page {page}.")
            else:
                parsed = parse_generic_search_html(
                    html,
                    source=source,
                    base_url=config.base_url,
                    link_patterns=config.link_patterns,
                    query=query,
                    max_items=max_items - len(vacancies),
                    company_href_parts=config.company_href_parts,
                )
                if not parsed:
                    if allow_llm_fallback:
                        llm_parsed = parse_html_with_llm(
                            source=source,
                            html=html,
                            query=query,
                            page_url=url,
                            max_items=max_items - len(vacancies),
                            llm_client=llm_client,
                        )
                        if llm_parsed:
                            warnings.append(f"{source} html was parsed by LLM fallback for page {page}.")
                            parsed = llm_parsed
                        else:
                            warnings.append(f"{source} html parser found no vacancies on page {page}.")
                    else:
                        warnings.append(f"{source} html parser found no vacancies on page {page}.")
            for vacancy in parsed:
                link = str(vacancy.get("link") or vacancy.get("vacancy_id") or "").lower()
                if link and link in seen_links:
                    continue
                seen_links.add(link)
                vacancies.append(vacancy)
        if len(vacancies) >= max_items:
            break
    return FetchResult(source=source, vacancies=vacancies[:max_items], warnings=warnings, request_log=request_log)


def _fetch_geekjob_vacancies(
    *,
    text: str,
    per_page: int = 20,
    pages: int = 1,
    timeout: int = 15,
    max_items: int = 50,
    start_page: int = 0,
) -> FetchResult:
    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []
    vacancies: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = max(1, min(pages, 20))
    max_items = max(1, min(max_items, 300))
    start_page = max(0, int(start_page or 0))

    for query in _query_variants("geekjob", text):
        for page in range(start_page + 1, start_page + pages + 1):
            if len(vacancies) >= max_items:
                break
            url = _url("https://geekjob.ru/json/find/vacancy", {"page": page, "qs": query})
            try:
                payload, status = _get_json(url, timeout=timeout)
                request_log.append({"source": "geekjob", "method": "json", "url": url, "status": status, "ok": True})
            except Exception as error:  # noqa: BLE001
                warnings.append(f"geekjob json request failed for page {page}: {_format_error(error)}")
                request_log.append({"source": "geekjob", "method": "json", "url": url, "status": _error_status(error), "ok": False})
                break
            if not isinstance(payload, dict):
                warnings.append(f"geekjob: unexpected JSON response for page {page}")
                break
            items = payload.get("data") or []
            if not isinstance(items, list) or not items:
                warnings.append(f"geekjob json parser found no vacancies on page {page}.")
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                vacancy = _map_geekjob_item(item, query)
                key = str(vacancy.get("vacancy_id") or vacancy.get("link") or "").lower()
                if key in seen:
                    continue
                seen.add(key)
                vacancies.append(vacancy)
                if len(vacancies) >= max_items:
                    break
            if not payload.get("nextpage"):
                break
        if len(vacancies) >= max_items:
            break
    return FetchResult(source="geekjob", vacancies=vacancies[:max_items], warnings=warnings, request_log=request_log)


def _map_geekjob_item(item: dict[str, Any], query: str) -> dict[str, Any]:
    title = str(item.get("position") or "").strip()
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    job_format = item.get("jobFormat") if isinstance(item.get("jobFormat"), dict) else {}
    location = join_values([item.get("country"), item.get("city")])
    description = join_values([title, location, item.get("salary")])
    vacancy_id = str(item.get("id") or "").strip()
    format_hint = " ".join(key for key, enabled in job_format.items() if enabled)
    return {
        "vacancy_id": f"geekjob-json:{vacancy_id}",
        "source": "geekjob-html",
        "title": title,
        "company": company.get("name") or "",
        "role": title,
        "level": normalize_level(f"{title} {query}"),
        "format": normalize_format(format_hint),
        "city": location,
        "relocation_possible": "yes" if job_format.get("relocate") else "",
        "published_at": (item.get("log") or {}).get("modify") if isinstance(item.get("log"), dict) else "",
        "deadline": "",
        "salary_rub": normalize_salary(item.get("salary")),
        "stack": query,
        "key_skills": "",
        "english_level": "",
        "link": f"https://geekjob.ru/vacancy/{vacancy_id}" if vacancy_id else "https://geekjob.ru/vacancies",
        "description": description,
    }


def _query_variants(source: str, text: str) -> list[str]:
    query = " ".join(str(text or "").split())
    if not query:
        return []
    variants = [query]
    lower = query.lower()
    replacements = [
        ("developer", "разработчик"),
        ("engineer", "инженер"),
        ("analyst", "аналитик"),
        ("junior", "младший"),
        ("middle", ""),
    ]
    if source in {"superjob", "rabota_ru", "zarplata", "gorodrabot", "trudvsem", "geekjob"}:
        translated = query
        for source_word, target_word in replacements:
            translated = re_sub_word(translated, source_word, target_word)
        translated = " ".join(translated.split())
        mixed = query
        for source_word, target_word in replacements[:3]:
            mixed = re_sub_word(mixed, source_word, target_word)
        mixed = " ".join(mixed.split())
        for candidate in (mixed, translated):
            if candidate and candidate.lower() not in {item.lower() for item in variants}:
                variants.append(candidate)
        if "analyst" in lower and "аналитик" not in {item.lower() for item in variants}:
            variants.append("аналитик")
        if "developer" in lower and "разработчик" not in {item.lower() for item in variants}:
            variants.append("разработчик")
    return variants[:3]


def re_sub_word(text: str, source: str, target: str) -> str:
    import re

    return re.sub(rf"\b{re.escape(source)}\b", target, text, flags=re.IGNORECASE)


def _get_text(url: str, *, timeout: int) -> tuple[str, int]:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": HTML_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured public search pages.
            return response.read().decode("utf-8", errors="replace"), response.status
    except Exception as error:
        if not _is_ssl_certificate_error(error):
            raise
        context = ssl._create_unverified_context()  # noqa: S323 - fallback for broken local CA chain, not captcha/auth bypass.
        with urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            return response.read().decode("utf-8", errors="replace"), response.status


def _get_json(url: str, *, timeout: int) -> tuple[Any, int]:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://geekjob.ru/vacancies",
            "User-Agent": HTML_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured public JSON endpoint.
            return json.loads(response.read().decode("utf-8", errors="replace")), response.status
    except Exception as error:
        if not _is_ssl_certificate_error(error):
            raise
        context = ssl._create_unverified_context()  # noqa: S323 - fallback for broken local CA chain, not captcha/auth bypass.
        with urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", errors="replace")), response.status


def _blocked_page_reason(source: str, html: str) -> str:
    text = html[:5000].lower()
    if source == "jooble" and "что происходит в россии" in text:
        return "blocked_region"
    if source == "avito" and ("доступ ограничен" in text or "too many requests" in text):
        return "blocked_or_rate_limited"
    return ""


def _is_ssl_certificate_error(error: Exception) -> bool:
    reason = getattr(error, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(error)


def _format_error(error: Exception) -> str:
    if isinstance(error, HTTPError):
        body = error.read().decode("utf-8", errors="replace").strip()
        return f"HTTP {error.code}: {body[:300]}"
    return f"{type(error).__name__}: {error}"


def _error_status(error: Exception) -> int | str:
    return error.code if isinstance(error, HTTPError) else type(error).__name__
