from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
import os
import re
import time
from typing import Any


LLM_BATCH_SIZE = 5
LLM_MAX_WORKERS = 6
LLM_EXPLANATION_DEADLINE_SECONDS = 120.0


def _normalize_text_key(value: Any) -> str:
	return re.sub(r"[^a-zа-я0-9+.#]+", " ", str(value or "").lower().replace("ё", "е")).strip()


def _dedupe_text_list(values: Any) -> list[str]:
	result: list[str] = []
	seen: set[str] = set()
	for value in values or []:
		text = str(value or "").strip()
		key = _normalize_text_key(text)
		if text and key and key not in seen:
			seen.add(key)
			result.append(text)
	return result


def _clean_llm_source_text(value: Any, *, limit: int) -> str:
	text = str(value or "").strip()
	if not text:
		return ""
	text = re.sub(r"\bСейчас\s+смотрят\b[^.?!\n\r]{0,80}", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\bВыплаты:\s*[^.?!\n\r]{0,80}", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\bОпыт\s+\d+\s*[-–]\s*(?:\d+)?\s*(?:года|лет|год)?\b", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\b(?:за\s+месяц,\s*)?на\s+руки\b", " ", text, flags=re.IGNORECASE)
	text = re.sub(r"\b\d+(?:[.,]\d+)?\s*•\b", " ", text)
	text = re.sub(r"\s+", " ", text).strip()
	return text[:limit]


def _is_noise_risk(value: Any) -> bool:
	text = _normalize_text_key(value)
	if not text:
		return True
	if re.search(r"^(но|однако|при этом)\b", text):
		return True
	if re.search(r"не критич|некритич|не является критич|не обязатель", text):
		return True
	return any(
		word in text
		for word in (
			"опечат",
			"транслит",
			"translit",
			"transcription",
			"транслитерац",
			"англицизм",
			"capital",
			"case",
			"uppercase",
			"lowercase",
			"регистр",
			"орфограф",
			"написан",
			"несовпада",
			"mismatch",
		)
	)


def _risk_contradicts_positive_score(value: Any, vacancy: dict[str, Any]) -> bool:
	text = _normalize_text_key(value)
	if not text:
		return True
	positive: set[str] = set()
	for item in vacancy.get("score_breakdown", []):
		if not isinstance(item, dict):
			continue
		try:
			points = int(float(item.get("points") or 0))
		except (TypeError, ValueError):
			points = 0
		if points > 0:
			positive.add(str(item.get("criterion") or ""))
	if "level_match" in positive and re.search(r"(уров|seniority|middle|senior|мидл|сеньор|junior|джун).*(ниже|не подход|вне|ожидан|недостат)|ниже ожиданий.*(мидл|сеньор|middle|senior)", text):
		return True
	if "work_format" in positive and re.search(r"(формат|офис|office|onsite).*(не подход|вне|не совпад|отсутств|не указан)", text):
		return True
	if "city" in positive and re.search(r"(город|локац|москва|moscow).*(не подход|вне|не совпад|отсутств|не указан)", text):
		return True
	return False


@dataclass(slots=True)
class AgentOutput:
	top_vacancies: list[dict[str, Any]]
	explanations: list[dict[str, Any]] = field(default_factory=list)


def explain_vacancies(
	scored_vacancies: list[dict[str, Any]],
	criteria: dict[str, Any] | None = None,
	limit: int = 5,
	llm_client: Any | None = None,
	llm_progress_callback: Any | None = None,
) -> AgentOutput:
	top_vacancies = scored_vacancies[:limit]
	explanations: list[dict[str, Any]] = []

	for priority, vacancy in enumerate(top_vacancies, start=1):
		reasons = vacancy.get("reasons", [])
		concerns = vacancy.get("concerns", [])
		llm_risks = vacancy.get("llm_risks") or vacancy.get("llm_score_risks") or []
		risks = [
			risk
			for risk in _dedupe_text_list(llm_risks if llm_risks else concerns)
			if not _is_noise_risk(risk) and not _risk_contradicts_positive_score(risk, vacancy)
		]
		matched_skills = vacancy.get("matched_skills", [])
		missing_skills = vacancy.get("missing_target_skills", [])
		score_breakdown = vacancy.get("score_breakdown", [])
		extracted_requirements = vacancy.get("extracted_requirements", [])
		explanations.append(
			{
				"priority": priority,
				"title": vacancy.get("title") or vacancy.get("normalized_title") or "Без названия",
				"company": vacancy.get("company") or "Не указана",
				"score": vacancy.get("score", 0),
				"why_fit": "; ".join(reasons) if reasons else "Частичное совпадение по метаданным и содержимому.",
				"matched_criteria": _matched_criteria(score_breakdown, reasons),
				"extracted_requirements": extracted_requirements,
				"risks": risks if risks else ["Критичных рисков не выявлено."],
				"what_to_improve": _build_next_step(matched_skills, missing_skills, concerns),
				"next_step": _build_application_step(vacancy),
				"application_priority": vacancy.get("application_priority") or f"P{priority}",
				"questions_to_employer": _build_questions(vacancy),
				"llm_used": bool(vacancy.get("llm_score_used") or vacancy.get("llm_rank_used")),
				"llm_comment": vacancy.get("llm_comment") or vacancy.get("llm_rank_comment") or "",
			}
		)

	_apply_llm_explanations(explanations, top_vacancies, criteria or {}, llm_client, progress_callback=llm_progress_callback)
	return AgentOutput(top_vacancies=top_vacancies, explanations=explanations)


def _matched_criteria(score_breakdown: list[dict[str, Any]], reasons: list[str]) -> list[str]:
	if not score_breakdown:
		return reasons

	criteria = []
	for item in score_breakdown:
		points = item.get("points", 0)
		if points > 0:
			criteria.append(f"{_criterion_label(item.get('criterion'))}: +{points}{_format_evidence(item.get('evidence'))}")
	return criteria or reasons


def _criterion_label(value: Any) -> str:
	return {
		"role_match": "Совпадение роли",
		"irrelevant_role": "Нерелевантная роль",
		"target_role_mismatch": "Должность вне целевых ролей",
		"llm_role_match": "LLM: совпадение должности",
		"llm_role_mismatch": "LLM: должность вне целевых ролей",
		"senior_lead_middle": "Неподходящий seniority",
		"level_match": "Подходящий уровень",
		"skills_match": "Совпадение навыков",
		"llm_skills_match": "LLM: совпадение навыков",
		"llm_skills_mismatch": "LLM: слабое совпадение навыков",
		"work_format": "Формат работы",
		"work_format_mismatch": "Формат работы вне предпочтений",
		"city": "Город",
		"city_mismatch": "Город / удалёнка вне предпочтений",
		"salary": "Зарплата",
		"salary_below_min": "Зарплата ниже минимума",
		"salary_missing": "Зарплата не указана",
		"english": "Английский",
		"english_mismatch": "Английский выше уровня кандидата",
		"fresh": "Свежая вакансия",
		"missing_link": "Нет ссылки",
		"stop_word": "Стоп-слово",
	}.get(str(value or ""), str(value or "Критерий"))


def _format_evidence(value: Any, *, limit: int = 90) -> str:
	if isinstance(value, list):
		text = ", ".join(str(item).strip() for item in value if str(item).strip())
	elif isinstance(value, dict):
		text = ", ".join(f"{key}={item}" for key, item in list(value.items())[:3])
	else:
		text = str(value or "").strip()
	if not text:
		return ""
	if len(text) <= limit:
		return f" ({text})"
	cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
	return f" ({cut + '…' if cut else text[:limit].rstrip() + '…'})"


def _build_next_step(matched_skills: list[str], missing_skills: list[str], concerns: list[str]) -> str:
	if matched_skills:
		skill_part = f"Подтянуть и подчеркнуть в резюме навыки: {', '.join(matched_skills[:5])}."
	else:
		skill_part = "Подтянуть ключевые навыки, указанные в вакансии, и явно показать их в резюме."

	if missing_skills:
		missing_part = f"Отдельно закрыть пробелы: {', '.join(missing_skills[:4])}."
	else:
		missing_part = "Критичных пробелов по целевым навыкам не видно."

	if concerns:
		concern_part = f"Снять риски: {', '.join(concerns[:3])}."
	else:
		concern_part = "Проверить детали вакансии и уточнить требования у рекрутера."

	return f"{skill_part} {missing_part} {concern_part}"


def _build_application_step(vacancy: dict[str, Any]) -> str:
	priority = str(vacancy.get("application_priority") or "")
	if priority.startswith("P1"):
		return "Откликнуться первым: адаптировать резюме под совпавшие навыки и приложить короткое сопроводительное письмо."
	if priority.startswith("P2"):
		return "Откликнуться после проверки требований: подсветить совпавшую роль, стек и формат работы."
	if priority.startswith("P3"):
		return "Проверить описание вручную и уточнить спорные требования перед откликом."
	return "Не тратить первый слот откликов; вернуться, если появятся дополнительные аргументы в пользу вакансии."


def _build_questions(vacancy: dict[str, Any]) -> list[str]:
	questions = []
	if not vacancy.get("url") and not vacancy.get("link"):
		questions.append("Где опубликована вакансия и есть ли официальная ссылка?")
	if vacancy.get("concerns"):
		questions.append("Можно ли уточнить требования, которые создают риск для кандидата?")
	if not vacancy.get("salary") and not vacancy.get("salary_rub"):
		questions.append("Какая зарплатная вилка и условия стажировки?")
	return questions or ["Какие задачи будут в первые 2-4 недели и как оценивается успешность кандидата?"]


def _apply_llm_explanations(
	explanations: list[dict[str, Any]],
	top_vacancies: list[dict[str, Any]],
	criteria: dict[str, Any],
	llm_client: Any | None,
	*,
	progress_callback: Any | None = None,
) -> None:
	if not getattr(llm_client, "enabled", False):
		return
	if not top_vacancies:
		return

	batches = [
		(offset, top_vacancies[offset : offset + LLM_BATCH_SIZE])
		for offset in range(0, len(top_vacancies), LLM_BATCH_SIZE)
	]
	sent_items = sum(len(batch) for _, batch in batches)
	completed_items = 0
	if progress_callback:
		progress_callback(completed_items, len(top_vacancies), f"отправлено: {sent_items}/{len(top_vacancies)}, ответы: 0/{len(top_vacancies)}")

	def request_batch(offset: int, batch: list[dict[str, Any]]) -> dict[str, Any]:
		return llm_client.json_task(
			stage="agent_explanation",
			system_prompt=(
				"You are a job-search decision assistant. Return only valid JSON matching expected_json_shape. "
				"Explain top vacancies using candidate criteria, score evidence, matched/missing skills, rank comments and vacancy text. "
				"Do not invent facts, do not change score, do not change rank, and do not omit input vacancies. "
				"Write concrete risks even when rule-based risks are empty: missing salary, unclear stack, vague level, unknown format, weak source data or mismatch with candidate criteria. "
				"Keep llm_comment as a factual role summary, not a risk list and not a duplicate of structured fields. "
				"Never start llm_comment by repeating the vacancy title, company, city, salary, work format, English level or seniority already shown in structured fields. "
				"Ignore job-board UI metadata in vacancy text: 'Сейчас смотрят', payment frequency, salary fragments like 'за месяц, на руки', experience counters, ratings, metro snippets and view counts. "
				"Treat canonical values as semantically stable across spelling variants, transliteration, capitalization and case: cities, work formats, seniority labels, company names, technologies and similar normalized fields should not become risks just because they are written differently. "
				"Never add a risk that contradicts positive score_breakdown evidence: if level_match is positive, do not say Middle/Senior is below expectations; if work_format or city is positive, do not mark that same field as a mismatch. "
				"Keep risks, missing data, stop words and warnings out of llm_comment; put them only into risks."
			),
			payload=_build_explanation_payload(criteria, batch, offset),
		)

	deadline_seconds = _llm_explanation_deadline_seconds()
	deadline_at = time.monotonic() + deadline_seconds
	executor = ThreadPoolExecutor(max_workers=min(LLM_MAX_WORKERS, len(batches)))
	futures = {executor.submit(request_batch, offset, batch): batch for offset, batch in batches}
	pending = set(futures)
	try:
		while pending:
			remaining = deadline_at - time.monotonic()
			if remaining <= 0:
				break
			done, pending = wait(pending, timeout=min(0.5, remaining), return_when=FIRST_COMPLETED)
			for future in done:
				batch = futures[future]
				try:
					result = future.result()
				except Exception as error:  # noqa: BLE001 - one failed LLM batch must not block all cards.
					_record_agent_explanation_trace(llm_client, False, f"{type(error).__name__}: {error}")
					result = {}
				_apply_explanation_result(result, explanations, top_vacancies)
				completed_items += len(batch)
				if progress_callback:
					progress_callback(
						completed_items,
						len(top_vacancies),
						f"отправлено: {sent_items}/{len(top_vacancies)}, ответы: {completed_items}/{len(top_vacancies)}",
					)
		if pending:
			timed_out_items = sum(len(futures[future]) for future in pending)
			for future in pending:
				future.cancel()
			completed_items += timed_out_items
			_record_agent_explanation_trace(
				llm_client,
				False,
				f"agent_explanation deadline exceeded after {deadline_seconds:g}s; timed out items: {timed_out_items}",
			)
			if progress_callback:
				progress_callback(
					min(completed_items, len(top_vacancies)),
					len(top_vacancies),
					f"отправлено: {sent_items}/{len(top_vacancies)}, ответы: {min(completed_items, len(top_vacancies))}/{len(top_vacancies)}, таймаут: {timed_out_items}",
				)
	finally:
		executor.shutdown(wait=False, cancel_futures=True)


def _llm_explanation_deadline_seconds() -> float:
	raw = os.environ.get("AGENT_EXPLANATION_DEADLINE_SECONDS", "")
	try:
		value = float(raw)
	except (TypeError, ValueError):
		value = LLM_EXPLANATION_DEADLINE_SECONDS
	return max(0.1, value)


def _record_agent_explanation_trace(llm_client: Any, ok: bool, reason: str) -> None:
	trace = {
		"stage": "agent_explanation",
		"mode": getattr(llm_client, "mode", ""),
		"model": getattr(llm_client, "model", ""),
		"base_url": getattr(llm_client, "base_url", ""),
		"ok": ok,
		"reason": reason,
	}
	append_trace = getattr(llm_client, "_append_trace", None)
	if callable(append_trace):
		append_trace(trace)
		return
	call_trace = getattr(llm_client, "call_trace", None)
	if isinstance(call_trace, list):
		call_trace.append(trace)


def _build_explanation_payload(criteria: dict[str, Any], batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
	return {
		"candidate_criteria": criteria,
		"instruction": (
			f"Improve explanations for {len(batch)} vacancies. Keep the rule-based score and ranking unchanged. "
			"Return practical Russian text for a candidate deciding where to apply. "
			"Keep why_fit, what_to_improve and next_step concise: 1-2 sentences. Keep list fields to 1-3 items. "
			"Make llm_comment a useful vacancy description in 4-5 non-repeating sentences. "
			"Use llm_comment for responsibilities, product/domain context, workflow, expected analysis tasks, tools mentioned in vacancy text, and what the candidate will likely do day to day. "
			"Use only facts present in the vacancy text; do not invent salary, city, company, format, English level, responsibilities or requirements. "
			"Do not duplicate structured parameters such as vacancy title, company, salary, location, format, English level, seniority, match score or risks inside llm_comment. "
			"Do not write generic openings like 'Вакансия ...' or repeat the top of the card; start with the work content instead. "
			"Ignore job-board UI metadata in vacancy text: 'Сейчас смотрят', payment frequency, salary fragments like 'за месяц, на руки', experience counters, ratings, metro snippets and view counts. "
			"Treat canonical values as semantically stable across spelling variants, transliteration, capitalization and case: cities, work formats, seniority labels, company names, technologies and similar normalized fields should not become risks just because they are written differently. "
			"Never add a risk that contradicts positive score_breakdown evidence: if level_match is positive, do not say Middle/Senior is below expectations; if work_format or city is positive, do not mark that same field as a mismatch. "
			"Do not mention stop words, missing salary, missing format, missing English, uncertain seniority, unknown schedule or apply/no-resume mechanics inside llm_comment. These belong only in risks or questions_to_employer. "
			"Generate the risks field yourself from score evidence, vacancy text and missing data; do not only copy existing rule-based concerns. "
			"Return one explanation for every input vacancy and keep vacancy_id unchanged."
		),
		"top_vacancies": [
			{
				"priority": offset + index,
				"vacancy_id": _vacancy_key(vacancy),
				"title": vacancy.get("title") or vacancy.get("normalized_title"),
				"company": vacancy.get("company"),
				"score": vacancy.get("score"),
				"llm_adjustment": vacancy.get("llm_adjustment"),
				"llm_score_comment": vacancy.get("llm_comment"),
				"llm_rank_comment": vacancy.get("llm_rank_comment"),
				"llm_score_risks": vacancy.get("llm_score_risks", []),
				"score_breakdown": vacancy.get("score_breakdown", []),
				"matched_skills": vacancy.get("matched_skills", []),
				"missing_target_skills": vacancy.get("missing_target_skills", []),
				"risks": vacancy.get("concerns", []),
				"description": _clean_llm_source_text(vacancy.get("description"), limit=500),
			}
			for index, vacancy in enumerate(batch, start=1)
		],
		"batch": {"offset": offset, "size": len(batch)},
		"expected_json_shape": {
			"explanations": [
				{
					"vacancy_id": "string",
					"why_fit": "Russian text",
					"risks": ["Russian risk"],
					"what_to_improve": "Russian text",
					"next_step": "Russian text",
					"questions_to_employer": ["Russian question"],
					"llm_comment": "Russian role description, 4-5 sentences, no invented facts and no duplicated structured parameters",
				}
			]
		},
	}


def _apply_explanation_result(
	result: dict[str, Any],
	explanations: list[dict[str, Any]],
	top_vacancies: list[dict[str, Any]],
) -> None:
	if not isinstance(result, dict):
		return
	items = result.get("explanations")
	if not isinstance(items, list):
		return

	by_id = {_vacancy_key(vacancy): index for index, vacancy in enumerate(top_vacancies)}
	for item in items:
		if not isinstance(item, dict):
			continue
		index = by_id.get(str(item.get("vacancy_id") or "").strip())
		if index is None:
			continue
		explanation = explanations[index]
		for field in ("why_fit", "what_to_improve", "next_step", "llm_comment"):
			value = str(item.get(field) or "").strip()
			if value:
				if field == "llm_comment":
					value = _clean_llm_source_text(value, limit=1200)
				explanation[field] = value
				if field == "llm_comment":
					top_vacancies[index]["llm_explanation_comment"] = value
		for field in ("risks", "questions_to_employer"):
			value = item.get(field)
			if isinstance(value, list) and value:
				cleaned = [
					part
					for part in _dedupe_text_list(value)
					if not _is_noise_risk(part) and not _risk_contradicts_positive_score(part, top_vacancies[index])
				][:5]
				explanation[field] = cleaned
				if field == "risks":
					top_vacancies[index]["llm_risks"] = cleaned
		explanation["llm_used"] = True


def _vacancy_key(vacancy: dict[str, Any]) -> str:
	return str(vacancy.get("vacancy_id") or vacancy.get("url") or vacancy.get("title") or "").strip()
