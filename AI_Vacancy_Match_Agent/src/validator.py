from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VACANCY_FIELD_ALIASES = {
	"title": ("title", "name"),
	"role": ("role", "position", "title"),
	"skills": ("skills", "key_skills", "stack", "requirements", "tech_stack"),
	"location": ("location", "city", "region"),
	"work_format": ("work_format", "format", "employment_type"),
	"salary": ("salary", "salary_rub", "compensation", "pay"),
	"url": ("url", "link"),
}

CRITERIA_FIELD_ALIASES = {
	"target_roles": ("target_roles", "role", "target_role", "desired_role"),
	"skills": ("skills", "skillset", "stack"),
	"preferred_levels": ("preferred_levels", "level", "levels"),
	"preferred_formats": ("preferred_formats", "work_format", "format", "employment_format"),
	"preferred_cities": ("preferred_cities", "location", "city", "preferred_city"),
}


@dataclass(slots=True)
class ValidationReport:
	total_vacancies: int
	valid_vacancies: int
	duplicates: int = 0
	empty_fields: int = 0
	missing_links: int = 0
	broken_rows: int = 0
	warnings: list[str] = field(default_factory=list)


def _is_empty(value: Any) -> bool:
	return value is None or str(value).strip() == ""


def _normalize_keys(mapping: dict[str, Any]) -> dict[str, Any]:
	return {str(key).strip().lower(): value for key, value in mapping.items()}


def _field_value(mapping: dict[str, Any], aliases: tuple[str, ...]) -> Any:
	for alias in aliases:
		value = mapping.get(alias)
		if not _is_empty(value):
			return value
	return ""


def _has_criteria(criteria: dict[str, Any], aliases: tuple[str, ...]) -> bool:
	return not _is_empty(_field_value(criteria, aliases))


def validate_data(
	vacancies: list[dict[str, Any]],
	criteria: dict[str, Any],
	loader_warnings: list[str] | None = None,
) -> tuple[list[dict[str, Any]], ValidationReport]:
	seen_signatures: set[tuple[str, str]] = set()
	valid_vacancies: list[dict[str, Any]] = []
	duplicates = 0
	empty_fields = 0
	missing_links = 0
	broken_rows = 0
	warnings: list[str] = []

	if loader_warnings:
		warnings.extend(loader_warnings)

	if not vacancies:
		warnings.append("Vacancies file is empty or unreadable.")

	for index, vacancy in enumerate(vacancies, start=1):
		normalized = _normalize_keys(vacancy)
		row_number = normalized.get("__row_number__", index)

		if normalized.get("__extra_columns__"):
			broken_rows += 1
			warnings.append(f"Broken CSV row {row_number}: unexpected extra columns.")

		visible_values = [value for key, value in normalized.items() if not key.startswith("__")]
		if not visible_values or all(_is_empty(value) for value in visible_values):
			broken_rows += 1
			warnings.append(f"Broken CSV row {row_number}: row is empty.")
			continue

		title = str(_field_value(normalized, VACANCY_FIELD_ALIASES["title"])).strip().lower()
		role = str(_field_value(normalized, VACANCY_FIELD_ALIASES["role"])).strip().lower()
		company = str(normalized.get("company") or normalized.get("employer") or "").strip().lower()
		link = str(_field_value(normalized, VACANCY_FIELD_ALIASES["url"])).strip().lower()
		vacancy_id = str(normalized.get("vacancy_id") or normalized.get("id") or "").strip().lower()
		signature = (vacancy_id or link or title or role, company)

		if signature in seen_signatures:
			duplicates += 1
			warnings.append(f"Duplicate vacancy at row {row_number}: {title or role or 'unknown'}")
			continue
		seen_signatures.add(signature)

		empty_required = [
			field
			for field, aliases in VACANCY_FIELD_ALIASES.items()
			if _is_empty(_field_value(normalized, aliases))
		]
		if empty_required:
			empty_fields += len(empty_required)
			warnings.append(f"Vacancy at row {row_number} has empty fields: {', '.join(empty_required)}")

		url = str(_field_value(normalized, VACANCY_FIELD_ALIASES["url"])).strip()
		if not url:
			missing_links += 1

		valid_vacancies.append(vacancy)

	if not criteria:
		warnings.append("Criteria file is empty or unreadable.")
	else:
		normalized_criteria = _normalize_keys(criteria)
		for field, aliases in CRITERIA_FIELD_ALIASES.items():
			if not _has_criteria(normalized_criteria, aliases):
				warnings.append(f"Criteria field is missing: {field}")

	return valid_vacancies, ValidationReport(
		total_vacancies=len(vacancies),
		valid_vacancies=len(valid_vacancies),
		duplicates=duplicates,
		empty_fields=empty_fields,
		missing_links=missing_links,
		broken_rows=broken_rows,
		warnings=warnings,
	)
