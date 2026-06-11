from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any


SCORING_TABLE = [
	("Совпадает роль", "+30 в auto; LLM-score в auto + LLM"),
	("Подходит уровень Internship / Junior / Entry", "+20"),
	("Совпадают навыки", "до +25 в auto; LLM-score в auto + LLM"),
	("Подходит формат работы", "+10"),
	("Подходит город / удаленка", "+10"),
	("Явно неверный город / удаленка вне предпочтений", "-30"),
	("Зарплата не ниже минимума", "+5"),
	("Зарплата не указана при включенном штрафе", "до -16"),
	("Английский подходит", "+5"),
	("Вакансия свежая", "+5"),
	("Уровень вне предпочтений", "-30; до -80 при сильном расхождении"),
	("Senior / Lead / Middle", "-40"),
	("Нет ссылки", "-5"),
	("Нерелевантная роль / вне целевых ролей", "-80"),
]


def _format_mapping(mapping: dict[str, Any]) -> str:
	lines = []
	for key, value in mapping.items():
		lines.append(f"- {key}: {value}")
	return "\n".join(lines)


def _format_list(values: Any, *, fallback: str = "нет") -> str:
	if isinstance(values, list):
		cleaned = [str(value).strip() for value in values if str(value).strip()]
	else:
		cleaned = [str(values).strip()] if str(values or "").strip() else []
	return ", ".join(cleaned) if cleaned else fallback


def _display_or_null(value: Any) -> str:
	text = str(value or "").strip()
	return text if text else "null"


def _llm_comment_for_vacancy(vacancy: dict[str, Any]) -> str:
	return (
		str(vacancy.get("llm_explanation_comment") or "").strip()
		or str(vacancy.get("llm_comment") or "").strip()
		or str(vacancy.get("llm_rank_comment") or "").strip()
		or "не использовался"
	)


def _risks_for_vacancy(vacancy: dict[str, Any]) -> str:
	return _format_list(
		vacancy.get("llm_risks") or vacancy.get("llm_score_risks") or vacancy.get("concerns", []),
		fallback="нет",
	)


def generate_outputs(
	output_dir: str | Path,
	*,
	data_summary: dict[str, Any],
	validation_report: Any,
	scoring_result: Any,
	agent_output: Any,
	trace_context: dict[str, Any],
) -> dict[str, Path]:
	output_path = Path(output_dir)
	output_path.mkdir(parents=True, exist_ok=True)

	report_path = output_path / "report.md"
	methodology_path = output_path / "methodology.md"
	run_log_path = output_path / "run.log"
	trace_path = output_path / "trace.json"

	report_path.write_text(
		_build_report(data_summary, validation_report, scoring_result, agent_output, trace_context),
		encoding="utf-8",
	)
	methodology_path.write_text(
		_build_methodology_report(data_summary, validation_report),
		encoding="utf-8",
	)
	run_log_path.write_text(_build_log(data_summary, validation_report, scoring_result, agent_output), encoding="utf-8")
	trace_payload = {
		"data_summary": data_summary,
		"validation_report": asdict(validation_report),
		"trace_context": trace_context,
		"run_mode": data_summary.get("run_mode", "dry_run"),
		"llm_used": data_summary.get("llm_used", False),
		"llm_trace": trace_context.get("llm_trace", []),
		"scoring_trace": scoring_result.trace,
		"card_vacancies": agent_output.top_vacancies,
		"agent_explanations": agent_output.explanations,
		"generated_at": datetime.now().isoformat(timespec="seconds"),
	}
	trace_path.write_text(json.dumps(trace_payload, ensure_ascii=False, indent=2), encoding="utf-8")

	return {"report": report_path, "methodology": methodology_path, "log": run_log_path, "trace": trace_path}


def _build_methodology_report(
	data_summary: dict[str, Any],
	validation_report: Any,
) -> str:
	lines = [
		"# Vacancy Match Agent Report",
		"",
		"## 1. Цель агента",
		"Автоматически сопоставить стажировки и junior-вакансии с профилем кандидата, ранжировать их и объяснить выбор.",
		"",
		"## 2. Входные данные",
		_format_mapping(data_summary),
		"",
		"LLM-режим:",
		f"- run_mode: {data_summary.get('run_mode', 'dry_run')}",
		f"- llm_used: {data_summary.get('llm_used', False)}",
		f"- llm_model: {data_summary.get('llm_model') or 'не используется'}",
		"",
		"## 3. Проверка качества данных",
		f"- Всего вакансий: {validation_report.total_vacancies}",
		f"- Валидных после проверки: {validation_report.valid_vacancies}",
		f"- Дубликаты: {validation_report.duplicates}",
		f"- Пустые поля: {validation_report.empty_fields}",
		f"- Вакансии без ссылки: {validation_report.missing_links}",
		f"- Битые строки: {getattr(validation_report, 'broken_rows', 0)}",
	]

	if validation_report.warnings:
		lines.extend(["", "Предупреждения:"])
		for warning in validation_report.warnings[:12]:
			lines.append(f"- {warning}")
		if len(validation_report.warnings) > 12:
			lines.append(f"- ... еще {len(validation_report.warnings) - 12} предупреждений см. в output/trace.json")

	lines.extend(
		[
			"",
			"## 4. Метод scoring",
			"В auto score считается детерминированно по фиксированным весам. В auto + LLM модель выставляет часть score за соответствие целевым ролям и навыкам, а остальные критерии остаются автоматическими. `llm_adjustment`, `llm_comment`, ранговое обоснование и LLM-риски сохраняются отдельно.",
			"",
			"| Критерий | Баллы |",
			"|---|---:|",
		]
	)
	for criterion, points in SCORING_TABLE:
		lines.append(f"| {criterion} | {points} |")

	return "\n".join(lines) + "\n"


def _build_report(
	data_summary: dict[str, Any],
	validation_report: Any,
	scoring_result: Any,
	agent_output: Any,
	trace_context: dict[str, Any],
) -> str:
	top_vacancies = agent_output.top_vacancies
	top_k = int(data_summary.get("top_k") or len(top_vacancies) or 5)
	lines = [f"## 5. Топ-{top_k} вакансий"]
	if top_vacancies:
		for index, vacancy in enumerate(top_vacancies, start=1):
			lines.extend(
				[
					f"### {index}. {vacancy.get('title') or vacancy.get('normalized_title') or 'Без названия'}",
					f"- Компания: {vacancy.get('company') or 'не указана'}",
					f"- Score: {vacancy.get('score', 0)}",
					f"- Приоритет: {vacancy.get('application_priority') or index}",
					f"- Формат / город: {_display_or_null(vacancy.get('work_format'))} / {_display_or_null(vacancy.get('location'))}",
					f"- Зарплата: {vacancy.get('salary') or vacancy.get('salary_rub') or 'не указана'}",
					f"- Ссылка: {vacancy.get('url') or vacancy.get('link') or 'нет'}",
					f"- Совпавшие навыки: {_format_list(vacancy.get('matched_skills', []))}",
					f"- Причины: {_format_list(vacancy.get('reasons', []))}",
					f"- Риски: {_risks_for_vacancy(vacancy)}",
					f"- LLM-комментарий: {_llm_comment_for_vacancy(vacancy)}",
				]
			)
	else:
		lines.append("Входных вакансий нет, поэтому ранжирование не выполнено.")

	lines.extend(["", "## 6. Причины выбора"])
	if agent_output.explanations:
		for item in agent_output.explanations:
			lines.extend(
				[
					f"### {item['priority']}. {item['title']}",
					f"- Почему подходит: {item['why_fit']}",
					f"- Совпавшие критерии: {', '.join(item['matched_criteria']) or 'нет'}",
					f"- Извлеченные требования: {', '.join(item['extracted_requirements']) or 'нет'}",
					f"- Что смущает: {', '.join(item['risks'])}",
					f"- Что подтянуть: {item['what_to_improve']}",
					f"- Следующий шаг: {item['next_step']}",
					f"- Приоритет отклика: {item['application_priority']}",
					f"- Вопрос работодателю: {', '.join(item['questions_to_employer'])}",
					f"- LLM использовался: {item.get('llm_used', False)}",
					f"- LLM-комментарий: {item.get('llm_comment') or 'нет'}",
				]
			)
	else:
		lines.append("Объяснения не сформированы из-за отсутствия вакансий.")

	lines.extend(
		[
			"",
			"## 7. Риски",
			"Пайплайн опирается на структуру входных полей и rule-based сопоставление, поэтому спорные вакансии нужно проверить вручную.",
			"",
			"Примеры причин низкого ранга:",
		]
	)
	for vacancy in scoring_result.scored_vacancies[-5:]:
		lines.append(
			f"- {vacancy.get('title') or vacancy.get('normalized_title') or 'Без названия'}: "
			f"score {vacancy.get('score', 0)}, риски: {', '.join(vacancy.get('concerns', [])) or 'нет'}"
		)

	lines.extend(
		[
			"",
			"## 8. Trace",
			f"Scored vacancies: {len(scoring_result.scored_vacancies)}",
			f"Trace entries: {len(scoring_result.trace)}",
			f"Run mode: {data_summary.get('run_mode', 'dry_run')}",
			f"Top-K explanations: {top_k}",
			f"LLM calls: {len(trace_context.get('llm_trace', []))}",
			"Подробная трассировка сохранена в `output/trace.json`, краткий лог запуска - в `output/run.log`.",
			"",
			"## 9. Limitations",
			"LLM вызывается опционально при наличии API-ключа и запуске без `--dry-run`. Без ключа проект работает в воспроизводимом dry-run режиме. Базовый score не зависит от LLM, поэтому спорные LLM-комментарии можно проверить по `score_breakdown`.",
		]
	)
	return "\n".join(lines) + "\n"


def _build_log(data_summary: dict[str, Any], validation_report: Any, scoring_result: Any, agent_output: Any) -> str:
	return "\n".join(
		[
			f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
			"Pipeline completed successfully.",
			f"Vacancies loaded: {data_summary.get('vacancies_count', 0)}",
			f"Criteria loaded: {data_summary.get('criteria_count', 0)}",
			f"Run mode: {data_summary.get('run_mode', 'dry_run')}",
			f"LLM used: {data_summary.get('llm_used', False)}",
			f"LLM model: {data_summary.get('llm_model') or 'none'}",
			f"Valid vacancies: {validation_report.valid_vacancies}",
			f"Duplicates: {validation_report.duplicates}",
			f"Broken rows: {getattr(validation_report, 'broken_rows', 0)}",
			f"Missing links: {validation_report.missing_links}",
			f"Scored vacancies: {len(scoring_result.scored_vacancies)}",
			f"Top explanations: {len(agent_output.explanations)}",
		]
	) + "\n"
