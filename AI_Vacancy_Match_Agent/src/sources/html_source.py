from __future__ import annotations

from datetime import datetime
from html import unescape
import json
import re
from typing import Any
from urllib.parse import urljoin

from .base import clean_text, join_values, normalize_format, normalize_level, normalize_salary
from .quality import clean_vacancy_fields, is_noisy_vacancy


def parse_hh_html(html: str, *, query: str, max_items: int = 50) -> list[dict[str, Any]]:
    vacancies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in _hh_search_card_blocks(html):
        if len(vacancies) >= max_items:
            break
        vacancy = _parse_hh_search_card(block, query=query)
        if not vacancy:
            continue
        key = _vacancy_merge_key(vacancy)
        if key in seen:
            continue
        seen.add(key)
        if not is_noisy_vacancy(vacancy):
            vacancies.append(vacancy)

    pattern = re.compile(
        r'<a[^>]+href="(?P<href>https?://hh\.ru/vacancy/(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        if len(vacancies) >= max_items:
            break
        vacancy_id = match.group("id")
        if f"id:{vacancy_id}" in seen:
            continue
        title = clean_text(match.group("title"))
        if not title or _looks_like_navigation(title):
            continue
        block = _hh_vacancy_block(html, match.start(), match.end())
        description = _clean_scraped_text(block)
        vacancy = clean_vacancy_fields(
            {
                "vacancy_id": f"hh-html:{vacancy_id}",
                "source": "hh-html",
                "title": title,
                "company": _extract_first_anchor_text(block, ("/employer/", "hh.ru/employer/")),
                "role": title,
                "level": normalize_level(f"{title} {description[:800]}"),
                "format": normalize_format(description[:1200]),
                "city": _extract_city(description),
                "relocation_possible": "",
                "published_at": "",
                "deadline": "",
                "salary_rub": normalize_salary(_extract_salary_text(block)),
                "salary_text": _extract_salary_text(block),
                "payment_frequency": _extract_hh_payment_frequency(block),
                "stack": _extract_stack_hint(block, query),
                "key_skills": "",
                "english_level": "",
                "link": match.group("href").replace("&amp;", "&"),
                "description": description[:1200],
                "experience": _extract_hh_experience(block),
                "work_format": normalize_format(description[:1200]),
                "views_count": _extract_hh_views_count(block),
                "raw_detail_text": description,
                "detail_source": "hh-search-html",
            }
        )
        key = _vacancy_merge_key(vacancy) or f"id:{vacancy_id}"
        if key in seen:
            continue
        seen.add(key)
        if not is_noisy_vacancy(vacancy):
            vacancies.append(vacancy)
    return vacancies


def parse_superjob_html(html: str, *, query: str, max_items: int = 50) -> list[dict[str, Any]]:
    vacancies: list[dict[str, Any]] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<a[^>]+href="(?P<href>(?:https?://www\.superjob\.ru)?/vakansii/[^"]*?(?P<id>\d+)\.html[^"]*)"[^>]*>(?P<title>.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        if len(vacancies) >= max_items:
            break
        vacancy_id = match.group("id")
        if vacancy_id in seen:
            continue
        seen.add(vacancy_id)
        title = clean_text(match.group("title"))
        if not title or _looks_like_navigation(title):
            continue
        block = _vacancy_card_block(html, match.start(), match.end(), fallback_radius=1100)
        description = _clean_scraped_text(block)
        link = urljoin("https://www.superjob.ru", match.group("href").replace("&amp;", "&"))
        vacancy = clean_vacancy_fields(
            {
                "vacancy_id": f"superjob-html:{vacancy_id}",
                "source": "superjob-html",
                "title": title,
                "company": _extract_first_anchor_text(block, ("/clients/", "superjob.ru/clients/")),
                "role": title,
                "level": normalize_level(f"{title} {description[:800]}"),
                "format": normalize_format(description[:1200]),
                "city": _extract_city(description),
                "relocation_possible": "",
                "published_at": "",
                "deadline": "",
                "salary_rub": normalize_salary(_extract_salary_text(block)),
                "stack": _extract_stack_hint(block, query),
                "key_skills": "",
                "english_level": "",
                "link": link,
                "description": description[:1200],
            }
        )
        if not is_noisy_vacancy(vacancy):
            vacancies.append(vacancy)
    return vacancies


def parse_hh_detail_html(html: str, *, page_url: str = "") -> dict[str, Any] | None:
    state = _extract_hh_lux_state(html)
    if isinstance(state, dict):
        parsed = _parse_hh_detail_state(state, page_url=page_url)
        if parsed:
            return _augment_hh_detail_with_visible_html(parsed, html)

    title = _extract_data_qa_text(html, "vacancy-title") or _extract_meta_title(html)
    description_block = _extract_balanced_block_by_data_qa(html, "vacancy-description")
    description = _clean_multiline_text(description_block)
    if not description:
        description = _clean_multiline_text(_relevant_detail_fragment(html, ("Обязанности", "Требования", "Условия", "Ключевые навыки")))
    if not title and not description:
        return None
    if _looks_like_ddos_or_error_page(title, description):
        return None

    company = _extract_data_qa_text(html, "vacancy-company-name") or _extract_first_anchor_text(html, ("/employer/", "hh.ru/employer/"))
    salary_text = _extract_hh_salary_text_from_html(html)
    key_skills = _extract_hh_key_skills(html)
    raw_text = _clean_multiline_text(_relevant_detail_fragment(html, (title, "Ключевые навыки", "Где предстоит работать", "Вакансия опубликована")))
    if description and description not in raw_text:
        raw_text = join_nonempty((raw_text, description))
    raw_detail_text = join_nonempty((raw_text, description))
    sections = split_ru_vacancy_sections(description)
    link = page_url or _extract_canonical_url(html)
    vacancy_id = _extract_hh_id_from_link(link) or _extract_hh_card_id(html)
    address = _extract_hh_address(raw_text)
    vacancy = clean_vacancy_fields(
        {
            "vacancy_id": f"hh-html:{vacancy_id}" if vacancy_id else f"hh-html:{abs(hash((title, link))) % 10_000_000}",
            "source": "hh-html-detail",
            "title": title,
            "company": company,
            "role": title,
            "level": normalize_level(join_nonempty((_extract_hh_experience(raw_text), title, description[:500]))),
            "format": normalize_format(raw_text),
            "city": _extract_city(raw_text),
            "relocation_possible": "",
            "published_at": "",
            "deadline": "",
            "salary_rub": normalize_salary(salary_text),
            "salary_text": salary_text,
            "payment_frequency": _extract_hh_payment_frequency(raw_text),
            "stack": join_nonempty((join_values(key_skills), _extract_stack_hint(join_nonempty((description, raw_detail_text)), title))),
            "key_skills": join_values(key_skills),
            "english_level": "",
            "link": link,
            "description": description or raw_text,
            "requirements": sections.get("requirements", ""),
            "responsibilities": sections.get("responsibilities", ""),
            "conditions": sections.get("conditions", ""),
            "employment_type": _extract_hh_employment(raw_text),
            "employment_form": _extract_labeled_value(raw_text, "Оформление"),
            "experience": _extract_hh_experience(raw_text),
            "schedule": _extract_labeled_value(raw_text, "График"),
            "working_hours": _extract_labeled_value(raw_text, "Рабочие часы"),
            "work_format": _extract_labeled_value(raw_text, "Формат работы") or normalize_format(raw_text),
            "address": address,
            "metro_stations": _extract_hh_metro(raw_text),
            "employer_name": company,
            "category": "",
            "published_at_text": _extract_hh_published_text(raw_text),
            "views_count": _extract_hh_views_count(raw_text),
            "detail_source": "hh-detail-html",
            "raw_detail_text": raw_detail_text,
        }
    )
    vacancy = _augment_hh_detail_with_visible_html(vacancy, html)
    return vacancy if not is_noisy_vacancy(vacancy) else None


def parse_superjob_detail_html(html: str, *, page_url: str = "", vacancy_id: str | None = None) -> dict[str, Any] | None:
    state = _extract_json_assignment(html, "window.APP_STATE")
    if isinstance(state, dict):
        parsed = _parse_superjob_detail_state(state, page_url=page_url, vacancy_id=vacancy_id)
        if parsed:
            return parsed
    return _parse_superjob_detail_visible_html(html, page_url=page_url, vacancy_id=vacancy_id)


def parse_generic_search_html(
    html: str,
    *,
    source: str,
    base_url: str,
    link_patterns: tuple[str, ...],
    query: str,
    max_items: int = 50,
    company_href_parts: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    vacancies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_pattern in link_patterns:
        pattern = re.compile(raw_pattern, flags=re.IGNORECASE | re.DOTALL)
        for match in pattern.finditer(html):
            if len(vacancies) >= max_items:
                break
            groups = match.groupdict()
            href = str(groups.get("href") or "").replace("&amp;", "&")
            if not href:
                continue
            link = urljoin(base_url, href)
            vacancy_id = clean_text(groups.get("id")) or _stable_link_id(link)
            title = clean_text(groups.get("title")) or _extract_attr(match.group(0), "aria-label") or _extract_attr(match.group(0), "title")
            if not title or _looks_like_navigation(title):
                continue
            if vacancy_id in seen:
                continue
            seen.add(vacancy_id)
            block = _vacancy_card_block(html, match.start(), match.end(), fallback_radius=1100)
            description = _clean_scraped_text(block)
            vacancy = clean_vacancy_fields(
                {
                    "vacancy_id": f"{source}-html:{vacancy_id}",
                    "source": f"{source}-html",
                    "title": title,
                    "company": _extract_first_anchor_text(block, company_href_parts) if company_href_parts else "",
                    "role": title,
                    "level": normalize_level(f"{title} {description[:800]}"),
                    "format": normalize_format(description[:1200]),
                    "city": _extract_city(description),
                    "relocation_possible": "",
                    "published_at": "",
                    "deadline": "",
                    "salary_rub": normalize_salary(_extract_salary_text(block)),
                    "stack": _extract_stack_hint(block, query),
                    "key_skills": "",
                    "english_level": "",
                    "link": link,
                    "description": description[:1200],
                }
            )
            if not is_noisy_vacancy(vacancy):
                vacancies.append(vacancy)
        if len(vacancies) >= max_items:
            break
    return vacancies


def join_nonempty(values: Any, *, sep: str = " ") -> str:
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_scraped_text(value)
        key = _normalize_merge_text(text)
        if not text or key in seen:
            continue
        seen.add(key)
        parts.append(text)
    return sep.join(parts).strip()


def split_ru_vacancy_sections(text: str) -> dict[str, str]:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not cleaned.strip():
        return {"intro": "", "requirements": "", "responsibilities": "", "conditions": ""}

    heading_map = [
        ("responsibilities", r"обязанности|задачи|вам предстоит|чем предстоит заниматься|ваши задачи|что предстоит делать|ключевые обязанности"),
        ("requirements", r"требования|вы нам подходите,?\s*если|от кандидата ожидаем|мы жд[её]м от вас|что мы жд[её]м от тебя|что мы жд[её]м|чего мы жд[её]м(?: от [^:\n?]+)?|чего мы ожидаем|что мы ожидаем|что ожидаем|что мы от вас ожидаем|ожидания|квалификации|базовые требования|предпочтительные квалификации|будет плюсом|будет преимуществом|плюсом будет|какой опыт нужен|какие навыки нужны"),
        ("conditions", r"условия|мы предлагаем|что мы предлагаем|что предлагаем|для вас|почему именно у нас|почему это будет интересно|почему это интересно"),
    ]
    bare_heading_regex = "|".join(f"(?:{pattern})" for _, pattern in heading_map)
    cleaned = re.sub(
        rf"(?i)(?<!\n)\s+({bare_heading_regex})\s*[:?]",
        r"\n\1:\n",
        cleaned,
    )
    cleaned = re.sub(
        rf"(?i)^({bare_heading_regex})\s*[:?]",
        r"\1:\n",
        cleaned,
    )
    heading_regex = "|".join(f"(?P<{name}_{index}>{pattern})" for index, (name, pattern) in enumerate(heading_map))
    pattern = re.compile(rf"(?im)^\s*(?:[-*•]\s*)?(?:{heading_regex})\s*[:?]?\s*$")
    matches = list(pattern.finditer(cleaned))
    result: dict[str, list[str]] = {"intro": [], "requirements": [], "responsibilities": [], "conditions": []}
    if not matches:
        return {"intro": cleaned.strip(), "requirements": "", "responsibilities": "", "conditions": ""}

    first_start = matches[0].start()
    if first_start > 0:
        result["intro"].append(cleaned[:first_start].strip())
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        heading = match.group(0)
        section_text = cleaned[match.end() : end].strip()
        name = "intro"
        for group_name, value in match.groupdict().items():
            if value:
                name = group_name.rsplit("_", 1)[0]
                break
        if section_text:
            result[name].append(join_nonempty((heading.strip(), section_text)))
    return {key: "\n\n".join(value for value in values if value).strip() for key, values in result.items()}


def _hh_search_card_blocks(html: str) -> list[str]:
    pattern = re.compile(
        r'<(?P<tag>div|article)\b(?=[^>]*(?:data-qa=["\'][^"\']*vacancy-serp__vacancy|class=["\'][^"\']*(?:vacancy-card|serp-item)[^"\']*))[^>]*>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    blocks: list[str] = []
    for match in pattern.finditer(html):
        tag = match.group("tag").lower()
        end = _balanced_tag_end(html, match.start(), tag) or min(len(html), match.start() + 6500)
        block = html[match.start() : min(end, match.start() + 9000)]
        if _extract_hh_card_title(block) or _extract_hh_card_id(block):
            blocks.append(block)
    return blocks


def _parse_hh_search_card(block: str, *, query: str) -> dict[str, Any] | None:
    vacancy_id = _extract_hh_card_id(block)
    href = _extract_hh_card_href(block)
    if not vacancy_id:
        vacancy_id = _extract_hh_id_from_link(href)
    title = _extract_hh_card_title(block)
    if not title or _looks_like_navigation(title):
        return None
    description = _clean_scraped_text(block)
    link = _hh_direct_vacancy_link(vacancy_id, href)
    salary_text = _extract_salary_text(block)
    company = _extract_first_anchor_text(block, ("/employer/", "hh.ru/employer/"))
    return clean_vacancy_fields(
        {
            "vacancy_id": f"hh-html:{vacancy_id or abs(hash((title, link))) % 10_000_000}",
            "source": "hh-html",
            "title": title,
            "company": company,
            "role": title,
            "level": normalize_level(join_nonempty((_extract_hh_experience(block), title, description[:800]))),
            "format": normalize_format(description[:1200]),
            "city": _extract_city(description),
            "relocation_possible": "",
            "published_at": "",
            "deadline": "",
            "salary_rub": normalize_salary(salary_text),
            "salary_text": salary_text,
            "payment_frequency": _extract_hh_payment_frequency(block),
            "stack": _extract_stack_hint(block, query),
            "key_skills": "",
            "english_level": "",
            "link": link,
            "description": description[:1200],
            "experience": _extract_hh_experience(block),
            "work_format": normalize_format(description[:1200]),
            "employer_name": company,
            "views_count": _extract_hh_views_count(block),
            "detail_source": "hh-search-html",
            "raw_detail_text": description,
        }
    )


def _extract_hh_card_id(block: str) -> str:
    patterns = (
        r'\bid=["\'](?P<id>\d{5,})["\']',
        r'\bdata-vacancy-id=["\'](?P<id>\d{5,})["\']',
        r'vacancyId=(?P<id>\d{5,})',
        r'/vacancy/(?P<id>\d{5,})',
    )
    for pattern in patterns:
        match = re.search(pattern, block, flags=re.IGNORECASE)
        if match:
            return match.group("id")
    return ""


def _extract_hh_id_from_link(link: str) -> str:
    match = re.search(r"/vacancy/(?P<id>\d{5,})", str(link or ""), flags=re.IGNORECASE)
    return match.group("id") if match else ""


def _extract_hh_card_href(block: str) -> str:
    patterns = (
        r'<a[^>]+data-qa=["\']serp-item__title["\'][^>]+href=["\'](?P<href>[^"\']+)["\']',
        r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]+data-qa=["\']serp-item__title["\']',
        r'<a[^>]+href=["\'](?P<href>https?://hh\.ru/vacancy/\d+[^"\']*)["\']',
        r'<a[^>]+href=["\'](?P<href>/vacancy/\d+[^"\']*)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, block, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group("href").replace("&amp;", "&")
    return ""


def _extract_hh_card_title(block: str) -> str:
    patterns = (
        r'<span[^>]+data-qa=["\']serp-item__title-text["\'][^>]*>(?P<title>.*?)</span>',
        r'<a[^>]+data-qa=["\']serp-item__title["\'][^>]*>(?P<title>.*?)</a>',
        r'<a[^>]+href=["\'][^"\']*/vacancy/\d+[^"\']*["\'][^>]*>(?P<title>.*?)</a>',
    )
    for pattern in patterns:
        match = re.search(pattern, block, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = _clean_scraped_text(match.group("title"))
            if title and not _looks_like_navigation(title):
                return title
    return ""


def _hh_direct_vacancy_link(vacancy_id: str, href: str) -> str:
    cleaned_href = str(href or "").replace("&amp;", "&")
    if "hh.ru/vacancy/" in cleaned_href:
        return cleaned_href
    if cleaned_href.startswith("/vacancy/"):
        return urljoin("https://hh.ru", cleaned_href)
    if vacancy_id:
        return f"https://hh.ru/vacancy/{vacancy_id}"
    return cleaned_href


def _extract_data_qa_text(html: str, data_qa: str) -> str:
    block = _extract_balanced_block_by_data_qa(html, data_qa)
    return _clean_scraped_text(block)


def _extract_balanced_block_by_data_qa(html: str, data_qa: str) -> str:
    pattern = re.compile(
        rf'<(?P<tag>[a-z0-9]+)\b(?=[^>]*data-qa=["\'][^"\']*{re.escape(data_qa)}[^"\']*["\'])[^>]*>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html)
    if not match:
        return ""
    tag = match.group("tag").lower()
    end = _balanced_tag_end(html, match.start(), tag)
    return html[match.start() : end] if end else html[match.start() : match.start() + 4000]


def _extract_balanced_block_by_class(html: str, class_name: str) -> str:
    pattern = re.compile(
        rf'<(?P<tag>[a-z0-9]+)\b(?=[^>]*class=["\'][^"\']*{re.escape(class_name)}[^"\']*["\'])[^>]*>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html)
    if not match:
        return ""
    tag = match.group("tag").lower()
    end = _balanced_tag_end(html, match.start(), tag)
    return html[match.start() : end] if end else html[match.start() : match.start() + 50000]


def _augment_hh_detail_with_visible_html(vacancy: dict[str, Any], html: str) -> dict[str, Any]:
    visible_text = _extract_hh_visible_detail_text(html)
    if not visible_text:
        return vacancy

    current_description = _clean_scraped_text(vacancy.get("description"))
    description = visible_text if len(visible_text) > len(current_description) else current_description
    merged_description = join_nonempty((current_description, visible_text), sep="\n\n")
    sections = split_ru_vacancy_sections(merged_description)
    if not sections.get("requirements") or not sections.get("responsibilities") or not sections.get("conditions"):
        fallback_sections = split_ru_vacancy_sections(_clean_multiline_text(html))
        sections = {
            "intro": sections.get("intro") or fallback_sections.get("intro", ""),
            "requirements": sections.get("requirements") or fallback_sections.get("requirements", ""),
            "responsibilities": sections.get("responsibilities") or fallback_sections.get("responsibilities", ""),
            "conditions": sections.get("conditions") or fallback_sections.get("conditions", ""),
        }
    key_skills = _clean_scraped_text(vacancy.get("key_skills"))
    raw_detail_text = _compose_detail_text(
        (
            vacancy.get("title"),
            vacancy.get("salary_text"),
            f"Выплаты: {vacancy.get('payment_frequency')}" if vacancy.get("payment_frequency") else "",
            f"Опыт работы: {vacancy.get('experience')}" if vacancy.get("experience") else "",
            vacancy.get("employment_type"),
            f"График: {vacancy.get('schedule')}" if vacancy.get("schedule") else "",
            f"Рабочие часы: {vacancy.get('working_hours')}" if vacancy.get("working_hours") else "",
            f"Формат работы: {vacancy.get('work_format')}" if vacancy.get("work_format") else "",
            f"Сейчас эту вакансию смотрят {vacancy.get('views_count')}" if vacancy.get("views_count") else "",
            vacancy.get("company"),
            description,
            f"Ключевые навыки {key_skills}" if key_skills else "",
            f"Где предстоит работать {vacancy.get('address')}" if vacancy.get("address") else "",
            f"Вакансия опубликована {vacancy.get('published_at_text')}" if vacancy.get("published_at_text") else "",
        )
    )
    stack = _merge_stack_texts(key_skills, _extract_stack_hint(merged_description, ""), _extract_stack_hint(raw_detail_text, ""))

    augmented = dict(vacancy)
    augmented.update(
        {
            "description": description,
            "requirements": sections.get("requirements") or vacancy.get("requirements", ""),
            "responsibilities": sections.get("responsibilities") or vacancy.get("responsibilities", ""),
            "conditions": sections.get("conditions") or vacancy.get("conditions", ""),
            "stack": stack,
            "key_skills": key_skills,
            "raw_detail_text": raw_detail_text,
        }
    )
    return clean_vacancy_fields(augmented)


def _extract_hh_visible_detail_text(html: str) -> str:
    candidates: list[str] = []
    branded_block = _extract_balanced_block_by_class(html, "tmpl_hh_wrapper")
    if branded_block:
        candidates.append(_clean_multiline_text(branded_block))
    description_block = "" if branded_block else _extract_balanced_block_by_data_qa(html, "vacancy-description")
    if description_block:
        candidates.append(_clean_multiline_text(description_block))
    return "\n\n".join(part for part in candidates if part).strip()


def _merge_stack_texts(*values: Any) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in re.split(r"\s*;\s*", _clean_scraped_text(value)):
            text = item.strip()
            if not text:
                continue
            key = text.lower().replace("ё", "е")
            if key in seen:
                continue
            seen.add(key)
            result.append(text)
    return "; ".join(result)


def _extract_meta_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = _clean_scraped_text(match.group(1)) if match else ""
    title = re.sub(r"\s+(?:вакансия|работа)\b.*$", "", title, flags=re.IGNORECASE).strip()
    return title


def _extract_canonical_url(html: str) -> str:
    match = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    if not match:
        match = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    return clean_text(match.group(1)) if match else ""


def _relevant_detail_fragment(html: str, anchors: tuple[Any, ...]) -> str:
    cleaned_anchors = [clean_text(anchor) for anchor in anchors if clean_text(anchor)]
    positions = [html.find(anchor) for anchor in cleaned_anchors if html.find(anchor) >= 0]
    if not positions:
        return html[:50000]
    start = max(0, min(positions) - 12000)
    return html[start : start + 60000]


def _looks_like_ddos_or_error_page(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    return "ddos-guard" in text or "checking your browser" in text or "произошла ошибка" in text


def _extract_hh_lux_state(html: str) -> dict[str, Any] | None:
    match = re.search(
        r'<template[^>]+id=["\']HH-Lux-InitialState["\'][^>]*>(?P<json>.*?)</template>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    raw = match.group("json")
    for candidate in (raw, unescape(raw)):
        try:
            state = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(state, dict):
            return state
    return None


def _parse_hh_detail_state(state: dict[str, Any], *, page_url: str) -> dict[str, Any] | None:
    vacancy_view = state.get("vacancyView") if isinstance(state.get("vacancyView"), dict) else {}
    if not vacancy_view:
        return None
    title = clean_text(vacancy_view.get("name"))
    description = _clean_scraped_text(vacancy_view.get("description"))
    if not title and not description:
        return None

    vacancy_id = clean_text(vacancy_view.get("vacancyId")) or _extract_hh_id_from_link(page_url)
    area = vacancy_view.get("area") if isinstance(vacancy_view.get("area"), dict) else {}
    city = clean_text(area.get("name"))
    publication_place = clean_text(area.get("areaCatalogTitle") or (f"в {city}" if city else ""))
    company_info = vacancy_view.get("company") if isinstance(vacancy_view.get("company"), dict) else {}
    company = clean_text(company_info.get("visibleName") or company_info.get("name"))
    salary_text = _format_hh_state_salary(vacancy_view.get("compensation"), state)
    payment_frequency = _hh_compensation_label(state, "frequency", (vacancy_view.get("compensation") or {}).get("frequency") if isinstance(vacancy_view.get("compensation"), dict) else "")
    experience = _hh_state_experience(vacancy_view.get("workExperience"))
    employment_code = vacancy_view.get("employmentForm")
    if not employment_code and isinstance(vacancy_view.get("employment"), dict):
        employment_code = vacancy_view.get("employment", {}).get("@type")
    employment_type = _hh_state_employment(employment_code, state)
    schedule = _hh_state_labels(state, "workScheduleByDays", vacancy_view.get("workScheduleByDays"))
    working_hours = _hh_state_labels(state, "workingHours", vacancy_view.get("workingHours"))
    work_format = _hh_state_labels(state, "workFormats", vacancy_view.get("workFormats"))
    address = _hh_state_address(vacancy_view.get("address"))
    metro_stations = _hh_state_metro(vacancy_view.get("address"))
    published_at = clean_text(vacancy_view.get("publicationDate"))
    published_at_text = _format_hh_publication_text(published_at, publication_place)
    views_count = _hh_state_views_count(state, vacancy_id)
    key_skills = _hh_state_key_skills(vacancy_view)
    sections = split_ru_vacancy_sections(description)
    raw_detail_text = _compose_detail_text(
        (
            title,
            salary_text,
            f"Выплаты: {payment_frequency}" if payment_frequency else "",
            f"Опыт работы: {experience}" if experience else "",
            employment_type,
            f"График: {schedule}" if schedule else "",
            f"Рабочие часы: {working_hours}" if working_hours else "",
            f"Формат работы: {work_format}" if work_format else "",
            f"Сейчас эту вакансию смотрят {views_count}" if views_count else "",
            company,
            description,
            f"Ключевые навыки {join_values(key_skills)}" if key_skills else "",
            f"Где предстоит работать {address}" if address else "",
            f"Вакансия опубликована {published_at_text}" if published_at_text else "",
        )
    )
    vacancy = clean_vacancy_fields(
        {
            "vacancy_id": f"hh-html:{vacancy_id}" if vacancy_id else f"hh-html:{abs(hash((title, page_url))) % 10_000_000}",
            "source": "hh-html-detail",
            "title": title,
            "company": company,
            "role": title,
            "level": normalize_level(join_nonempty((experience, title, description[:500]))),
            "format": normalize_format(join_nonempty((work_format, description[:800]))),
            "city": city,
            "relocation_possible": "",
            "published_at": published_at,
            "deadline": clean_text(vacancy_view.get("validThroughTime")),
            "salary_rub": normalize_salary(salary_text),
            "salary_text": salary_text,
            "payment_frequency": payment_frequency,
            "stack": join_nonempty((join_values(key_skills), _extract_stack_hint(join_nonempty((description, raw_detail_text)), title))),
            "key_skills": join_values(key_skills),
            "english_level": "",
            "link": page_url or f"https://hh.ru/vacancy/{vacancy_id}",
            "description": description,
            "requirements": sections.get("requirements", ""),
            "responsibilities": sections.get("responsibilities", ""),
            "conditions": sections.get("conditions", ""),
            "employment_type": employment_type,
            "employment_form": "",
            "experience": experience,
            "schedule": schedule,
            "working_hours": working_hours,
            "work_format": work_format,
            "address": address,
            "metro_stations": metro_stations,
            "employer_name": company,
            "agency_company": "",
            "company_description": clean_text(company_info.get("description")),
            "category": "",
            "published_at_text": published_at_text,
            "views_count": views_count,
            "detail_source": "hh-lux-state",
            "raw_detail_text": raw_detail_text,
        }
    )
    return vacancy if not is_noisy_vacancy(vacancy) else None


def _extract_hh_salary_text_from_html(html: str) -> str:
    for data_qa in ("vacancy-salary-compensation-type-net", "vacancy-salary-compensation-type-gross", "vacancy-salary"):
        text = _extract_data_qa_text(html, data_qa)
        if not text:
            continue
        if "уровень дохода не указан" in text.lower():
            return "Уровень дохода не указан"
        salary = _extract_salary_text(text)
        return salary or text
    title_pos = html.find('data-qa="vacancy-title"')
    if title_pos < 0:
        title_pos = html.find("data-qa='vacancy-title'")
    if title_pos >= 0:
        nearby = _clean_scraped_text(html[title_pos : title_pos + 5000])
        if "уровень дохода не указан" in nearby.lower():
            return "Уровень дохода не указан"
        before_experience = re.split(r"Опыт работы|Полная занятость|Частичная занятость", nearby, maxsplit=1)[0]
        return _extract_salary_text(before_experience)
    return ""


def _format_hh_state_salary(compensation: Any, state: dict[str, Any]) -> str:
    if not isinstance(compensation, dict):
        return ""
    if compensation.get("noCompensation") is not None:
        return "Уровень дохода не указан"
    salary_from = compensation.get("from") or compensation.get("perModeFrom")
    salary_to = compensation.get("to") or compensation.get("perModeTo")
    if not salary_from and not salary_to:
        return ""
    currency = _hh_currency_symbol(state, compensation.get("currencyCode"))
    mode = _hh_compensation_label(state, "mode", compensation.get("mode")).lower()
    if mode:
        mode = mode.replace("за ", "за ", 1)
    tax_text = ""
    if compensation.get("gross") is True:
        tax_text = "до вычета налогов"
    elif compensation.get("gross") is False:
        tax_text = "на руки"
    if salary_from and salary_to and salary_from != salary_to:
        salary = f"{_format_money(salary_from)} — {_format_money(salary_to)} {currency}"
    elif salary_from:
        salary = f"от {_format_money(salary_from)} {currency}"
    else:
        salary = f"до {_format_money(salary_to)} {currency}"
    if mode:
        salary = f"{salary} {mode}"
    if tax_text:
        salary = f"{salary}, {tax_text}"
    return salary


def _hh_currency_symbol(state: dict[str, Any], code: Any) -> str:
    code_text = clean_text(code) or "RUR"
    currencies = state.get("currencies") if isinstance(state.get("currencies"), dict) else {}
    for item in currencies.get("list") or []:
        if isinstance(item, dict) and clean_text(item.get("code")) == code_text:
            return clean_text(item.get("name")) or "₽"
    return {"RUR": "₽", "USD": "$", "EUR": "€"}.get(code_text, code_text)


def _hh_compensation_label(state: dict[str, Any], group: str, code: Any) -> str:
    return _hh_dictionary_label(state.get("vacancyCompensationFieldsDictionary"), group, code)


def _hh_state_labels(state: dict[str, Any], group: str, value: Any) -> str:
    labels = [_hh_dictionary_label(state.get("vacancyEmploymentFieldsDictionary"), group, code) or clean_text(code) for code in _hh_flatten_codes(value, group)]
    return join_nonempty(labels, sep="; ")


def _hh_dictionary_label(dictionary: Any, group: str, code: Any) -> str:
    code_text = clean_text(code)
    if not code_text or not isinstance(dictionary, dict):
        return ""
    for item in dictionary.get(group) or []:
        if isinstance(item, dict) and clean_text(item.get("id")) == code_text:
            return clean_text(item.get("text"))
    return ""


def _hh_flatten_codes(value: Any, element_key: str = "") -> list[str]:
    result: list[str] = []

    def add(item: Any) -> None:
        if isinstance(item, str):
            if item:
                result.append(item)
            return
        if isinstance(item, (int, float)):
            result.append(str(item))
            return
        if isinstance(item, list):
            for nested in item:
                add(nested)
            return
        if isinstance(item, dict):
            if element_key and item.get(f"{element_key}Element") is not None:
                add(item.get(f"{element_key}Element"))
                return
            for nested in item.values():
                add(nested)

    add(value)
    unique: list[str] = []
    for code in result:
        if code not in unique:
            unique.append(code)
    return unique


def _hh_state_experience(code: Any) -> str:
    return {
        "noExperience": "Без опыта",
        "between1And3": "1–3 года",
        "between3And6": "3–6 лет",
        "moreThan6": "более 6 лет",
    }.get(clean_text(code), clean_text(code))


def _hh_state_employment(code: Any, state: dict[str, Any]) -> str:
    code_text = clean_text(code)
    label = _hh_dictionary_label(state.get("vacancyEmploymentFieldsDictionary"), "employmentForm", code_text)
    if label:
        if label.lower() in {"полная", "частичная"}:
            return f"{label} занятость"
        return label
    return {
        "FULL": "Полная занятость",
        "PART": "Частичная занятость",
        "PROJECT": "Проектная работа",
        "FLY_IN_FLY_OUT": "Вахта",
        "SIDE_JOB": "Подработка",
    }.get(code_text, code_text)


def _hh_state_address(address: Any) -> str:
    if not isinstance(address, dict):
        return ""
    display = clean_text(address.get("displayName"))
    if display:
        return display
    return join_nonempty((address.get("city"), address.get("street"), address.get("building")), sep=", ")


def _hh_state_metro(address: Any) -> str:
    if not isinstance(address, dict):
        return ""
    metro_data = ((address.get("metroStations") or {}).get("metro") if isinstance(address.get("metroStations"), dict) else None)
    if isinstance(metro_data, dict):
        metro_data = [metro_data]
    if not isinstance(metro_data, list):
        return ""
    return join_nonempty([item.get("name") for item in metro_data if isinstance(item, dict)], sep="; ")


def _format_hh_publication_text(value: Any, city: str = "") -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    months = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }
    date_text = f"{parsed.day} {months.get(parsed.month, parsed.month)} {parsed.year}"
    place = clean_text(city)
    if place and not place.lower().startswith(("в ", "во ")):
        place = f"в {place}"
    return join_nonempty((date_text, place))


def _hh_state_views_count(state: dict[str, Any], vacancy_id: str) -> str:
    counters = state.get("vacancyOnlineUsersCounters") if isinstance(state.get("vacancyOnlineUsersCounters"), dict) else {}
    counter = counters.get(str(vacancy_id)) if vacancy_id else None
    if not isinstance(counter, dict):
        return ""
    value = counter.get("excludingCurrent") if counter.get("excludingCurrent") is not None else counter.get("includingCurrent")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return clean_text(value)
    return f"{number} {_hh_people_word(number)}"


def _hh_people_word(number: int) -> str:
    if 11 <= number % 100 <= 14:
        return "человек"
    if number % 10 == 1:
        return "человек"
    if 2 <= number % 10 <= 4:
        return "человека"
    return "человек"


def _hh_state_key_skills(vacancy_view: dict[str, Any]) -> list[str]:
    skills = vacancy_view.get("keySkills") or vacancy_view.get("confirmableKeySkills")
    result: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            skill = clean_text(value)
            if skill and skill not in result:
                result.append(skill)
        elif isinstance(value, dict):
            for key in ("name", "text", "title"):
                if value.get(key):
                    add(value.get(key))
                    return
            for nested in value.values():
                add(nested)
        elif isinstance(value, list):
            for nested in value:
                add(nested)

    add(skills)
    return result


def _extract_hh_key_skills(html: str) -> list[str]:
    skills: list[str] = []
    for match in re.finditer(
        r'<(?:span|div)[^>]+data-qa=["\'][^"\']*(?:bloko-tag__text|skills-element)[^"\']*["\'][^>]*>(.*?)</(?:span|div)>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        skill = _clean_scraped_text(match.group(1))
        if skill and skill not in skills:
            skills.append(skill)
    if skills:
        return skills
    text = _clean_scraped_text(_relevant_detail_fragment(html, ("Ключевые навыки",)))
    if "Ключевые навыки" not in text:
        return []
    tail = text.split("Ключевые навыки", 1)[1]
    tail = re.split(r"(?:Где предстоит работать|Вакансия опубликована|Задайте вопрос)", tail, maxsplit=1)[0]
    return [item.strip() for item in re.split(r"\s{2,}|;|\n", tail) if 2 <= len(item.strip()) <= 80][:30]


def _extract_hh_payment_frequency(value: str) -> str:
    text = clean_text(value)
    match = re.search(
        r"Выплаты:\s*(раз в\s+(?:день|неделю|месяц)|два раза в\s+месяц|ежедневно|еженедельно|ежемесячно|ежеквартально)",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _extract_hh_experience(value: str) -> str:
    text = _clean_scraped_text(value)
    match = re.search(r"Опыт(?: работы)?:\s*([^.;\n]+?)(?=\s+(?:Полная|Частичная|График|Оформление|Рабочие|Формат)|$)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"\b(Без опыта|Опыт\s+\d+\s*[-–]\s*\d+\s+года|Опыт\s+более\s+\d+\s+лет)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_hh_employment(value: str) -> str:
    text = _clean_scraped_text(value)
    match = re.search(r"\b(Полная занятость|Частичная занятость|Проектная работа|Стажировка|Волонтерство)\b", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_hh_views_count(value: str) -> str:
    text = _clean_scraped_text(value)
    match = re.search(r"Сейчас\s+(?:эту вакансию\s+)?смотрят?\s+([^.;\n]+)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_labeled_value(value: str, label: str) -> str:
    text = _clean_scraped_text(value)
    pattern = rf"{re.escape(label)}:\s*([^.;\n]+?)(?=\s+(?:Опыт|Полная|Частичная|Оформление|График|Рабочие часы|Формат работы|Сейчас|Обязанности|Требования|Условия|Ключевые навыки|Где предстоит работать|Вакансия опубликована)|$)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_hh_address(value: str) -> str:
    text = _clean_scraped_text(value)
    match = re.search(r"Где предстоит работать\s+(.+?)(?:\s+Вакансия опубликована|\s+Задайте вопрос|$)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_hh_metro(value: str) -> str:
    address = _extract_hh_address(value)
    parts = [part.strip() for part in address.split(",")]
    return "; ".join(part for part in parts[:4] if part and not re.search(r"\d", part))


def _extract_hh_published_text(value: str) -> str:
    text = _clean_scraped_text(value)
    match = re.search(r"Вакансия опубликована\s+(.+?)(?:$|Задайте вопрос)", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_json_assignment(html: str, assignment: str) -> Any:
    marker = re.search(rf"{re.escape(assignment)}\s*=\s*", html)
    if not marker:
        return None
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(html[marker.end() :])
        return value
    except json.JSONDecodeError:
        match = re.search(rf"{re.escape(assignment)}\s*=\s*(.*?)</script>", html, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(1).rstrip(";"))
        except json.JSONDecodeError:
            return None


def _clean_multiline_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<(script|style|noscript|svg|canvas)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|li|ul|ol|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in re.split(r"[\r\n]+", text)]
    return "\n".join(line for line in lines if line)


def _format_superjob_published_text(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%H:%M")


def _parse_superjob_detail_state(state: dict[str, Any], *, page_url: str, vacancy_id: str | None) -> dict[str, Any] | None:
    entities = state.get("entities") if isinstance(state.get("entities"), dict) else {}
    for current_id in _superjob_detail_ids(state, vacancy_id):
        vacancy = _sj_entity(entities, "vacancy", current_id)
        if not vacancy:
            continue
        main = _sj_rel_entity(entities, vacancy, "mainInfo")
        detail = _sj_rel_entity(entities, vacancy, "detailInfo")
        company_info = _sj_rel_entity(entities, vacancy, "companyInfo")
        company = _sj_rel_entity(entities, vacancy, "company")
        town = _sj_rel_entity(entities, vacancy, "town")
        experience = _sj_rel_entity(entities, vacancy, "requiredExperience")
        profession = _sj_rel_entity(entities, vacancy, "profession")
        work_type = _sj_rel_entity(entities, detail, "workType")
        catalogues = _sj_rel_entities(entities, vacancy, "catalogues")
        tags = _sj_rel_entities(entities, vacancy, "vacancyTags")
        employment_tags = _sj_rel_entities(entities, vacancy, "employmentTypeTags")
        schedule_tags = _sj_rel_entities(entities, vacancy, "workScheduleTags")
        format_tags = _sj_rel_entities(entities, vacancy, "workFormatTags")

        main_attrs = _sj_attrs(main)
        detail_attrs = _sj_attrs(detail)
        company_info_attrs = _sj_attrs(company_info)
        company_attrs = _sj_attrs(company)
        title = clean_text(main_attrs.get("profession") or _sj_attrs(profession).get("label"))
        if not title:
            continue
        full_text = _clean_multiline_text(detail_attrs.get("fullTextPlain") or detail_attrs.get("requirements") or detail_attrs.get("fullText"))
        sections = split_ru_vacancy_sections(full_text)
        company_name = clean_text(company_info_attrs.get("name") or company_attrs.get("title"))
        agency_company = clean_text(company_attrs.get("title")) if clean_text(company_attrs.get("title")) != company_name else ""
        company_description = clean_text(company_info_attrs.get("description") or company_attrs.get("description"))
        category = join_nonempty([_sj_attrs(item).get("label") for item in catalogues], sep="; ")
        experience_text = clean_text(_sj_attrs(experience).get("defaultLabel") or _sj_attrs(experience).get("shortLabel"))
        tag_labels = [clean_text(_sj_attrs(item).get("label")) for item in tags]
        employment_type = join_nonempty([_sj_attrs(item).get("label") for item in employment_tags], sep="; ")
        if not employment_type:
            employment_type = join_nonempty([label for label in tag_labels if "занятость" in label.lower()], sep="; ")
        schedule = join_nonempty(
            [_sj_attrs(work_type).get("defaultLabel"), *[_sj_attrs(item).get("label") for item in schedule_tags]],
            sep="; ",
        )
        work_format = join_nonempty([_sj_attrs(item).get("label") for item in format_tags], sep="; ")
        if not work_format and detail_attrs.get("isRemoteWork"):
            work_format = "Удаленная работа"
        salary_text = _format_superjob_salary(entities, main)
        city = clean_text(_sj_attrs(town).get("name"))
        link = page_url or _superjob_link_from_state(state, current_id)
        published_value = clean_text(main_attrs.get("updatedAt") or main_attrs.get("publishedAt"))
        published_text = _format_superjob_published_text(published_value)
        responsibilities = sections.get("responsibilities") or clean_text(detail_attrs.get("duties"))
        requirements = sections.get("requirements") or clean_text(detail_attrs.get("requirements"))
        conditions = sections.get("conditions") or clean_text(detail_attrs.get("conditions"))
        employee_count = _sj_company_employee_count(entities, company)
        raw_detail_text = _compose_detail_text(
            (
                published_text,
                category,
                title,
                salary_text,
                city,
                employment_type,
                schedule,
                experience_text,
                company_name,
                agency_company,
                _superjob_client_since(company_attrs),
                employee_count,
                company_description,
                "Хочу тут работать",
                full_text,
            )
        )
        vacancy_dict = clean_vacancy_fields(
            {
                "vacancy_id": f"superjob-html:{current_id}",
                "source": "superjob-html-detail",
                "title": title,
                "company": company_name,
                "role": title,
                "level": normalize_level(join_nonempty((experience_text, title, full_text[:800]))),
                "format": normalize_format(join_nonempty((work_format, schedule, full_text[:1200]))),
                "city": city,
                "relocation_possible": "",
                "published_at": published_value,
                "deadline": clean_text(main_attrs.get("publishedTill")),
                "salary_rub": normalize_salary(salary_text),
                "salary_text": salary_text,
                "payment_frequency": "",
                "stack": _extract_stack_hint(full_text, title),
                "key_skills": _extract_stack_hint(full_text, title),
                "english_level": "",
                "link": link,
                "description": full_text,
                "requirements": requirements,
                "responsibilities": responsibilities,
                "conditions": conditions,
                "employment_type": employment_type or join_nonempty(tag_labels, sep="; "),
                "experience": experience_text,
                "schedule": schedule,
                "working_hours": "",
                "work_format": work_format,
                "address": city,
                "metro_stations": "",
                "employer_name": company_name,
                "agency_company": agency_company,
                "company_description": company_description,
                "category": category,
                "published_at_text": published_text,
                "views_count": "",
                "detail_source": "superjob-app-state",
                "raw_detail_text": raw_detail_text,
            }
        )
        return vacancy_dict if not is_noisy_vacancy(vacancy_dict) else None
    return None


def _superjob_detail_ids(state: dict[str, Any], vacancy_id: str | None) -> list[str]:
    ids: list[str] = []
    if vacancy_id:
        ids.append(str(vacancy_id))
    route_id = (
        ((state.get("currentRoute") or {}).get("routeParams") or {}).get("vacancyId")
        or ((state.get("pageInfo") or {}).get("routeParams") or {}).get("vacancyId")
    )
    if route_id:
        ids.append(str(route_id))
    for key in ((state.get("entities") or {}).get("vacancy") or {}).keys():
        ids.append(str(key))
    result: list[str] = []
    for item in ids:
        if item and item not in result:
            result.append(item)
    return result


def _sj_entity(entities: dict[str, Any], type_name: str, entity_id: Any) -> dict[str, Any]:
    return entities.get(type_name, {}).get(str(entity_id), {}) if isinstance(entities.get(type_name), dict) else {}


def _sj_attrs(entity: dict[str, Any]) -> dict[str, Any]:
    attrs = entity.get("attributes") if isinstance(entity, dict) else {}
    return attrs if isinstance(attrs, dict) else {}


def _sj_rel_entity(entities: dict[str, Any], entity: dict[str, Any], relation: str) -> dict[str, Any]:
    data = ((entity.get("relationships") or {}).get(relation) or {}).get("data") if isinstance(entity, dict) else None
    if isinstance(data, dict):
        return _sj_entity(entities, str(data.get("type") or ""), data.get("id"))
    return {}


def _sj_rel_entities(entities: dict[str, Any], entity: dict[str, Any], relation: str) -> list[dict[str, Any]]:
    data = ((entity.get("relationships") or {}).get(relation) or {}).get("data") if isinstance(entity, dict) else None
    if not isinstance(data, list):
        return []
    return [_sj_entity(entities, str(item.get("type") or ""), item.get("id")) for item in data if isinstance(item, dict)]


def _format_superjob_salary(entities: dict[str, Any], main: dict[str, Any]) -> str:
    main_attrs = _sj_attrs(main)
    salary = _sj_rel_entity(entities, main, "salary")
    salary_attrs = _sj_attrs(salary)
    minimum = salary_attrs.get("minSalary", main_attrs.get("minSalary"))
    maximum = salary_attrs.get("maxSalary", main_attrs.get("maxSalary"))
    if salary_attrs.get("paymentAgreement") or (not minimum and not maximum):
        return "По договоренности"
    currency = _sj_rel_entity(entities, salary, "currency")
    period = _sj_rel_entity(entities, salary, "salaryPeriod")
    symbol = clean_text(_sj_attrs(currency).get("symbol")) or "₽"
    period_text = clean_text(_sj_attrs(period).get("defaultLabel")).lower()
    suffix = f"{symbol}/{period_text}" if period_text else symbol
    if minimum and maximum and minimum != maximum:
        return f"{_format_money(minimum)} — {_format_money(maximum)} {suffix}"
    if minimum:
        return f"от {_format_money(minimum)} {suffix}"
    if maximum:
        return f"до {_format_money(maximum)} {suffix}"
    return ""


def _format_money(value: Any) -> str:
    try:
        number = int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return clean_text(value)
    return f"{number:,}".replace(",", " ")


def _superjob_link_from_state(state: dict[str, Any], vacancy_id: str) -> str:
    route = state.get("currentRoute") or {}
    pathname = clean_text(route.get("pathname"))
    if pathname:
        return urljoin("https://www.superjob.ru", pathname)
    return f"https://www.superjob.ru/vakansii/vacancy-{vacancy_id}.html"


def _sj_company_employee_count(entities: dict[str, Any], company: dict[str, Any]) -> str:
    employee_count = _sj_rel_entity(entities, company, "countOfEmployee")
    label = clean_text(_sj_attrs(employee_count).get("defaultLabel"))
    return re.sub(r"\b\d{4,}\b", lambda match: _format_money(match.group(0)), label)


def _superjob_client_since(company_attrs: dict[str, Any]) -> str:
    created = clean_text(company_attrs.get("createdAt"))
    match = re.match(r"(\d{4})", created)
    return f"Клиент SuperJob с {match.group(1)} года" if match else ""


def _compose_detail_text(values: Any) -> str:
    return join_nonempty(values, sep="\n")


def _parse_superjob_detail_visible_html(html: str, *, page_url: str, vacancy_id: str | None) -> dict[str, Any] | None:
    title = _extract_meta_title(html)
    if not title:
        title = _clean_scraped_text(_extract_balanced_block_by_data_qa(html, "vacancy-title"))
    if not title:
        return None
    text = _clean_scraped_text(_relevant_detail_fragment(html, (title, "Требования", "Задачи", "Что мы предлагаем")))
    if _looks_like_ddos_or_error_page(title, text):
        return None
    sections = split_ru_vacancy_sections(text)
    extracted_id = vacancy_id or _extract_superjob_id_from_link(page_url) or _extract_superjob_id_from_link(html)
    salary = _extract_salary_text(text) or ("По договоренности" if "По договоренности" in text else "")
    vacancy = clean_vacancy_fields(
        {
            "vacancy_id": f"superjob-html:{extracted_id}" if extracted_id else f"superjob-html:{abs(hash((title, page_url))) % 10_000_000}",
            "source": "superjob-html-detail",
            "title": title,
            "company": "",
            "role": title,
            "level": normalize_level(text[:1200]),
            "format": normalize_format(text[:1200]),
            "city": _extract_city(text),
            "relocation_possible": "",
            "published_at": "",
            "deadline": "",
            "salary_rub": normalize_salary(salary),
            "salary_text": salary,
            "stack": _extract_stack_hint(text, title),
            "key_skills": _extract_stack_hint(text, title),
            "english_level": "",
            "link": page_url,
            "description": text,
            "requirements": sections.get("requirements", ""),
            "responsibilities": sections.get("responsibilities", ""),
            "conditions": sections.get("conditions", ""),
            "detail_source": "superjob-visible-html",
            "raw_detail_text": text,
        }
    )
    return vacancy if not is_noisy_vacancy(vacancy) else None


def _extract_superjob_id_from_link(value: str) -> str:
    match = re.search(r"(?P<id>\d+)\.html", str(value or ""), flags=re.IGNORECASE)
    return match.group("id") if match else ""


def parse_html_with_llm(
    *,
    source: str,
    html: str,
    query: str,
    page_url: str,
    max_items: int,
    llm_client: Any | None,
) -> list[dict[str, Any]]:
    if not getattr(llm_client, "enabled", False) or not html.strip():
        return []
    max_items = max(1, min(int(max_items or 1), 50))
    candidate_cards = _vacancy_candidate_cards(html, source=source, page_url=page_url, limit=max_items)
    result = _request_llm_search_parse(
        llm_client,
        source=source,
        query=query,
        page_url=page_url,
        max_items=max_items,
        candidate_cards=candidate_cards,
        retry=False,
    )
    raw_items = result.get("vacancies") if isinstance(result, dict) else None
    if _needs_llm_parse_retry(raw_items, candidate_cards):
        result = _request_llm_search_parse(
            llm_client,
            source=source,
            query=query,
            page_url=page_url,
            max_items=max_items,
            candidate_cards=candidate_cards,
            retry=True,
        )
        raw_items = result.get("vacancies") if isinstance(result, dict) else None
    if not isinstance(raw_items, list):
        return []
    vacancies: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items[:max_items], start=1):
        if not isinstance(item, dict):
            continue
        title = _clean_scraped_text(item.get("title"))
        link = clean_text(item.get("link"))
        requirements = _clean_scraped_text(item.get("requirements"))
        responsibilities = _clean_scraped_text(item.get("responsibilities"))
        conditions = _clean_scraped_text(item.get("conditions"))
        skills = item.get("skills")
        skills_text = "; ".join(_clean_scraped_text(skill) for skill in skills if _clean_scraped_text(skill)) if isinstance(skills, list) else ""
        description = _clean_scraped_text(" ".join(
            str(part or "")
            for part in (item.get("description"), requirements, responsibilities, conditions, skills_text)
        ))
        if not title:
            continue
        vacancy = clean_vacancy_fields(
            {
                "vacancy_id": f"{source}-llm:{index}:{abs(hash((title, link))) % 10_000_000}",
                "source": f"{source}-llm-html",
                "title": title,
                "company": _clean_scraped_text(item.get("company")),
                "role": title,
                "level": normalize_level(_clean_scraped_text(item.get("experience_level")) or title + " " + description),
                "format": normalize_format(_clean_scraped_text(item.get("work_format")) or description),
                "city": _clean_scraped_text(item.get("city")),
                "relocation_possible": "",
                "published_at": "",
                "deadline": "",
                "salary_rub": _clean_scraped_text(item.get("salary_rub")),
                "stack": skills_text or description,
                "key_skills": skills_text,
                "english_level": "",
                "link": urljoin(page_url, link),
                "description": description,
                "requirements": requirements,
                "responsibilities": responsibilities,
                "conditions": conditions,
                "employment_type": _clean_scraped_text(item.get("employment_type")),
            }
        )
        if not is_noisy_vacancy(vacancy):
            vacancies.append(vacancy)
    return vacancies


def merge_auto_and_llm_vacancies(
    *,
    source: str,
    auto_vacancies: list[dict[str, Any]],
    llm_vacancies: list[dict[str, Any]],
    max_items: int,
) -> list[dict[str, Any]]:
    max_items = max(1, int(max_items or 1))
    llm_by_key: dict[str, dict[str, Any]] = {}
    used_llm_keys: set[str] = set()
    for vacancy in llm_vacancies:
        key = _vacancy_merge_key(vacancy)
        if key and key not in llm_by_key:
            llm_by_key[key] = vacancy

    matched: list[dict[str, Any]] = []
    auto_only: list[dict[str, Any]] = []
    seen: set[str] = set()
    for auto_vacancy in auto_vacancies:
        key = _vacancy_merge_key(auto_vacancy)
        llm_vacancy = llm_by_key.get(key) if key else None
        if llm_vacancy:
            used_llm_keys.add(key)
            merged = _merge_vacancy_pair(source=source, auto_vacancy=auto_vacancy, llm_vacancy=llm_vacancy)
            out_key = _vacancy_merge_key(merged) or str(id(merged))
            if out_key not in seen:
                seen.add(out_key)
                matched.append(merged)
        else:
            out_key = key or str(id(auto_vacancy))
            if out_key not in seen:
                seen.add(out_key)
                auto_only.append(auto_vacancy)

    llm_only: list[dict[str, Any]] = []
    for llm_vacancy in llm_vacancies:
        key = _vacancy_merge_key(llm_vacancy)
        if key in used_llm_keys:
            continue
        out_key = key or str(id(llm_vacancy))
        if out_key not in seen:
            seen.add(out_key)
            llm_only.append(llm_vacancy)

    return (matched + auto_only + llm_only)[:max_items]


def _request_llm_search_parse(
    llm_client: Any,
    *,
    source: str,
    query: str,
    page_url: str,
    max_items: int,
    candidate_cards: list[dict[str, str]],
    retry: bool,
) -> dict[str, Any]:
    instruction = (
        "You are extracting vacancy cards from one public job-search results page (SERP), not from a single vacancy page. "
        "Use only the supplied candidate_cards as evidence. Each output item must correspond to a visible vacancy card or vacancy link on this page. "
        "Prefer anchors whose href/title look like a job vacancy; ignore navigation, filters, login, resume creation, ads, employer profile links, and apply buttons. "
        "Extract up to max_items vacancies in page order. Preserve the original vacancy URL from the page; relative links are allowed. "
        "For every vacancy, extract as much grounded detail as the page shows: company, city, salary, work format, employment type, level, skills, description, requirements, responsibilities, and conditions. "
        "Make description the richest compact summary of all visible role-specific facts, including tasks, stack, product/domain, schedule, work format, location and notable constraints when present. "
        "Put requirements, responsibilities and conditions into their own fields when the card separates them; otherwise keep the visible facts in description. "
        "Do not invent missing company, city, salary, requirements, responsibilities, conditions, work_format, experience_level, skills, or employment_type; use an empty string when absent. "
        "A valid result is JSON with key vacancies. If the page has no visible vacancy cards, return {\"vacancies\": []}."
    )
    if retry:
        instruction += (
            " Previous output was empty or incomplete. Re-check every candidate card and return all vacancy-like links you can justify. "
            "For each vacancy-like anchor, title and link are mandatory; other fields may be empty strings."
        )
    return llm_client.json_task(
        stage=f"{source}_html_parse",
        system_prompt=(
            "Return only one valid JSON object. No markdown, no explanations. "
            "Never fabricate vacancies; every item must be grounded in the provided page evidence."
        ),
        payload={
            "source": source,
            "query": query,
            "page_url": page_url,
            "max_items": max_items,
            "instruction": instruction,
            "candidate_cards": candidate_cards,
            "anchor_inventory": candidate_cards,
            "expected_json_shape": {
                "vacancies": [
                    {
                        "title": "string, required for each returned vacancy",
                        "company": "string_or_empty",
                        "city": "string_or_empty",
                        "salary_rub": "string_or_empty",
                        "link": "string, required; href from the page",
                        "work_format": "string_or_empty",
                        "experience_level": "string_or_empty",
                        "skills": ["string"],
                        "description": "string_or_empty",
                        "requirements": "string_or_empty",
                        "responsibilities": "string_or_empty",
                        "conditions": "string_or_empty",
                        "employment_type": "string_or_empty",
                    }
                ]
            },
        },
    )


def _merge_vacancy_pair(*, source: str, auto_vacancy: dict[str, Any], llm_vacancy: dict[str, Any]) -> dict[str, Any]:
    merged = dict(auto_vacancy)
    merged["vacancy_id"] = _merged_vacancy_id(source, auto_vacancy, llm_vacancy)
    merged["source"] = f"{source}-mixed-html"
    for field in (
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
        "english_level",
        "link",
        "employment_type",
    ):
        merged[field] = _best_scalar(auto_vacancy.get(field), llm_vacancy.get(field))
    for field, limit in (
        ("description", 4500),
        ("requirements", 2200),
        ("responsibilities", 2200),
        ("conditions", 2200),
        ("stack", 1800),
        ("key_skills", 1800),
    ):
        merged[field] = _merge_text_field(auto_vacancy.get(field), llm_vacancy.get(field), limit=limit)
    if not merged.get("key_skills"):
        merged["key_skills"] = merged.get("stack", "")
    return merged


def _merged_vacancy_id(source: str, auto_vacancy: dict[str, Any], llm_vacancy: dict[str, Any]) -> str:
    key = _vacancy_merge_key(auto_vacancy) or _vacancy_merge_key(llm_vacancy) or str((auto_vacancy.get("title"), llm_vacancy.get("title")))
    return f"{source}-mixed:{abs(hash(key)) % 100_000_000}"


def _vacancy_merge_key(vacancy: dict[str, Any]) -> str:
    link = clean_text(vacancy.get("link"))
    if link:
        return "link:" + _canonical_link_for_merge(link)
    title = _normalize_merge_text(vacancy.get("title") or vacancy.get("role"))
    company = _normalize_merge_text(vacancy.get("company"))
    if title or company:
        return f"text:{title}|{company}"
    return ""


def _canonical_link_for_merge(link: str) -> str:
    value = str(link or "").strip().lower()
    value = re.sub(r"[?#].*$", "", value)
    return value.rstrip("/")


def _normalize_merge_text(value: Any) -> str:
    text = clean_text(value).lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _best_scalar(first: Any, second: Any) -> str:
    values = [clean_text(first), clean_text(second)]
    values = [value for value in values if value and value.lower() not in {"unknown", "none", "null"}]
    if not values:
        return ""
    return max(values, key=lambda value: (len(value.split()), len(value)))


def _merge_text_field(first: Any, second: Any, *, limit: int) -> str:
    parts: list[str] = []
    for value in (first, second):
        text = _clean_scraped_text(value)
        if not text or text.lower() in {"unknown", "none", "null"}:
            continue
        normalized = _normalize_merge_text(text)
        existing_normalized = [_normalize_merge_text(existing) for existing in parts]
        if any(normalized == existing or normalized in existing or existing in normalized for existing in existing_normalized):
            continue
        parts.append(text)
    return " ".join(parts)[:limit].strip()


def _needs_llm_parse_retry(raw_items: Any, anchor_inventory: list[dict[str, str]]) -> bool:
    if not isinstance(raw_items, list):
        return True
    if not anchor_inventory:
        return False
    expected = min(len(anchor_inventory), 3)
    return len(raw_items) < expected


def _nearby_block(html: str, start: int, end: int, radius: int = 2500) -> str:
    return html[max(0, start - radius) : min(len(html), end + radius)]


def _hh_vacancy_block(html: str, start: int, end: int) -> str:
    marker_pattern = re.compile(
        r'<(?:div|article)\b[^>]*(?:data-qa=["\']vacancy-serp__vacancy["\']|class=["\'][^"\']*(?:vacancy-card|serp-item)[^"\']*["\'])',
        flags=re.IGNORECASE,
    )
    markers = list(marker_pattern.finditer(html))
    previous_markers = [marker for marker in markers if marker.start() <= start]
    if not previous_markers:
        return _nearby_block(html, start, end, radius=1200)

    card_start = previous_markers[-1].start()
    next_markers = [marker for marker in markers if marker.start() > start]
    card_end = next_markers[0].start() if next_markers else min(len(html), max(end + 1800, card_start + 2600))
    return html[card_start:card_end]


def _vacancy_card_block(html: str, start: int, end: int, *, fallback_radius: int = 1100) -> str:
    markers = list(_card_marker_pattern().finditer(html))
    previous_markers = [marker for marker in markers if marker.start() <= start]
    if not previous_markers:
        return _nearby_block(html, start, end, radius=fallback_radius)

    marker = previous_markers[-1]
    if start - marker.start() > 6500:
        return _nearby_block(html, start, end, radius=fallback_radius)

    tag = marker.group("tag").lower()
    card_start = marker.start()
    next_markers = [candidate for candidate in markers if candidate.start() > start]
    balanced_end = _balanced_tag_end(html, card_start, tag)
    if balanced_end:
        card_end = balanced_end
    elif next_markers:
        card_end = next_markers[0].start()
    else:
        card_end = min(len(html), max(end + fallback_radius, card_start + 2600))
    if next_markers:
        card_end = min(card_end, next_markers[0].start())
    card_end = min(card_end, card_start + 4200)
    return html[card_start:card_end]


def _card_marker_pattern() -> re.Pattern[str]:
    markers = "vacancy|job|serp-item|card|result|listing|item"
    return re.compile(
        rf'<(?P<tag>article|li|div)\b(?=[^>]*(?:class|id|data-[\w-]+)=["\'][^"\']*(?:{markers})[^"\']*["\'])[^>]*>',
        flags=re.IGNORECASE | re.DOTALL,
    )


def _balanced_tag_end(html: str, start: int, tag: str) -> int:
    pattern = re.compile(rf"</?{re.escape(tag)}\b[^>]*>", flags=re.IGNORECASE)
    depth = 0
    for match in pattern.finditer(html, start):
        token = match.group(0)
        if token.startswith("</"):
            depth -= 1
            if depth <= 0:
                return match.end()
        elif not token.endswith("/>"):
            depth += 1
    return 0


def _clean_scraped_text(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\b[a-z]*onse\?\s*vacancyId=\d+[^\s\"'<]*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:fill-rule|clip-rule|fill-opacity|fill|class|style|data-[\w-]+|aria-[\w-]+|xlink:href|title|type)=[\"'][^\"']*[\"']", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:f-test-[\w-]+|undefined)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(?:[A-Za-z0-9_-]{2,}\s+){2,}[A-Za-z0-9_-]{2,}[\"']?>\s*", " ", text)
    text = re.sub(r"^\s*(?:span|pan|div|svg|path|button|class)[\"']?>\s*", " ", text, flags=re.IGNORECASE)
    starts_with_iso_datetime = bool(re.match(r"\s*\d{4}-\d{2}-\d{2}T\d{2}", text))
    if not starts_with_iso_datetime and not _extract_salary_text(text[:120]):
        text = re.sub(r"^\s*[A-Za-z]?\d+(?:[.\s,-]*[A-Za-z]?\d+){5,}[A-Za-z]*[\"']?\s*>?\s*", " ", text)
    if not starts_with_iso_datetime and not _extract_salary_text(text):
        text = re.sub(r"\b[A-Za-z]?\d+(?:\.\d+)?(?:[.\s,-]+[A-Za-z]?\d+(?:\.\d+)?){7,}[A-Za-z]*\b", " ", text)
    text = re.sub(r"^\s*[\"']?>\s*", " ", text)
    text = _strip_leading_card_controls(text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n'>")
    return text


def _strip_leading_card_controls(value: str) -> str:
    text = re.sub(r"\b(?:Apply|Откликнуться|Чат|Добавить в избранное)\b", " ", value, flags=re.IGNORECASE)
    text = re.sub(r"\bСейчас\s+смотрят\b[^.?!\n\r]{0,80}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bВыплаты:\s*[^.?!\n\r]{0,80}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bОпыт\s+\d+\s*[-–]\s*(?:\d+)?\s*(?:года|лет|год)?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:за\s+месяц,\s*)?на\s+руки\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+(?:[.,]\d+)?\s*•\b", " ", text)
    text = re.sub(r"\+7\s*\d{3}\s*\d{3}[•\d]*", " ", text)
    text = re.sub(r"\b(?:Сегодня|Вчера)\s*(?:в\s*\d{1,2}:\d{2})?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+\s+зарплат[аы]?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+\s+отзыв(?:ов|а)?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bПерейти в каталог компаний\b", " ", text, flags=re.IGNORECASE)
    return text


def _extract_first_anchor_text(block: str, href_parts: tuple[str, ...]) -> str:
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.IGNORECASE | re.DOTALL):
        if any(part in href for part in href_parts):
            cleaned = clean_text(text)
            if cleaned and not _looks_like_navigation(cleaned):
                return cleaned
    return ""


def _extract_attr(fragment: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}=["\']([^"\']+)["\']', fragment, flags=re.IGNORECASE)
    return clean_text(match.group(1)) if match else ""


def _extract_city(block: str) -> str:
    text = clean_text(block)
    for city in ("Москва", "Санкт-Петербург", "Казань", "Екатеринбург", "Новосибирск", "Нижний Новгород", "Удаленно", "Удалённо"):
        if city.lower().replace("ё", "е") in text.lower().replace("ё", "е"):
            return city
    return ""


def _extract_salary_text(block: str) -> str:
    text = clean_text(block)
    patterns = [
        r"(?:от\s*)?\d[\d\s]{3,}(?:\s*[-–—]\s*\d[\d\s]{3,})?\s*(?:₽(?:/\w+)?|руб\.?|RUB)",
        r"\d[\d\s]{3,}\s*(?:₽|руб\.?|RUB)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def _extract_stack_hint(block: str, query: str) -> str:
    text = clean_text(block)
    query_words = [word.lower() for word in re.split(r"\W+", query) if len(word) > 2]
    patterns: list[tuple[str, str]] = [
        (r"\bpython\b", "Python"),
        (r"\bsql\b", "SQL"),
        (r"\bmysql\b", "MySQL"),
        (r"\bpostgres(?:ql)?\b", "PostgreSQL"),
        (r"\bmongodb\b", "MongoDB"),
        (r"\bdynamodb\b", "DynamoDB"),
        (r"\bredis\b", "Redis"),
        (r"\bnosql\b", "NoSQL"),
        (r"\bexcel\b", "Excel"),
        (r"\bpandas\b", "Pandas"),
        (r"\bpower\s*bi\b", "Power BI"),
        (r"\btableau\b", "Tableau"),
        (r"\bjava\b", "Java"),
        (r"\bgo\b", "Go"),
        (r"\breact\b", "React"),
        (r"\bdocker\b", "Docker"),
        (r"\blinux\b", "Linux"),
        (r"\bbash\b", "Bash"),
        (r"\bappsec\b", "AppSec"),
        (r"\bapplication security\b", "Application Security"),
        (r"\bssdlc\b", "SSDLC"),
        (r"\bsast\b", "SAST"),
        (r"\bdast\b", "DAST"),
        (r"\bsca\b", "SCA"),
        (r"\bk8s\b", "k8s"),
        (r"\bkubernetes\b", "Kubernetes"),
        (r"\bci\s*/\s*cd\b", "CI/CD"),
        (r"\bllm\b", "LLM"),
        (r"\bai\b", "AI"),
        (r"\bml\b", "ML"),
        (r"\bnlp\b", "NLP"),
        (r"\blangchain\b", "LangChain"),
        (r"\blanggraph\b", "LangGraph"),
        (r"\bfastapi\b", "FastAPI"),
        (r"\bdjango\b", "Django"),
        (r"\bflask\b", "Flask"),
        (r"\brest(?:\s*/\s*grpc)?\b", "REST API"),
        (r"\bgrpc\b", "gRPC"),
        (r"\bkafka\b", "Kafka"),
        (r"\brabbitmq\b", "RabbitMQ"),
        (r"\bprometheus\b", "Prometheus"),
        (r"\bgrafana\b", "Grafana"),
        (r"\belk\b", "ELK"),
        (r"\bopentelemetry\b", "OpenTelemetry"),
        (r"\bjaeger\b", "Jaeger"),
        (r"\bpytest\b", "pytest"),
        (r"\bunittest\b", "unittest"),
        (r"\blocust\b", "Locust"),
        (r"\bk6\b", "k6"),
        (r"\bjmeter\b", "JMeter"),
        (r"\bhttp\b", "HTTP"),
        (r"\bhttps\b", "HTTPS"),
        (r"\bwebsockets?\b", "WebSockets"),
        (r"\bjson\b", "JSON"),
        (r"\bxml\b", "XML"),
        (r"\bpostman\b", "Postman"),
        (r"\binsomnia\b", "Insomnia"),
        (r"\bcurl\b", "curl"),
        (r"\bcharles\b", "Charles"),
        (r"\bwireshark\b", "Wireshark"),
        (r"\bgit\b", "Git"),
        (r"\bjira\b", "Jira"),
        (r"\btestrail\b", "TestRail"),
        (r"\bzephyr\b", "Zephyr"),
        (r"\ballure\b", "Allure"),
        (r"\bconfluence\b", "Confluence"),
        (r"\bvs\s*code\b", "VS Code"),
        (r"\bpycharm\b", "PyCharm"),
        (r"\bgithub copilot\b", "GitHub Copilot"),
        (r"\bcursor\b", "Cursor"),
        (r"\bcodeium\b", "Codeium"),
        (r"\bmachine learning\b", "Machine Learning"),
        (r"\bpytorch\b", "PyTorch"),
        (r"\btensorflow\b", "TensorFlow"),
        (r"\bvllm\b", "vLLM"),
        (r"\bsglang\b", "SGLang"),
        (r"\btrt\b", "TRT"),
        (r"\bagile\b", "Agile"),
        (r"\bdevops\b", "DevOps"),
        (r"\bscrum\b", "Scrum"),
    ]
    found: list[str] = []
    seen: set[str] = set()

    def add_skill(label: str) -> None:
        key = label.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(label)

    for pattern, label in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            add_skill(label)

    for word in query_words:
        if re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE):
            add_skill(word)
    return "; ".join(found)


def _looks_like_navigation(text: str) -> bool:
    normalized = text.lower()
    return normalized in {"войти", "создать резюме", "вакансии", "на карте", "откликнуться"} or len(normalized) < 3


def _stable_link_id(link: str) -> str:
    compact = re.sub(r"[^a-zA-Z0-9а-яА-Я_-]+", "-", link).strip("-")
    return compact[-96:] or "unknown"


def _compact_html_for_llm(html: str) -> str:
    anchor_fragments = []
    for match in re.finditer(r"<a\b.*?</a>", html, flags=re.IGNORECASE | re.DOTALL):
        fragment = match.group(0)
        if any(
            part in fragment.lower()
            for part in (
                "vacancy",
                "vacancies",
                "vakansii",
                "career",
                "job",
                "desc",
                "rabota",
                "trudvsem",
                "geekjob",
                "avito",
                "работ",
                "аналит",
                "junior",
                "стаж",
            )
        ):
            anchor_fragments.append(fragment[:800])
        if len(anchor_fragments) >= 80:
            break
    compact = "\n".join(anchor_fragments) or html[:15000]
    return clean_text(compact)[:15000]


def _vacancy_candidate_cards(html: str, *, source: str, page_url: str, limit: int) -> list[dict[str, str]]:
    anchors: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL):
        attrs = match.group("attrs")
        href = _extract_attr(attrs, "href")
        if not href:
            continue
        title = clean_text(match.group("body")) or _extract_attr(attrs, "aria-label") or _extract_attr(attrs, "title")
        title = _clean_scraped_text(title)
        if not title or _looks_like_navigation(title):
            continue
        absolute_link = urljoin(page_url, href)
        if absolute_link.lower() in seen:
            continue
        if not _looks_like_vacancy_link(source, absolute_link, title):
            continue
        seen.add(absolute_link.lower())
        block = _vacancy_card_block(html, match.start(), match.end(), fallback_radius=900)
        nearby_text = _clean_scraped_text(block)[:800]
        anchors.append(
            {
                "title_candidate": title[:180],
                "title": title[:180],
                "link": href,
                "href": href,
                "absolute_link": absolute_link,
                "nearby_text": nearby_text,
                "possible_company": _extract_first_anchor_text(block, _company_href_parts_for_source(source)),
                "possible_city": _extract_city(block),
                "possible_salary": _extract_salary_text(block),
            }
        )
        if len(anchors) >= limit:
            break
    return anchors


def _company_href_parts_for_source(source: str) -> tuple[str, ...]:
    return {
        "hh": ("/employer/", "hh.ru/employer/"),
        "superjob": ("/clients/", "superjob.ru/clients/"),
        "rabota_ru": ("/company/", "/companies/", "/employer/"),
        "avito": ("/brands/", "/company/", "/rabotodateli/"),
        "zarplata": ("/employer/", "/company/"),
        "gorodrabot": ("/company/", "/companies/"),
        "jooble": ("/company/", "/companies/"),
        "habr": ("/companies/",),
        "geekjob": ("/company/", "/companies/"),
        "trudvsem": ("/employer/", "/company/"),
    }.get(source, ())


def _looks_like_vacancy_link(source: str, link: str, title: str) -> bool:
    value = f"{link} {title}".lower().replace("ё", "е")
    source_markers = {
        "hh": ("hh.ru/vacancy/",),
        "superjob": ("superjob.ru/vakansii/", "superjob.ru/vacancy/"),
        "rabota_ru": ("rabota.ru/vacancy/",),
        "avito": ("/vakansii/",),
        "zarplata": ("zarplata.ru/vacancy/",),
        "gorodrabot": ("gorodrabot.ru/vacancy/",),
        "jooble": ("jooble.org/desc/", "jooble.org/vacancies/"),
        "habr": ("career.habr.com/vacancies/", "/vacancies/"),
        "geekjob": ("geekjob.ru/vacancy/",),
        "trudvsem": ("trudvsem.ru/vacancy/", "trudvsem.ru/vacancy/card/"),
    }
    if any(marker in value for marker in source_markers.get(source, ())):
        return True
    return any(marker in value for marker in ("ваканси", "vacancy", "job", "работ", "аналит", "junior", "стажер", "стажёр"))
