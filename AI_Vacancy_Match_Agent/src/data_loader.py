from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from html import unescape
from pathlib import Path
import re
from typing import Any


@dataclass(slots=True)
class LoadedData:
	vacancies: list[dict[str, Any]]
	criteria: dict[str, Any]
	vacancies_path: Path
	criteria_path: Path
	load_warnings: list[str]


def _clean_key(key: Any) -> str:
	return str(key or "").strip().lstrip("\ufeff").lower()


def _clean_value(value: Any) -> Any:
	if isinstance(value, str):
		text = unescape(value).strip()
		text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
		text = re.sub(r"<(script|style|noscript|svg|canvas)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
		text = re.sub(r"<[^>]+>", " ", text)
		text = re.sub(r"<[^<]*$", " ", text)
		text = _strip_scraped_artifacts(text)
		return re.sub(r"\s+", " ", text).strip(" \t\r\n\"'>")
	return value


def _read_csv_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
	warnings: list[str] = []
	if not path.exists():
		return [], [f"Input file not found: {path}"]
	if path.stat().st_size == 0:
		return [], [f"Input file is empty: {path}"]

	with path.open(newline="", encoding="utf-8-sig") as file:
		reader = csv.DictReader(file)
		if not reader.fieldnames:
			return [], [f"CSV file has no header: {path}"]

		rows: list[dict[str, Any]] = []
		for row_number, row in enumerate(reader, start=2):
			cleaned: dict[str, Any] = {"__row_number__": row_number}
			for key, value in row.items():
				if key is None:
					extra_values = [_clean_value(item) for item in value or []]
					cleaned["__extra_columns__"] = extra_values
					warnings.append(f"CSV row {row_number} has extra columns: {extra_values}")
					continue
				cleaned[_clean_key(key)] = _clean_value(value)
			rows.append(cleaned)

	return rows, warnings


def _read_json_payload(path: Path) -> tuple[Any, list[str]]:
	if not path.exists():
		return [], [f"Input file not found: {path}"]
	if path.stat().st_size == 0:
		return [], [f"Input file is empty: {path}"]

	try:
		return json.loads(path.read_text(encoding="utf-8-sig")), []
	except json.JSONDecodeError as error:
		return [], [f"JSON file is unreadable: {path} ({error})"]


def _read_markdown_criteria(path: Path) -> tuple[dict[str, str], list[str]]:
	if not path.exists():
		return {}, [f"Input file not found: {path}"]
	if path.stat().st_size == 0:
		return {}, [f"Input file is empty: {path}"]

	criteria: dict[str, str] = {}
	for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
		line = raw_line.strip().lstrip("-*").strip()
		if not line or line.startswith("#"):
			continue
		if ":" in line:
			key, value = line.split(":", 1)
		elif "=" in line:
			key, value = line.split("=", 1)
		else:
			continue
		clean_key = _clean_key(key)
		if clean_key:
			criteria[clean_key] = str(value).strip()

	return criteria, []


def _records_from_payload(payload: Any, preferred_key: str) -> list[dict[str, Any]]:
	if isinstance(payload, list):
		return [_normalize_mapping(item) for item in payload if isinstance(item, dict)]
	if isinstance(payload, dict):
		nested = payload.get(preferred_key)
		if isinstance(nested, list):
			return [_normalize_mapping(item) for item in nested if isinstance(item, dict)]
		return [_normalize_mapping(payload)]
	return []


def _normalize_mapping(mapping: dict[Any, Any]) -> dict[str, Any]:
	return {_clean_key(key): _clean_value(value) for key, value in mapping.items()}


def _read_records(path: Path, preferred_key: str) -> tuple[list[dict[str, Any]], list[str]]:
	suffix = path.suffix.lower()
	if suffix == ".csv":
		rows, warnings = _read_csv_rows(path)
		if preferred_key == "vacancies":
			rows = [_normalize_loaded_vacancy(row) for row in rows]
		return rows, warnings
	if suffix == ".json":
		payload, warnings = _read_json_payload(path)
		rows = _records_from_payload(payload, preferred_key)
		if preferred_key == "vacancies":
			rows = [_normalize_loaded_vacancy(row) for row in rows]
		return rows, warnings
	return [], [f"Unsupported input format for records: {path}"]


def _normalize_loaded_vacancy(row: dict[str, Any]) -> dict[str, Any]:
	normalized = dict(row)
	for field in ("format", "work_format"):
		value = str(normalized.get(field) or "")
		inferred = _infer_format(" ".join(str(normalized.get(key) or "") for key in (field, "description", "conditions")))
		if inferred and (_looks_like_html_artifact(value) or len(value) > 80 or not _is_known_format(value)):
			normalized[field] = inferred
	return normalized


def _looks_like_html_artifact(value: str) -> bool:
	lowered = value.lower()
	return any(marker in lowered for marker in ("response?vacancyid", "magritte-", "data-qa=", "<div", "<path", "fill-rule", "banner-adfox"))


def _infer_format(value: str) -> str:
	text = value.lower().replace("ё", "е")
	if "remote" in text or "удален" in text:
		return "remote"
	if "hybrid" in text or "гибрид" in text:
		return "hybrid"
	if "office" in text or "офис" in text or "onsite" in text:
		return "onsite"
	return ""


def _is_known_format(value: str) -> bool:
	return value.strip().lower() in {"remote", "hybrid", "onsite", "office"}


def _strip_leading_card_controls(value: str) -> str:
	text = re.sub(r"\b(?:Apply|Откликнуться|Чат|Добавить в избранное)\b", " ", value, flags=re.IGNORECASE)
	text = re.sub(r"\+7\s*\d{3}\s*\d{3}[•\d]*", " ", text)
	text = re.sub(r"\b(?:Сегодня|Вчера|\d{1,2}\s+[А-Яа-я]+)\s*(?:в\s*\d{1,2}:\d{2})?", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\b(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\b\d+\s+зарплат[аы]?\b", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\b\d+\s+отзыв(?:ов|а)?\b", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\bПерейти в каталог компаний\b", " ", text, flags=re.IGNORECASE)
	return text


def _strip_scraped_artifacts(value: str) -> str:
	text = value
	text = re.sub(r"\b[a-z]*onse\?\s*vacancyId=\d+[^\s\"'<]*", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\b(?:fill-rule|clip-rule|fill-opacity|fill|class|style|data-[\w-]+|aria-[\w-]+|xlink:href|title|type)=[\"'][^\"']*[\"']", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\b(?:f-test-[\w-]+|undefined)\b", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"^\s*(?:[A-Za-z0-9_-]{2,}\s+){2,}[A-Za-z0-9_-]{2,}[\"']?>\s*", " ", text)
	text = re.sub(r"^\s*(?:span|pan|div|svg|path|button|class)[\"']?>\s*", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"^\s*[A-Za-z]?\d+(?:[.\s,-]*[A-Za-z]?\d+){5,}[A-Za-z]*[\"']?\s*>?\s*", " ", text)
	text = re.sub(r"\b[A-Za-z]?\d+(?:\.\d+)?(?:[.\s,-]+[A-Za-z]?\d+(?:\.\d+)?){7,}[A-Za-z]*\b", " ", text)
	text = re.sub(r"^\s*[\"']?>\s*", " ", text)
	return _strip_leading_card_controls(text)


def _normalize_criteria(rows: list[dict[str, Any]]) -> dict[str, Any]:
	criteria: dict[str, Any] = {}
	for row in rows:
		raw_key = (row.get("key") or row.get("criteria") or row.get("name") or "").strip()
		raw_value = (row.get("value") or row.get("text") or row.get("description") or "").strip()
		if raw_key:
			criteria[raw_key.lower()] = raw_value
			continue

		for key, value in row.items():
			if key.startswith("__"):
				continue
			if value not in (None, ""):
				criteria[_clean_key(key)] = _clean_value(value)
	return criteria


def load_data(vacancies_path: str | Path, criteria_path: str | Path) -> LoadedData:
	vacancies_file = Path(vacancies_path)
	criteria_file = Path(criteria_path)

	vacancies, vacancy_warnings = _read_records(vacancies_file, "vacancies")

	if criteria_file.suffix.lower() == ".md":
		criteria, criteria_warnings = _read_markdown_criteria(criteria_file)
	elif criteria_file.suffix.lower() == ".json":
		payload, criteria_warnings = _read_json_payload(criteria_file)
		criteria = _normalize_criteria(_records_from_payload(payload, "criteria"))
	else:
		criteria_rows, criteria_warnings = _read_csv_rows(criteria_file)
		criteria = _normalize_criteria(criteria_rows)

	return LoadedData(
		vacancies=vacancies,
		criteria=criteria,
		vacancies_path=vacancies_file,
		criteria_path=criteria_file,
		load_warnings=vacancy_warnings + criteria_warnings,
	)
