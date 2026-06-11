from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
	sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent import explain_vacancies
from data_loader import load_data
from llm_client import LLMClient
from reporter import generate_outputs
from scorer import ScoringResult, rank_vacancies, score_vacancies
from validator import validate_data


FIELD_ALIASES = {
	"title": ("title", "name"),
	"company": ("company", "employer"),
	"role": ("role", "position", "title"),
	"level": ("level", "seniority"),
	"skills": ("skills", "stack", "key_skills", "requirements", "tech_stack"),
	"location": ("location", "city", "region"),
	"work_format": ("work_format", "format", "employment_type"),
	"salary": ("salary", "salary_rub", "compensation", "pay"),
	"english": ("english", "english_level", "language"),
	"url": ("url", "link"),
	"published_at": ("published_at", "date", "created_at"),
	"deadline": ("deadline", "expires_at"),
	"vacancy_id": ("vacancy_id", "id"),
}


def _progress(stage: str, completed: int, total: int) -> None:
	total = max(total, 1)
	_progress_percent(stage, (completed / total) * 100)


def _progress_percent(stage: str, percent: float) -> None:
	percent = max(0, min(100, int(percent)))
	bar_width = 24
	filled = int(bar_width * percent / 100)
	bar = "#" * filled + "-" * (bar_width - filled)
	_safe_print(f"[{bar}] {percent:3d}% {stage}")


def _safe_print(value: Any = "") -> None:
	text = str(value)
	try:
		print(text, flush=True)
	except UnicodeEncodeError:
		encoding = sys.stdout.encoding or "utf-8"
		sys.stdout.buffer.write((text + "\n").encode(encoding, errors="backslashreplace"))
		sys.stdout.flush()


def _require_llm_stage_success(llm_client: Any, stage: str, produced_count: int, target_count: int, label: str) -> None:
	if not getattr(llm_client, "enabled", False) or target_count <= 0:
		return
	message = _llm_stage_failure_message(llm_client, stage, produced_count, label)
	if message:
		raise RuntimeError(message)


def _warn_llm_stage_failure(llm_client: Any, stage: str, produced_count: int, label: str) -> None:
	message = _llm_stage_failure_message(llm_client, stage, produced_count, label)
	if message:
		_safe_print(f"Warning: {message}")


def _llm_stage_failure_message(llm_client: Any, stage: str, produced_count: int, label: str) -> str:
	if not getattr(llm_client, "enabled", False):
		return ""
	stage_trace = [item for item in getattr(llm_client, "call_trace", []) if item.get("stage") == stage]
	successes = [item for item in stage_trace if item.get("ok")]
	if not successes:
		reason = "; ".join(str(item.get("reason") or item.get("http_status") or "unknown") for item in stage_trace[-3:])
		return f"LLM {label} failed: no successful response from provider. {reason}".strip()
	if produced_count <= 0:
		response_ids = ", ".join(str(item.get("response_id") or "-") for item in successes[-3:])
		return f"LLM {label} returned successful response but no usable items. response_id: {response_ids}"
	return ""


class ProgressTracker:
	def __init__(self, steps: list[tuple[str, int]]) -> None:
		self.steps = steps
		self.total_weight = sum(weight for _, weight in steps) or 1
		self.completed_weight = 0

	def start(self, step: str, detail: str = "") -> None:
		self.report(step, 0, detail)

	def finish(self, step: str, detail: str = "") -> None:
		self.report(step, 1, detail)
		self.completed_weight += self._weight(step)

	def report(self, step: str, fraction: float, detail: str = "") -> None:
		fraction = max(0.0, min(1.0, float(fraction or 0)))
		percent = ((self.completed_weight + self._weight(step) * fraction) / self.total_weight) * 100
		message = step if not detail else f"{step} · {detail}"
		_progress_percent(message, percent)

	def _weight(self, step: str) -> int:
		for name, weight in self.steps:
			if name == step:
				return weight
		return 1


def main(argv: list[str] | None = None) -> int:
	args = _parse_args(argv)
	vacancies_path = _resolve_input_path(
		args.vacancies,
		[PROJECT_ROOT / "vacancies.csv", PROJECT_ROOT / "vacancies.json"],
	)
	criteria_path = _resolve_input_path(
		args.criteria,
		[PROJECT_ROOT / "criteria.csv", PROJECT_ROOT / "criteria.json", PROJECT_ROOT / "criteria.md"],
	)
	output_dir = _resolve_path(args.output)
	top_k = max(1, min(args.top_k, 20))
	secret_files = [PROJECT_ROOT / ".env"]

	for secret_file in secret_files:
		_load_env_file(secret_file)

	detected_files = [path.name for path in secret_files if path.is_file()]
	if detected_files:
		print(f"Environment file detected: {', '.join(detected_files)}")
	llm_score_enabled = bool(args.llm_score and not args.dry_run)
	llm_explanation_enabled = bool(args.llm_explanation and not args.dry_run)
	llm_client = LLMClient.from_env(dry_run=args.dry_run or not (llm_score_enabled or llm_explanation_enabled))
	print(f"Run mode: {llm_client.mode} ({llm_client.reason})")

	if llm_client.enabled:
		stages = [
			("Load Data", 4),
			("Validate Data", 6),
			("Normalize Data", 4),
			("Apply Filters", 4),
			("Calculate Score", 24),
			("LLM Score Review", 22),
			("Rank Vacancies", 6),
			("LLM Rank Review", 12),
			("Agent Explanation", 14),
			("Generate Outputs", 4),
		]
	else:
		stages = [
			("Load Data", 5),
			("Validate Data", 8),
			("Normalize Data", 6),
			("Apply Filters", 6),
			("Calculate Score", 50),
			("LLM Score Review", 1),
			("Rank Vacancies", 12),
			("LLM Rank Review", 1),
			("Agent Explanation", 7),
			("Generate Outputs", 4),
		]
	progress = ProgressTracker(stages)

	progress.start("Load Data", "чтение CSV")
	data = load_data(vacancies_path, criteria_path)
	progress.finish("Load Data", f"загружено вакансий: {len(data.vacancies)}")

	progress.start("Validate Data", f"проверка строк: {len(data.vacancies)}")
	validated_vacancies, validation_report = validate_data(data.vacancies, data.criteria, data.load_warnings)
	progress.finish("Validate Data", f"валидных: {len(validated_vacancies)}/{len(data.vacancies)}")

	progress.start("Normalize Data", f"нормализация: {len(validated_vacancies)}")
	normalized_vacancies = _normalize_vacancies(validated_vacancies)
	progress.finish("Normalize Data", f"нормализовано: {len(normalized_vacancies)}/{len(validated_vacancies)}")

	progress.start("Apply Filters", f"фильтры: {len(normalized_vacancies)}")
	filtered_vacancies = _apply_filters(normalized_vacancies, data.criteria)
	filter_matched_count = sum(1 for vacancy in filtered_vacancies if vacancy.get("filter_passed"))
	progress.finish("Apply Filters", f"прошло фильтры: {filter_matched_count}/{len(filtered_vacancies)}")

	progress.start("Calculate Score", f"проверено: 0/{len(filtered_vacancies)}")
	def scoring_progress(done: int, total: int) -> None:
		progress.report("Calculate Score", done / max(total, 1), f"проверено: {done}/{total}")

	scoring_result = score_vacancies(
		filtered_vacancies,
		data.criteria,
		llm_client=llm_client if llm_score_enabled and llm_client.enabled else None,
		llm_limit=top_k,
		progress_callback=scoring_progress,
		use_llm_match_scoring=llm_score_enabled and llm_client.enabled,
		apply_llm_review=False,
	)
	if llm_score_enabled and llm_client.enabled:
		llm_match_count = sum(1 for vacancy in scoring_result.scored_vacancies if vacancy.get("llm_match_score_used"))
		_require_llm_stage_success(llm_client, "llm_match_score", llm_match_count, len(filtered_vacancies), "match scoring")
	progress.finish("Calculate Score", f"оценено: {len(scoring_result.scored_vacancies)}/{len(filtered_vacancies)}")

	progress.start("Rank Vacancies", f"ранжирование: {len(scoring_result.scored_vacancies)}")
	ranked_vacancies = rank_vacancies(scoring_result, data.criteria, llm_client=None, llm_limit=top_k)
	progress.finish("Rank Vacancies", f"ранжировано: {len(ranked_vacancies)}")

	llm_score_target = min(top_k, len(ranked_vacancies))
	llm_rank_target = min(top_k, len(ranked_vacancies))
	explain_target = min(top_k, len(ranked_vacancies))

	if llm_score_enabled and llm_client.enabled:
		progress.start("LLM Score Review", f"генерация комментариев: 0/{llm_score_target}" if llm_score_target else "пропущено")
		progress.start("LLM Rank Review", f"обоснование ранга: 0/{llm_rank_target}" if llm_rank_target else "пропущено")
	else:
		progress.start("LLM Score Review", "пропущено")
		progress.start("LLM Rank Review", "пропущено")
	if llm_explanation_enabled and llm_client.enabled:
		progress.start("Agent Explanation", f"LLM описание/риски: 0/{explain_target}" if explain_target else "пропущено")
	else:
		progress.start("Agent Explanation", f"rule-based объяснения: 0/{explain_target}")

	def llm_score_progress(done: int, total: int, detail: str = "") -> None:
		message = f"генерация комментариев: {done}/{total}"
		if detail:
			message = f"{message} · {detail}"
		progress.report("LLM Score Review", done / max(total, 1), message)

	def llm_rank_progress(done: int, total: int, detail: str = "") -> None:
		message = f"обоснование ранга: {done}/{total}"
		if detail:
			message = f"{message} · {detail}"
		progress.report("LLM Rank Review", done / max(total, 1), message)

	def llm_explanation_progress(done: int, total: int, detail: str = "") -> None:
		message = f"LLM описание/риски: {done}/{total}"
		if detail:
			message = f"{message} · {detail}"
		progress.report("Agent Explanation", done / max(total, 1), message)

	if llm_score_enabled and llm_client.enabled:
		def run_score_review() -> int:
			score_vacancies(
				ranked_vacancies,
				data.criteria,
				llm_client=llm_client,
				llm_limit=top_k,
				score_only_existing=True,
				llm_progress_callback=llm_score_progress,
			)
			return sum(1 for vacancy in ranked_vacancies[:llm_score_target] if vacancy.get("llm_score_used"))

		def run_rank_review() -> int:
			rank_vacancies(
				ScoringResult(scored_vacancies=list(ranked_vacancies)),
				data.criteria,
				llm_client=llm_client,
				llm_limit=top_k,
				llm_progress_callback=llm_rank_progress,
			)
			return sum(1 for vacancy in ranked_vacancies[:llm_rank_target] if vacancy.get("llm_rank_used"))

		score_comments = 0
		rank_comments = 0
		with ThreadPoolExecutor(max_workers=2) as executor:
			futures = {
				executor.submit(run_score_review): "score",
				executor.submit(run_rank_review): "rank",
			}
			for future in as_completed(futures):
				stage = futures[future]
				if stage == "score":
					score_comments = future.result()
					_warn_llm_stage_failure(llm_client, "calculate_score", score_comments, "score review")
					progress.finish("LLM Score Review", f"комментарии score: {score_comments}/{llm_score_target}" if llm_score_target else "пропущено")
				elif stage == "rank":
					rank_comments = future.result()
					_warn_llm_stage_failure(llm_client, "rank_vacancies", rank_comments, "rank review")
					progress.finish("LLM Rank Review", f"обоснования ранга: {rank_comments}/{llm_rank_target}" if llm_rank_target else "пропущено")
	else:
		progress.finish("LLM Score Review", "пропущено")
		progress.finish("LLM Rank Review", "пропущено")

	if llm_explanation_enabled and llm_client.enabled:
		agent_output = explain_vacancies(
			ranked_vacancies,
			data.criteria,
			limit=top_k,
			llm_client=llm_client,
			llm_progress_callback=llm_explanation_progress,
		)
		explanation_comments = sum(1 for vacancy in ranked_vacancies[:explain_target] if vacancy.get("llm_explanation_comment"))
		_warn_llm_stage_failure(llm_client, "agent_explanation", explanation_comments, "agent explanation")
		progress.finish("Agent Explanation", f"LLM описания: {explanation_comments}/{explain_target}" if explain_target else "пропущено")
	else:
		agent_output = explain_vacancies(ranked_vacancies, data.criteria, limit=top_k, llm_client=None)
		progress.finish("Agent Explanation", f"объяснения: {len(agent_output.explanations)}/{explain_target}")

	progress.start("Generate Outputs", "запись report/trace")
	outputs = generate_outputs(
		output_dir,
		data_summary={
			"vacancies_count": len(data.vacancies),
			"criteria_count": len(data.criteria),
			"vacancies_file": str(vacancies_path),
			"criteria_file": str(criteria_path),
			"dry_run": llm_client.mode == "dry_run",
			"run_mode": llm_client.mode,
			"llm_used": llm_client.enabled,
			"llm_model": llm_client.model if llm_client.enabled else "",
			"llm_reason": llm_client.reason,
			"top_k": top_k,
		},
		validation_report=validation_report,
		scoring_result=scoring_result,
		agent_output=agent_output,
		trace_context={
			"ranked_count": len(ranked_vacancies),
			"top_k": top_k,
			"filter_matched_count": filter_matched_count,
			"scored_after_validation_count": len(filtered_vacancies),
			"pipeline_steps": [name for name, _ in stages],
			"run_mode": llm_client.mode,
			"llm_trace": llm_client.call_trace,
		},
	)
	progress.finish("Generate Outputs", "файлы готовы")

	print(
		f"Outputs generated: {outputs['report'].name}, {outputs['methodology'].name}, "
		f"{outputs['log'].name}, {outputs['trace'].name}"
	)
	return 0


def _normalize_vacancies(vacancies: list[dict[str, Any]]) -> list[dict[str, Any]]:
	normalized: list[dict[str, Any]] = []
	for vacancy in vacancies:
		item = {str(key).strip().lower(): value for key, value in vacancy.items()}
		item["title"] = _first_value(item, FIELD_ALIASES["title"])
		item["company"] = _first_value(item, FIELD_ALIASES["company"])
		item["role"] = _first_value(item, FIELD_ALIASES["role"])
		item["level"] = _first_value(item, FIELD_ALIASES["level"])
		item["skills"] = _merged_value(item, FIELD_ALIASES["skills"])
		item["location"] = _sanitize_location_or_format(_first_value(item, FIELD_ALIASES["location"]))
		item["work_format"] = _sanitize_location_or_format(_first_value(item, FIELD_ALIASES["work_format"]))
		item["salary"] = _first_value(item, FIELD_ALIASES["salary"])
		item["english"] = _first_value(item, FIELD_ALIASES["english"])
		item["url"] = _first_value(item, FIELD_ALIASES["url"])
		item["published_at"] = _first_value(item, FIELD_ALIASES["published_at"])
		item["deadline"] = _first_value(item, FIELD_ALIASES["deadline"])
		item["vacancy_id"] = _first_value(item, FIELD_ALIASES["vacancy_id"])
		normalized.append(item)
	return normalized


def _apply_filters(vacancies: list[dict[str, Any]], criteria: dict[str, Any]) -> list[dict[str, Any]]:
	normalized_criteria = {str(key).strip().lower(): value for key, value in criteria.items()}
	target_locations = _split_values(
		normalized_criteria.get("preferred_cities")
		or normalized_criteria.get("location")
		or normalized_criteria.get("city")
		or normalized_criteria.get("preferred_city")
	)
	target_formats = _split_values(
		normalized_criteria.get("preferred_formats")
		or normalized_criteria.get("work_format")
		or normalized_criteria.get("format")
		or normalized_criteria.get("employment_format")
	)

	if not target_locations and not target_formats:
		return [dict(vacancy, filter_passed=True, filter_reasons=[]) for vacancy in vacancies]

	filtered: list[dict[str, Any]] = []
	for vacancy in vacancies:
		vacancy_location = str(vacancy.get("location") or "").lower()
		vacancy_format = str(vacancy.get("work_format") or "").lower()
		vacancy_format_markers = _format_markers(vacancy_format)
		location_ok = not target_locations or any(
			_matches_text(location, vacancy_location)
			or (_matches_text(location, "удаленно remote") and "remote" in vacancy_format_markers)
			for location in target_locations
		)
		format_ok = not target_formats or bool(
			{marker for item in target_formats for marker in _format_markers(item)} & vacancy_format_markers
		)
		filter_reasons = []
		if not location_ok:
			filter_reasons.append("Город/удаленка вне предпочтений")
		if not format_ok:
			filter_reasons.append("Формат работы вне предпочтений")
		item = dict(vacancy)
		item["filter_passed"] = location_ok and format_ok
		item["filter_reasons"] = filter_reasons
		filtered.append(item)
	return filtered


def _load_env_file(path: Path) -> None:
	if not path.is_file():
		return

	for raw_line in path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		key = key.strip()
		value = value.strip().strip('"').strip("'")
		if key and key not in os.environ:
			os.environ[key] = value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Rank internship and junior vacancies for a candidate profile.")
	parser.add_argument("--vacancies", help="Path to vacancies.csv or vacancies.json")
	parser.add_argument("--criteria", help="Path to criteria.csv, criteria.json or criteria.md")
	parser.add_argument("--output", default=str(PROJECT_ROOT / "output"), help="Directory for report.md, run.log, trace.json")
	parser.add_argument("--top-k", type=int, default=5, help="Number of best vacancies to explain in the report, 1..20")
	parser.add_argument("--dry-run", action="store_true", help="Run deterministic rule-based pipeline without external LLM/API calls")
	parser.add_argument("--llm-score", action="store_true", help="Use LLM for score review and rank justification")
	parser.add_argument("--llm-explanation", action="store_true", help="Use LLM for vacancy card explanation text")
	return parser.parse_args(argv)


def _resolve_path(value: str | None) -> Path:
	if not value:
		return PROJECT_ROOT
	path = Path(value)
	return path if path.is_absolute() else PROJECT_ROOT / path


def _resolve_input_path(value: str | None, candidates: list[Path]) -> Path:
	if value:
		return _resolve_path(value)
	for candidate in candidates:
		if candidate.exists():
			return candidate
	return candidates[0]


def _first_value(mapping: dict[str, Any], aliases: tuple[str, ...]) -> str:
	for alias in aliases:
		value = mapping.get(alias)
		if value is not None and str(value).strip():
			return str(value).strip()
	return ""


def _merged_value(mapping: dict[str, Any], aliases: tuple[str, ...]) -> str:
	values = []
	for alias in aliases:
		value = mapping.get(alias)
		if value is not None and str(value).strip():
			values.append(str(value).strip())
	return " | ".join(dict.fromkeys(values))


_BAD_FIELD_TOKENS = (
	"response?vacancyid",
	"hhtmfrom",
	"vacancy_search_list",
	"vacancy-serp__vacancy",
	"data-qa",
	"magritte",
	"aria-",
	"tabindex",
	"role=",
	"href=",
	"откликнуться",
)


def _sanitize_location_or_format(value: Any) -> str:
	text = str(value or "").strip()
	if not text:
		return ""
	lowered = text.lower()
	if "<" in text or ">" in text:
		return ""
	if any(token in lowered for token in _BAD_FIELD_TOKENS):
		return ""
	if len(text) > 80:
		return ""
	if re.search(r"\bresponse\?vacancyid=\d+", lowered):
		return ""
	return text


def _split_values(value: Any) -> list[str]:
	if value is None:
		return []
	return [part.strip().lower() for part in str(value).replace("|", ";").split(";") if part.strip()]


def _matches_text(needle: str, haystack: str) -> bool:
	return needle.strip().lower().replace("ё", "е") in haystack.strip().lower().replace("ё", "е")


def _format_markers(value: str) -> set[str]:
	text = value.lower().replace("ё", "е")
	markers: set[str] = set()
	if "remote" in text or "удален" in text or "удаленно" in text:
		markers.add("remote")
	if "hybrid" in text or "гибрид" in text:
		markers.add("hybrid")
	if "onsite" in text or "office" in text or "офис" in text:
		markers.add("onsite")
	return markers or ({text} if text else set())


if __name__ == "__main__":
	raise SystemExit(main())
