from __future__ import annotations

from collections import Counter, defaultdict
from html import unescape
import re
from typing import Any
from urllib.parse import urlparse


TECHNICAL_MARKERS = (
    "class=",
    "data-",
    "aria-",
    "svg",
    "path",
    "fill-rule",
    "clip-rule",
    "onclick",
    "javascript",
    "<script",
    "<style",
)

SERVICE_WORDS = (
    "apply",
    "login",
    "sign in",
    "create resume",
    "favorite",
    "share",
    "show phone",
    "cookie",
    "subscribe",
    "filter",
    "sort",
    "откликнуться",
    "войти",
    "создать резюме",
    "избранное",
    "поделиться",
    "показать телефон",
    "подписаться",
    "фильтр",
    "сортировка",
    "Ð¾Ñ‚ÐºÐ»Ð¸ÐºÐ½ÑƒÑ‚ÑŒÑÑ",
    "Ð²Ð¾Ð¹Ñ‚Ð¸",
    "ÑÐ¾Ð·Ð´Ð°Ñ‚ÑŒ Ñ€ÐµÐ·ÑŽÐ¼Ðµ",
    "Ð¸Ð·Ð±Ñ€Ð°Ð½Ð½Ð¾Ðµ",
    "Ð¿Ð¾Ð´ÐµÐ»Ð¸Ñ‚ÑŒÑÑ",
    "Ð¿Ð¾ÐºÐ°Ð·Ð°Ñ‚ÑŒ Ñ‚ÐµÐ»ÐµÑ„Ð¾Ð½",
    "Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ°Ñ‚ÑŒÑÑ",
    "Ñ„Ð¸Ð»ÑŒÑ‚Ñ€",
    "ÑÐ¾Ñ€Ñ‚Ð¸Ñ€Ð¾Ð²ÐºÐ°",
)

NAV_TITLES = {
    "apply",
    "login",
    "sign in",
    "jobs",
    "vacancies",
    "search",
    "map",
    "next",
    "previous",
    "войти",
    "вакансии",
    "на карте",
    "откликнуться",
    "создать резюме",
    "Ð²Ð¾Ð¹Ñ‚Ð¸",
    "Ð²Ð°ÐºÐ°Ð½ÑÐ¸Ð¸",
    "Ð½Ð° ÐºÐ°Ñ€Ñ‚Ðµ",
    "Ð¾Ñ‚ÐºÐ»Ð¸ÐºÐ½ÑƒÑ‚ÑŒÑÑ",
    "ÑÐ¾Ð·Ð´Ð°Ñ‚ÑŒ Ñ€ÐµÐ·ÑŽÐ¼Ðµ",
}

FORMAT_HINTS = {
    "remote": ("remote", "удален", "удалён", "Ð°Ð»ÐµÐ½"),
    "hybrid": ("hybrid", "гибрид", "Ð³Ð¸Ð±Ñ€Ð¸Ð´"),
    "onsite": ("office", "onsite", "офис", "Ð¾Ñ„Ð¸Ñ"),
}

SALARY_PATTERN = re.compile(
    r"(?ix)"
    r"(?:\b(?:от|до|from|to)\s*)?"
    r"\d[\d\s.,]{1,10}"
    r"(?:\s*[-–—]\s*(?:\b(?:от|до|from|to)\s*)?\d[\d\s.,]{1,10})?"
    r"\s*(?:₽(?:/\w+)?|руб\.?|rub|k|тыс\.?|сум|so['’]?m|usd|eur|\$|gross|net)(?=$|\s|[.,;)/])"
    r"|(?:\b(?:от|до|from|to)\s+\d[\d\s.,]{2,10}\b)"
    r"|(?:\b\d{2,4}\s*k\b)",
)


def clean_text(text: Any) -> str:
    value = unescape(str(text or ""))
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    value = re.sub(r"<(script|style|noscript|svg|canvas)\b.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"<[^<]*$", " ", value)
    value = _strip_scraped_artifacts(value)
    value = _drop_repeated_lines(value)
    return re.sub(r"\s+", " ", value).strip(" \t\r\n'>")


def clean_vacancy_fields(vacancy: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(vacancy)
    for key, value in list(cleaned.items()):
        if isinstance(value, str):
            cleaned[key] = clean_text(value)

    title = clean_text(cleaned.get("title") or cleaned.get("role"))
    if _looks_like_title_noise(title):
        cleaned["title"] = ""
    else:
        cleaned["title"] = title[:160].strip()
    cleaned["role"] = clean_text(cleaned.get("role")) or cleaned.get("title", "")

    company = clean_text(cleaned.get("company"))
    if _looks_like_company_noise(company, cleaned.get("title")):
        company = ""
    cleaned["company"] = company[:140].strip()

    for field, limit in (
        ("description", 12000),
        ("requirements", 6000),
        ("responsibilities", 6000),
        ("conditions", 6000),
        ("stack", 4000),
        ("key_skills", 4000),
    ):
        cleaned[field] = _clean_long_field(cleaned.get(field), limit=limit)

    cleaned["salary_rub"] = _clean_salary(cleaned.get("salary_rub"))
    cleaned["format"] = _clean_format(cleaned.get("format"), cleaned)
    cleaned["city"] = _clean_short_field(cleaned.get("city"), limit=80)
    cleaned["level"] = _clean_short_field(cleaned.get("level"), limit=80)
    cleaned["english_level"] = _clean_short_field(cleaned.get("english_level"), limit=80)
    cleaned["employment_type"] = _clean_short_field(cleaned.get("employment_type"), limit=100)
    cleaned["link"] = clean_text(cleaned.get("link"))
    return cleaned


def vacancy_quality_score(vacancy: dict[str, Any]) -> int:
    issues = explain_quality_issues(vacancy)
    score = 100
    weights = {
        "empty_title": 55,
        "title_too_long": 35,
        "title_navigation": 45,
        "description_html_noise": 45,
        "description_page_glue": 35,
        "description_repeated": 25,
        "invalid_salary": 20,
        "invalid_link": 30,
        "empty_company": 8,
        "company_equals_title": 20,
        "service_word_overload": 25,
    }
    for issue in issues:
        score -= weights.get(issue, 10)
    if clean_text(vacancy.get("title")) and _looks_like_job_signal(vacancy):
        score += 10
    return max(0, min(100, score))


def is_noisy_vacancy(vacancy: dict[str, Any]) -> bool:
    issues = set(explain_quality_issues(vacancy))
    if "empty_title" in issues:
        return True
    if "invalid_link" in issues and ("title_navigation" in issues or "description_page_glue" in issues):
        return True
    if "description_html_noise" in issues and "service_word_overload" in issues:
        return True
    if "title_navigation" in issues and not _looks_like_job_signal(vacancy):
        return True
    return vacancy_quality_score(vacancy) < 35


def explain_quality_issues(vacancy: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    title = clean_text(vacancy.get("title"))
    description = clean_text(vacancy.get("description"))
    salary = clean_text(vacancy.get("salary_rub"))
    link = clean_text(vacancy.get("link"))
    company = clean_text(vacancy.get("company"))
    combined = " ".join(clean_text(vacancy.get(key)) for key in ("title", "company", "description", "requirements", "conditions"))

    if not title:
        issues.append("empty_title")
    elif len(title) > 140 or len(title.split()) > 18:
        issues.append("title_too_long")
    if title and _looks_like_title_noise(title):
        issues.append("title_navigation")

    lowered_description = description.lower()
    if any(marker in lowered_description for marker in TECHNICAL_MARKERS):
        issues.append("description_html_noise")
    if len(description) > 12000 and _service_word_count(description) >= 4:
        issues.append("description_page_glue")
    if _has_many_repeats(description):
        issues.append("description_repeated")

    if salary and not _looks_like_salary(salary):
        issues.append("invalid_salary")
    if link and not _looks_like_vacancy_url(link):
        issues.append("invalid_link")
    if not link:
        issues.append("invalid_link")
    if not company:
        issues.append("empty_company")
    elif _normalize_for_compare(company) == _normalize_for_compare(title):
        issues.append("company_equals_title")
    if _service_word_count(combined) >= 5:
        issues.append("service_word_overload")

    return list(dict.fromkeys(issues))


def deduplicate_vacancies(vacancies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for vacancy in vacancies:
        key = _dedupe_key(vacancy)
        if not key:
            key = f"row:{len(order)}:{id(vacancy)}"
        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = vacancy
            order.append(key)
            continue
        if _dedupe_rank(vacancy) > _dedupe_rank(current):
            best_by_key[key] = vacancy
    return [best_by_key[key] for key in order]


def prepare_vacancies_for_output(vacancies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cleaned_rows: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    duplicate_drops: list[dict[str, Any]] = []
    duplicate_keys: set[str] = set()
    seen_keys: dict[str, dict[str, Any]] = {}

    for index, vacancy in enumerate(vacancies, start=1):
        cleaned = clean_vacancy_fields(vacancy)
        issues = explain_quality_issues(cleaned)
        score = vacancy_quality_score(cleaned)
        cleaned["_quality_score"] = score
        cleaned["_quality_issues"] = issues
        if is_noisy_vacancy(cleaned):
            dropped.append({"row": index, "vacancy": cleaned, "issues": issues, "score": score})
            continue
        key = _dedupe_key(cleaned)
        if key and key in seen_keys:
            duplicate_keys.add(key)
            duplicate_drops.append({"row": index, "vacancy": cleaned, "issues": ["duplicate"], "score": score})
            cleaned_rows.append(cleaned)
            if _dedupe_rank(cleaned) > _dedupe_rank(seen_keys[key]):
                seen_keys[key] = cleaned
            continue
        if key:
            seen_keys[key] = cleaned
        cleaned_rows.append(cleaned)

    if duplicate_keys:
        cleaned_rows = deduplicate_vacancies(cleaned_rows)
    report = build_quality_report(vacancies, cleaned_rows, dropped + duplicate_drops)
    return cleaned_rows, report


def build_quality_report(
    raw_vacancies: list[dict[str, Any]],
    kept_vacancies: list[dict[str, Any]],
    dropped_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    total_rows = len(raw_vacancies)
    kept_rows = len(kept_vacancies)
    noisy_kept = [vacancy for vacancy in kept_vacancies if explain_quality_issues(vacancy)]
    quality_by_source: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "kept": 0, "dropped": 0, "noisy": 0, "score_sum": 0})

    for vacancy in raw_vacancies:
        source = str(vacancy.get("source") or "unknown")
        quality_by_source[source]["total"] += 1
    for vacancy in kept_vacancies:
        source = str(vacancy.get("source") or "unknown")
        quality_by_source[source]["kept"] += 1
        quality_by_source[source]["score_sum"] += vacancy_quality_score(vacancy)
        if explain_quality_issues(vacancy):
            quality_by_source[source]["noisy"] += 1
    for entry in dropped_entries:
        vacancy = entry.get("vacancy") or {}
        source = str(vacancy.get("source") or "unknown")
        quality_by_source[source]["dropped"] += 1

    by_source_out = {}
    for source, stats in quality_by_source.items():
        kept = int(stats["kept"])
        by_source_out[source] = {
            "total": stats["total"],
            "kept": kept,
            "dropped": stats["dropped"],
            "noisy": stats["noisy"],
            "avg_quality_score": round(stats["score_sum"] / kept, 1) if kept else 0,
        }

    warnings: list[str] = []
    noisy_share = round((len(noisy_kept) + len(dropped_entries)) / total_rows, 3) if total_rows else 0
    if noisy_share > 0.25:
        warnings.append(f"High parser-noise share: {noisy_share:.1%}")
    dirty_sources = [source for source, stats in by_source_out.items() if stats["total"] and stats["dropped"] / stats["total"] > 0.25]
    if dirty_sources:
        warnings.append("Dirty sources: " + ", ".join(sorted(dirty_sources)))

    return {
        "total_rows": total_rows,
        "kept_rows": kept_rows,
        "dropped_rows": max(0, total_rows - kept_rows),
        "noisy_rows": len(noisy_kept) + len(dropped_entries),
        "noisy_share": noisy_share,
        "quality_by_source": by_source_out,
        "field_quality": _field_quality(kept_vacancies),
        "examples_of_noise": [_example(item) for item in noisy_kept[:5]],
        "dropped_examples": [_example(entry.get("vacancy") or {}, entry.get("issues") or []) for entry in dropped_entries[:5]],
        "warnings": warnings,
    }


def _field_quality(vacancies: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    fields = ("title", "company", "salary_rub", "city", "format", "key_skills", "link", "description")
    result: dict[str, dict[str, Any]] = {}
    total = len(vacancies)
    for field in fields:
        present = sum(1 for vacancy in vacancies if clean_text(vacancy.get(field)))
        noisy = 0
        for vacancy in vacancies:
            value = clean_text(vacancy.get(field))
            if not value:
                continue
            if field == "salary_rub" and not _looks_like_salary(value):
                noisy += 1
            elif field == "link" and not _looks_like_vacancy_url(value):
                noisy += 1
            elif any(marker in value.lower() for marker in TECHNICAL_MARKERS):
                noisy += 1
        result[field] = {
            "present": present,
            "missing": max(0, total - present),
            "noisy": noisy,
            "present_share": round(present / total, 3) if total else 0,
        }
    return result


def _example(vacancy: dict[str, Any], issues: list[str] | None = None) -> dict[str, Any]:
    return {
        "source": vacancy.get("source", ""),
        "title": clean_text(vacancy.get("title"))[:120],
        "company": clean_text(vacancy.get("company"))[:80],
        "link": clean_text(vacancy.get("link"))[:180],
        "issues": issues if issues is not None else explain_quality_issues(vacancy),
        "quality_score": vacancy_quality_score(vacancy),
        "description_snippet": clean_text(vacancy.get("description"))[:220],
    }


def _strip_scraped_artifacts(value: str) -> str:
    text = value
    text = re.sub(r"\b[a-z]*onse\?\s*vacancyId=\d+[^\s\"'<]*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:fill-rule|clip-rule|fill-opacity|fill|class|style|data-[\w-]+|aria-[\w-]+|xlink:href|title|type)=[\"'][^\"']*[\"']", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:f-test-[\w-]+|undefined|magritte-[\w-]+)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(?:[A-Za-z0-9_-]{2,}\s+){2,}[A-Za-z0-9_-]{2,}[\"']?>\s*", " ", text)
    text = re.sub(r"^\s*(?:span|pan|div|svg|path|button|class)[\"']?>\s*", " ", text, flags=re.IGNORECASE)
    starts_with_iso_datetime = bool(re.match(r"\s*\d{4}-\d{2}-\d{2}T\d{2}", text))
    if not starts_with_iso_datetime and not SALARY_PATTERN.search(text[:120]):
        text = re.sub(r"^\s*[A-Za-z]?\d+(?:[.\s,-]*[A-Za-z]?\d+){5,}[A-Za-z]*[\"']?\s*>?\s*", " ", text)
    if not starts_with_iso_datetime and not SALARY_PATTERN.search(text):
        text = re.sub(r"\b[A-Za-z]?\d+(?:\.\d+)?(?:[.\s,-]+[A-Za-z]?\d+(?:\.\d+)?){7,}[A-Za-z]*\b", " ", text)
    text = re.sub(r"^\s*[\"']?>\s*", " ", text)
    text = re.sub(r"\b(?:Apply|Chat|Favorite|Share|Show phone|ÐžÑ‚ÐºÐ»Ð¸ÐºÐ½ÑƒÑ‚ÑŒÑÑ|Ð§Ð°Ñ‚|Ð”Ð¾Ð±Ð°Ð²Ð¸Ñ‚ÑŒ Ð² Ð¸Ð·Ð±Ñ€Ð°Ð½Ð½Ð¾Ðµ)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\+7\s*\d{3}\s*\d{3}[•â€¢\d]*", " ", text)
    text = re.sub(r"\b(?:Сегодня|Вчера|Ð¡ÐµÐ³Ð¾Ð´Ð½Ñ|Ð’Ñ‡ÐµÑ€Ð°)\s*(?:(?:в|Ð²)\s*\d{1,2}:?\d{2})?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+\s+(?:зарплат[аы]?|отзыв(?:ов|а)?|Ð·Ð°Ñ€Ð¿Ð»Ð°Ñ‚[Ð°Ñ‹]?|Ð¾Ñ‚Ð·Ñ‹Ð²(?:Ð¾Ð²|Ð°)?)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:Перейти в каталог компаний|ÐŸÐµÑ€ÐµÐ¹Ñ‚Ð¸ Ð² ÐºÐ°Ñ‚Ð°Ð»Ð¾Ð³ ÐºÐ¾Ð¼Ð¿Ð°Ð½Ð¸Ð¹)\b", " ", text, flags=re.IGNORECASE)
    return text


def _drop_repeated_lines(value: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in re.split(r"[\r\n]+", value) if line.strip()]
    if len(lines) < 4:
        return value
    result: list[str] = []
    seen: Counter[str] = Counter()
    for line in lines:
        key = line.lower()
        seen[key] += 1
        if seen[key] <= 2:
            result.append(line)
    return " ".join(result)


def _clean_long_field(value: Any, *, limit: int) -> str:
    text = clean_text(value)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].strip()
    return text


def _clean_short_field(value: Any, *, limit: int) -> str:
    text = clean_text(value)
    if len(text) > limit or _service_word_count(text) >= 2 or any(marker in text.lower() for marker in TECHNICAL_MARKERS):
        return ""
    return text


def _clean_salary(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return text[:80] if _looks_like_salary(text) else ""


def _clean_format(value: Any, vacancy: dict[str, Any]) -> str:
    raw = clean_text(value)
    inferred = _infer_format(" ".join(clean_text(vacancy.get(key)) for key in ("format", "description", "conditions")))
    if raw.lower() in {"remote", "hybrid", "onsite", "office"}:
        return "onsite" if raw.lower() == "office" else raw.lower()
    if inferred and (len(raw) > 60 or _service_word_count(raw) or any(marker in raw.lower() for marker in TECHNICAL_MARKERS) or not raw):
        return inferred
    return raw[:60] if raw and _service_word_count(raw) == 0 else inferred


def _infer_format(value: str) -> str:
    text = value.lower().replace("ё", "е").replace("Ñ‘", "Ðµ")
    for normalized, hints in FORMAT_HINTS.items():
        if any(hint in text for hint in hints):
            return normalized
    return ""


def _looks_like_salary(value: str) -> bool:
    text = clean_text(value).lower()
    if not text:
        return False
    if len(re.findall(r"\d", text)) > 14:
        return False
    return bool(SALARY_PATTERN.search(text))


def _looks_like_vacancy_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    normalized = value.lower()
    return any(marker in normalized for marker in ("vacancy", "vakansii", "job", "career", "desc", "trudvsem", "rabota"))


def _looks_like_title_noise(value: str) -> bool:
    text = clean_text(value)
    normalized = _normalize_for_compare(text)
    if not text or normalized in NAV_TITLES:
        return True
    if len(text) < 3:
        return True
    if len(text) > 140 or len(text.split()) > 18:
        return True
    if _service_word_count(text) >= 2:
        return True
    return any(marker in text.lower() for marker in TECHNICAL_MARKERS)


def _looks_like_company_noise(company: str, title: Any) -> bool:
    if not company:
        return False
    if len(company) > 140 or _service_word_count(company) >= 2:
        return True
    if _normalize_for_compare(company) == _normalize_for_compare(title):
        return True
    return any(marker in company.lower() for marker in TECHNICAL_MARKERS)


def _looks_like_job_signal(vacancy: dict[str, Any]) -> bool:
    text = " ".join(clean_text(vacancy.get(key)) for key in ("title", "description", "requirements", "stack", "key_skills")).lower()
    return any(marker in text for marker in ("analyst", "developer", "engineer", "manager", "sql", "python", "junior", "intern", "аналит", "разработ", "стаж"))


def _service_word_count(value: str) -> int:
    text = clean_text(value).lower()
    return sum(1 for word in SERVICE_WORDS if word and word in text)


def _has_many_repeats(value: str) -> bool:
    words = [word for word in re.findall(r"[\wА-Яа-яÐ-Ñ]{3,}", clean_text(value).lower()) if word not in {"the", "and"}]
    if len(words) < 80:
        return False
    counts = Counter(words)
    repeated = sum(count for _, count in counts.most_common(8))
    return repeated / len(words) > 0.38


def _dedupe_key(vacancy: dict[str, Any]) -> str:
    link = clean_text(vacancy.get("link")).lower()
    if link:
        link = re.sub(r"[?#].*$", "", link).rstrip("/")
    title = _normalize_for_compare(vacancy.get("title"))
    company = _normalize_for_compare(vacancy.get("company"))
    city = _normalize_for_compare(vacancy.get("city"))
    if link:
        return f"{title}|{company}|{city}|{link}"
    if title and (company or city):
        return f"{title}|{company}|{city}"
    return ""


def _dedupe_rank(vacancy: dict[str, Any]) -> tuple[int, int, int]:
    useful_text = " ".join(clean_text(vacancy.get(key)) for key in ("description", "requirements", "responsibilities", "conditions", "key_skills"))
    filled_fields = sum(1 for key in ("company", "city", "salary_rub", "format", "key_skills", "description") if clean_text(vacancy.get(key)))
    return vacancy_quality_score(vacancy), filled_fields, len(useful_text)


def _normalize_for_compare(value: Any) -> str:
    text = clean_text(value).lower().replace("ё", "е").replace("Ñ‘", "Ðµ")
    text = re.sub(r"[^0-9a-zа-яÐ-Ñ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
