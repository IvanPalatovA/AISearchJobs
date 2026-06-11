from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Iterable

VACANCY_COLUMNS = [
    "vacancy_id",
    "source",
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
    "salary_text",
    "payment_frequency",
    "stack",
    "key_skills",
    "english_level",
    "link",
    "description",
    "requirements",
    "responsibilities",
    "conditions",
    "employment_type",
    "employment_form",
    "experience",
    "schedule",
    "working_hours",
    "work_format",
    "address",
    "metro_stations",
    "employer_name",
    "agency_company",
    "company_description",
    "category",
    "published_at_text",
    "views_count",
    "detail_source",
    "raw_detail_text",
]


@dataclass(slots=True)
class FetchResult:
    source: str
    vacancies: list[dict[str, Any]]
    warnings: list[str]
    request_log: list[dict[str, Any]] = field(default_factory=list)


def clean_html(value: Any) -> str:
	text = unescape(str(value or ""))
	text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
	text = re.sub(r"<(script|style|noscript|svg|canvas)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
	text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"<[^>]+>", " ", text)
	text = re.sub(r"<[^<]*$", " ", text)
	text = re.sub(r"\s+", " ", text).strip()
	return text


def clean_text(value: Any) -> str:
	text = unescape(str(value or ""))
	text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
	text = re.sub(r"<(script|style|noscript|svg|canvas)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
	text = re.sub(r"<[^>]+>", " ", text)
	text = re.sub(r"<[^<]*$", " ", text)
	text = re.sub(r"\s+", " ", text).strip()
	return text


def join_values(values: Iterable[Any]) -> str:
    return "; ".join(str(value).strip() for value in values if str(value or "").strip())


def normalize_salary(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        salary_from = value.get("from") or value.get("payment_from")
        salary_to = value.get("to") or value.get("payment_to")
        currency = value.get("currency") or value.get("currency_type") or ""
        parts = []
        if salary_from:
            parts.append(str(salary_from))
        if salary_to and salary_to != salary_from:
            parts.append(str(salary_to))
        if not parts:
            return ""
        return "-".join(parts) + (f" {currency}" if currency else "")
    return str(value).strip()


def normalize_level(text: Any) -> str:
    value = str(text or "").lower().replace("ё", "е")
    if any(word in value for word in ("стаж", "intern", "trainee")):
        return "Internship"
    if any(word in value for word in ("junior", "entry", "младш", "начина")):
        return "Junior"
    if "middle" in value:
        return "Middle"
    if any(word in value for word in ("senior", "lead", "руковод", "главн")):
        return "Senior"
    if "нет опыта" in value or "no experience" in value or "noexperience" in value:
        return "Entry"
    if "1" in value and "3" in value:
        return "Junior"
    return str(text or "").strip()


def normalize_format(text: Any) -> str:
    value = str(text or "").lower().replace("ё", "е")
    if "remote" in value or "удален" in value:
        return "remote"
    if "hybrid" in value or "гибрид" in value:
        return "hybrid"
    if "office" in value or "офис" in value or "полный день" in value or "onsite" in value:
        return "onsite"
    return str(text or "").strip()


def unique_output_path(output_dir: str | Path, base_name: str = "vacancies", suffix: str = ".csv") -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{base_name}_{timestamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{base_name}_{timestamp}_{counter}{suffix}"
        counter += 1
    return candidate


def safe_output_path(output_dir: str | Path, filename: str = "vacancies.csv") -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    requested = Path(filename)
    stem = requested.stem or "vacancies"
    suffix = requested.suffix or ".csv"
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    return unique_output_path(directory, stem, suffix)


def write_vacancies_csv(vacancies: list[dict[str, Any]], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=VACANCY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for vacancy in vacancies:
            row = {column: vacancy.get(column, "") for column in VACANCY_COLUMNS}
            writer.writerow(row)
    return path


def deduplicate(vacancies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for vacancy in vacancies:
        signature = (
            str(vacancy.get("source") or "").lower(),
            str(vacancy.get("vacancy_id") or "").lower(),
            str(vacancy.get("link") or vacancy.get("title") or "").lower(),
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(vacancy)
    return result
