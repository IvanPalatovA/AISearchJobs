from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Any


ROLE_POSITIVE_WORDS = [
	"developer",
	"engineer",
	"analyst",
	"data",
	"python",
	"backend",
	"frontend",
	"qa",
	"ml",
	"it",
	"разработчик",
	"аналитик",
]
ROLE_NEGATIVE_WORDS = ["senior", "lead", "middle", "руководитель", "team lead"]
LEVEL_MATCH_WORDS = ["intern", "internship", "junior", "entry", "trainee", "стажер", "стажёр", "стажировка"]
ROLE_ALIASES = {
	"ai": {"ai", "ии", "искусственный интеллект", "artificial intelligence"},
	"nlp": {"nlp", "natural language processing", "обработка естественного языка"},
	"llm": {"llm", "large language model", "языковая модель", "языковые модели"},
	"agent": {"agent", "ai agent", "ai-agent", "агент", "агенты"},
	"cd": {"cd", "continuous delivery", "continuous deployment"},
	"frontend": {"frontend", "front", "фронтенд", "фронтэнд"},
	"backend": {"backend", "back", "бэкенд", "бекенд"},
	"developer": {"developer", "engineer", "разработчик", "программист"},
	"analyst": {"analyst", "аналитик"},
	"qa": {"qa", "quality", "тестировщик", "тестирование"},
	"data": {"data", "данные", "данных"},
}
GENERIC_ROLE_ALIASES = {"developer", "analyst", "qa", "data"}
GENERIC_ROLE_TOKENS = {
	"developer",
	"engineer",
	"разработчик",
	"программист",
	"specialist",
	"специалист",
	"intern",
	"internship",
	"стажер",
	"стажерка",
	"стажировка",
	"junior",
	"middle",
	"senior",
	"lead",
}
ENGLISH_LEVELS = {
	"a0": 0,
	"a1": 1,
	"a2": 2,
	"a2+": 3,
	"b1": 4,
	"b2": 5,
	"c1": 6,
	"c2": 7,
}
SCORE_WEIGHTS = {
	"role_match": 30,
	"level_match": 20,
	"skills_match_max": 25,
	"work_format": 10,
	"city": 10,
	"city_mismatch": -30,
	"salary": 5,
	"english": 5,
	"fresh": 5,
	"level_mismatch": -30,
	"level_far_mismatch": -80,
	"senior_lead_middle": -40,
	"missing_link": -5,
	"irrelevant_role": -80,
	"target_role_mismatch": -80,
}
DEFAULT_IMPORTANCE = {
	"target_roles": "high",
	"preferred_levels": "high",
	"skills": "high",
	"preferred_formats": "medium",
	"preferred_cities": "medium",
	"min_salary": "low",
	"english_level": "low",
	"stop_words": "low",
}
IMPORTANCE_MULTIPLIERS = {"low": 0.7, "medium": 1.0, "high": 1.35}
BOUNDARY_PENALTIES = {"low": 3, "medium": 7, "high": 12}
SALARY_BOUNDARY_PENALTIES = {"low": 4, "medium": 9, "high": 16}
STOP_WORD_PENALTIES = {"low": 5, "medium": 10, "high": 18}
LLM_BATCH_SIZE = 5
LLM_MAX_WORKERS = 6
TECH_SKILL_PATTERNS = [
	("JavaScript", r"\bjavascript\b|\bjs\b"),
	("TypeScript", r"\btypescript\b|\bts\b"),
	("React", r"\breact\b"),
	("React Hooks", r"\bhooks?\b|react\s+hooks?"),
	("React Router", r"\brouter\b|react\s+router"),
	("React Effector", r"\beffector\b|react[-\s]?effector"),
	("RxJS", r"\brxjs\b"),
	("GeoJSON", r"\bgeojson\b"),
	("OpenLayers", r"\bopenlayers\b|open\s+layers"),
	("Next.js", r"\bnext\.?js\b"),
	("Redux", r"\bredux\b"),
	("MobX", r"\bmobx\b"),
	("Vue", r"\bvue\b"),
	("Angular", r"\bangular\b"),
	("HTML", r"\bhtml\b"),
	("CSS", r"\bcss\b"),
	("Sass", r"\bsass\b|\bscss\b"),
	("Webpack", r"\bwebpack\b"),
	("Vite", r"\bvite\b"),
	("Jest", r"\bjest\b"),
	("Cypress", r"\bcypress\b"),
	("Playwright", r"\bplaywright\b"),
	("REST API", r"\brest(?:\s+api)?\b"),
	("GraphQL", r"\bgraphql\b"),
	("Node.js", r"\bnode\.?js\b"),
]
LEVEL_ORDER = {
	"internship": 0,
	"entry": 0,
	"junior": 1,
	"middle": 2,
	"senior": 3,
	"lead": 4,
}
KNOWN_CITY_ALIASES = {
	"москва": {"москва", "moscow", "мск"},
	"санкт петербург": {"санкт петербург", "санкт-петербург", "спб", "питер", "saint petersburg", "st petersburg"},
	"казань": {"казань", "kazan"},
	"екатеринбург": {"екатеринбург", "екб", "yekaterinburg"},
	"новосибирск": {"новосибирск", "novosibirsk"},
	"нижний новгород": {"нижний новгород", "nizhny novgorod"},
	"челябинск": {"челябинск", "chelyabinsk"},
}


@dataclass(slots=True)
class ScoringResult:
	scored_vacancies: list[dict[str, Any]]
	trace: list[dict[str, Any]] = field(default_factory=list)
	llm_trace: list[dict[str, Any]] = field(default_factory=list)


def _as_text(value: Any) -> str:
	return str(value or "").strip()


def _compact_evidence(value: Any, *, limit: int = 120) -> Any:
	if isinstance(value, list):
		items = [_compact_evidence(item, limit=max(24, limit // 2)) for item in value[:3]]
		return [item for item in items if _as_text(item)]
	if isinstance(value, dict):
		return {key: _compact_evidence(item, limit=max(24, limit // 2)) for key, item in list(value.items())[:3]}

	text = _as_text(value)
	if not text:
		return text

	text = re.sub(r"\s+", " ", text).strip()
	if len(text) <= limit:
		return text

	cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
	return f"{cut}…" if cut else f"{text[:limit].rstrip()}…"


def _normalize_match_text(value: Any) -> str:
	text = _as_text(value).lower().replace("ё", "е")
	return re.sub(r"[^a-zа-я0-9+.#]+", " ", text).strip()


def _dedupe_text_list(values: Any) -> list[str]:
	result: list[str] = []
	seen: set[str] = set()
	for value in values or []:
		text = _as_text(value)
		key = _normalize_match_text(text)
		if text and key and key not in seen:
			seen.add(key)
			result.append(text)
	return result


def _clean_llm_source_text(value: Any, *, limit: int) -> str:
	text = _as_text(value)
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
	text = _normalize_match_text(value)
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
	text = _normalize_match_text(value)
	if not text:
		return True
	positive = {
		str(item.get("criterion") or "")
		for item in vacancy.get("score_breakdown", [])
		if isinstance(item, dict) and _safe_int(item.get("points"), default=0, minimum=-10_000, maximum=10_000) > 0
	}
	if "level_match" in positive and re.search(r"(уров|seniority|middle|senior|мидл|сеньор|junior|джун).*(ниже|не подход|вне|ожидан|недостат)|ниже ожиданий.*(мидл|сеньор|middle|senior)", text):
		return True
	if "work_format" in positive and re.search(r"(формат|офис|office|onsite).*(не подход|вне|не совпад|отсутств|не указан)", text):
		return True
	if "city" in positive and re.search(r"(город|локац|москва|moscow).*(не подход|вне|не совпад|отсутств|не указан)", text):
		return True
	return False


def _tokens(text: str) -> set[str]:
	return {token for token in re.split(r"[^a-zA-Zа-яА-Я0-9+.#]+", text.lower().replace("ё", "е")) if token}


def _role_alias_tokens(value: Any) -> set[str]:
	text = _normalize_match_text(value)
	tokens = set(_tokens(text))
	aliases: set[str] = set()
	for canonical, variants in ROLE_ALIASES.items():
		if any(_normalize_match_text(variant) in text or variant in tokens for variant in variants):
			aliases.add(canonical)
	return aliases


def _role_matches(target_role: str, role_text: str, role_tokens: set[str], role_aliases: set[str]) -> bool:
	if _phrase_matches(target_role, role_text, role_tokens):
		return True
	target_aliases = _role_alias_tokens(target_role)
	if not target_aliases or not target_aliases <= role_aliases:
		return False
	if target_aliases - GENERIC_ROLE_ALIASES:
		return True
	meaningful_tokens = _meaningful_role_tokens(target_role)
	return not meaningful_tokens or bool(meaningful_tokens & role_tokens)


def _meaningful_role_tokens(value: Any) -> set[str]:
	return {
		token
		for token in _tokens(_normalize_match_text(value))
		if token not in GENERIC_ROLE_TOKENS and len(token) > 1
	}


def _matched_target_roles(target_roles: list[str], vacancy: dict[str, Any], *, include_description: bool = False) -> list[str]:
	title = _as_text(vacancy.get("title") or vacancy.get("normalized_title"))
	role = _as_text(vacancy.get("role") or title)
	parts = [title, role]
	if include_description:
		parts.extend(
			_as_text(vacancy.get(key))
			for key in ("description", "requirements", "responsibilities")
			if _as_text(vacancy.get(key))
		)
	role_text_raw = " ".join(parts)
	role_text = _normalize_match_text(role_text_raw)
	role_tokens = _tokens(f"{title} {role}")
	role_aliases = _role_alias_tokens(role_text_raw)
	return [target_role for target_role in target_roles if _role_matches(target_role, role_text, role_tokens, role_aliases)]


def _level_markers(value: Any) -> set[str]:
	text = _normalize_match_text(value)
	markers: set[str] = set()
	if any(word in text for word in ("internship", "intern", "trainee", "стажировка", "стажер", "стажёр")):
		markers.add("internship")
	if any(word in text for word in ("entry", "без опыта", "no experience")):
		markers.add("entry")
	if any(word in text for word in ("junior", "джуниор", "младший")):
		markers.add("junior")
	if any(word in text for word in ("middle", "мидл")):
		markers.add("middle")
	if any(word in text for word in ("senior", "сеньор", "старший")):
		markers.add("senior")
	if any(word in text for word in ("lead", "team lead", "лид", "тимлид", "руководитель")):
		markers.add("lead")
	return markers


def _declared_level_markers(vacancy: dict[str, Any]) -> set[str]:
	primary = " ".join(
		_as_text(vacancy.get(key))
		for key in ("level", "experience", "experience_level", "title", "role", "normalized_title")
		if _as_text(vacancy.get(key))
	)
	markers = _level_markers(primary)
	if markers:
		return markers
	fallback = " ".join(
		_as_text(vacancy.get(key))
		for key in ("requirements", "conditions")
		if _as_text(vacancy.get(key))
	)
	return _level_markers(fallback)


def _level_distance(target_markers: set[str], vacancy_markers: set[str]) -> int | None:
	distances: list[int] = []
	for target in target_markers:
		target_rank = LEVEL_ORDER.get(target)
		if target_rank is None:
			continue
		for marker in vacancy_markers:
			marker_rank = LEVEL_ORDER.get(marker)
			if marker_rank is not None:
				distances.append(abs(marker_rank - target_rank))
	return min(distances) if distances else None


def _split_values(value: Any) -> list[str]:
	if value is None:
		return []
	if isinstance(value, (list, tuple, set)):
		values: list[str] = []
		for item in value:
			values.extend(_split_values(item))
		return values
	return [part.strip() for part in re.split(r"[;|,\n]+", str(value)) if part.strip()]


def _first(criteria: dict[str, Any], *keys: str) -> Any:
	for key in keys:
		value = criteria.get(key)
		if _as_text(value):
			return value
	return ""


def _parse_salary(value: str) -> int | None:
	numbers = re.findall(r"\d+", value.replace(" ", ""))
	if not numbers:
		return None
	return max(int(number) for number in numbers)


def _parse_date(value: str) -> datetime | None:
	text = value.strip()
	if not text:
		return None

	for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y"):
		try:
			return datetime.strptime(text[:10], fmt).replace(tzinfo=timezone.utc)
		except ValueError:
			continue
	return None


def _phrase_matches(phrase: str, text: str, text_tokens: set[str]) -> bool:
	normalized_phrase = _normalize_match_text(phrase)
	if not normalized_phrase:
		return False
	if normalized_phrase in text:
		return True

	phrase_tokens = _tokens(phrase)
	if not phrase_tokens:
		return False
	if len(phrase_tokens) == 1:
		return next(iter(phrase_tokens)) in text_tokens
	return phrase_tokens <= text_tokens


def _format_markers(value: str) -> set[str]:
	text = _normalize_match_text(value)
	markers: set[str] = set()
	if "remote" in text or "удален" in text or "удаленно" in text:
		markers.add("remote")
	if "hybrid" in text or "гибрид" in text:
		markers.add("hybrid")
	if "onsite" in text or "office" in text or "офис" in text:
		markers.add("onsite")
	return markers or ({text} if text else set())


def _canonical_city(value: Any) -> str:
	text = _normalize_match_text(value).replace("-", " ")
	if not text:
		return ""
	for canonical, aliases in KNOWN_CITY_ALIASES.items():
		if any(re.search(rf"(?<![a-zа-я0-9]){re.escape(alias)}(?![a-zа-я0-9])", text) for alias in aliases):
			return canonical
	return ""


def _location_match_status(
	target_locations: list[str],
	vacancy_location: str,
	vacancy_format_markers: set[str],
	target_format_markers: set[str],
) -> tuple[bool | None, list[str] | str]:
	if not target_locations:
		return None, ""

	target_cities = [_canonical_city(location) for location in target_locations]
	target_cities = [city for city in target_cities if city]
	vacancy_city = _canonical_city(vacancy_location)
	normalized_location = _normalize_match_text(vacancy_location)
	for location in target_locations:
		normalized_target = _normalize_match_text(location)
		if normalized_target and normalized_target in normalized_location:
			return True, [location]
	if target_cities and vacancy_city:
		if vacancy_city in target_cities:
			return True, [vacancy_city]
		return False, vacancy_location or vacancy_city
	if "remote" in vacancy_format_markers and ("remote" in target_format_markers or any(_normalize_match_text(location) in {"удаленно", "удаленка", "remote"} for location in target_locations)):
		return True, ["remote"]
	if target_cities and not normalized_location:
		return None, "empty location"
	if target_cities and normalized_location:
		return None, vacancy_location
	return None, vacancy_location


def _english_rank(value: str) -> int | None:
	text = value.lower().replace(" ", "")
	match = re.search(r"(a1|a2\+?|b1|b2|c1|c2)", text)
	if not match:
		return None
	return ENGLISH_LEVELS.get(match.group(1))


def _parse_importance(criteria: dict[str, Any]) -> dict[str, str]:
	raw = _first(criteria, "criterion_importance", "criteria_importance", "importance", "weights")
	importance = dict(DEFAULT_IMPORTANCE)
	if isinstance(raw, dict):
		items = raw.items()
	else:
		text = _as_text(raw)
		items = []
		if text:
			items = re.findall(r"([a-zA-Z_]+)\s*[:=]\s*(low|medium|high|низк\w*|средн\w*|высок\w*)", text, flags=re.IGNORECASE)
	for key, value in items:
		normalized_key = str(key).strip().lower()
		normalized_value = _normalize_importance(value)
		if normalized_key and normalized_value:
			importance[normalized_key] = normalized_value
	return importance


def _as_bool(value: Any, *, default: bool = False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	text = _normalize_match_text(value)
	if not text:
		return default
	return text in {"1", "true", "yes", "y", "on", "да", "истина", "вкл", "включено"}


def _normalize_importance(value: Any) -> str:
	text = _normalize_match_text(value)
	if text in {"low", "red"} or text.startswith("низк") or text.startswith("крас"):
		return "low"
	if text in {"medium", "yellow"} or text.startswith("сред") or text.startswith("желт"):
		return "medium"
	if text in {"high", "green"} or text.startswith("выс") or text.startswith("важ") or text.startswith("зел"):
		return "high"
	return ""


def _weighted_points(base_points: int, importance: dict[str, str], field_key: str) -> int:
	priority = importance.get(field_key, DEFAULT_IMPORTANCE.get(field_key, "medium"))
	return round(base_points * IMPORTANCE_MULTIPLIERS.get(priority, 1.0))


def _boundary_penalty(importance: dict[str, str], field_key: str) -> int:
	priority = importance.get(field_key, DEFAULT_IMPORTANCE.get(field_key, "medium"))
	return -BOUNDARY_PENALTIES.get(priority, BOUNDARY_PENALTIES["medium"])


def _salary_penalty(target_salary: int, vacancy_salary: int, importance: dict[str, str]) -> int:
	if target_salary <= 0:
		return 0
	priority = importance.get("min_salary", DEFAULT_IMPORTANCE["min_salary"])
	max_penalty = SALARY_BOUNDARY_PENALTIES.get(priority, SALARY_BOUNDARY_PENALTIES["medium"])
	deficit_ratio = max(0, min(1, (target_salary - vacancy_salary) / target_salary))
	return -max(1, round(max_penalty * deficit_ratio))


def _priority_label(score: int) -> str:
	if score >= 90:
		return "P1 - высокий"
	if score >= 70:
		return "P2 - средний"
	if score >= 45:
		return "P3 - проверить вручную"
	return "P4 - низкий"


def _extract_requirements(vacancy: dict[str, Any]) -> list[str]:
	skills_text = " | ".join(
		_as_text(vacancy.get(key))
		for key in ("skills", "key_skills", "stack", "requirements", "tech_stack")
		if _as_text(vacancy.get(key))
	)
	return sorted({item for item in _split_values(skills_text) if item})[:10]


def _extract_vacancy_skills(vacancy: dict[str, Any]) -> list[str]:
	skills_text = " | ".join(
		_as_text(vacancy.get(key))
		for key in ("skills", "key_skills", "stack", "tech_stack", "requirements", "responsibilities", "description", "raw_detail_text")
		if _as_text(vacancy.get(key))
	)
	items = {item for item in _split_values(skills_text) if item}
	normalized = _normalize_match_text(skills_text)
	for label, pattern in TECH_SKILL_PATTERNS:
		if re.search(pattern, normalized, flags=re.IGNORECASE):
			items.add(label)
	return sorted(items, key=lambda item: item.lower())[:30]


def score_vacancies(
	vacancies: list[dict[str, Any]],
	criteria: dict[str, Any],
	llm_client: Any | None = None,
	llm_limit: int = 5,
	progress_callback: Any | None = None,
	score_only_existing: bool = False,
	llm_progress_callback: Any | None = None,
	use_llm_match_scoring: bool = False,
	apply_llm_review: bool = True,
) -> ScoringResult:
	if score_only_existing:
		scored_vacancies = list(vacancies)
		_apply_llm_score_review(scored_vacancies, criteria, llm_client, limit=llm_limit, progress_callback=llm_progress_callback)
		return ScoringResult(scored_vacancies=scored_vacancies, trace=_build_trace(scored_vacancies))

	criteria = {str(key).strip().lower(): value for key, value in criteria.items()}
	importance = _parse_importance(criteria)
	target_roles = _split_values(_first(criteria, "target_roles", "role", "target_role", "desired_role"))
	target_role_tokens = {
		token
		for role in target_roles
		for token in _tokens(role)
		if token not in {"junior", "intern", "internship", "entry", "стажер", "стажерка"}
	}
	target_skills = _split_values(_first(criteria, "skills", "skillset", "stack"))
	target_locations = _split_values(_first(criteria, "preferred_cities", "location", "city", "preferred_city"))
	target_formats = _split_values(_first(criteria, "preferred_formats", "work_format", "format", "employment_format"))
	target_levels = _split_values(_first(criteria, "preferred_levels", "level", "levels"))
	target_salary = _parse_salary(_as_text(_first(criteria, "min_salary", "salary", "expected_salary")))
	candidate_english = _as_text(_first(criteria, "english", "english_level", "language"))
	stop_words = _split_values(_first(criteria, "stop_words"))
	include_role_description = _as_bool(_first(criteria, "target_roles_use_description", "role_use_description", "use_role_description"))
	penalize_missing_salary = _as_bool(_first(criteria, "salary_missing_penalty", "penalize_missing_salary", "salary_required"))
	llm_match_scoring = bool(use_llm_match_scoring and getattr(llm_client, "enabled", False))

	scored_vacancies: list[dict[str, Any]] = []
	now = datetime.now(timezone.utc)

	total_vacancies = len(vacancies)
	progress_step = max(1, total_vacancies // 20) if total_vacancies else 1
	for index, vacancy in enumerate(vacancies, start=1):
		normalized = {str(key).strip().lower(): value for key, value in vacancy.items()}
		title = _as_text(normalized.get("title") or normalized.get("role"))
		role = _as_text(normalized.get("role") or normalized.get("title"))
		level = _as_text(normalized.get("level"))
		description = _as_text(normalized.get("description") or normalized.get("summary") or normalized.get("text"))
		requirements = _as_text(normalized.get("requirements"))
		responsibilities = _as_text(normalized.get("responsibilities"))
		conditions = _as_text(normalized.get("conditions"))
		combined_text = " ".join(
			[
				title,
				role,
				level,
				description,
				requirements,
				responsibilities,
				conditions,
				_as_text(normalized.get("skills")),
				_as_text(normalized.get("key_skills")),
				_as_text(normalized.get("stack")),
			]
		)
		normalized_combined = _normalize_match_text(combined_text)
		combined_tokens = _tokens(combined_text)
		role_context_tokens = _tokens(f"{title} {role} {description} {requirements} {responsibilities}")

		score = 0
		reasons: list[str] = []
		concerns: list[str] = []
		score_breakdown: list[dict[str, Any]] = []

		matched_roles: list[str] = []
		if not llm_match_scoring:
			matched_roles = _matched_target_roles(target_roles, normalized, include_description=include_role_description)
			if matched_roles:
				points = _weighted_points(SCORE_WEIGHTS["role_match"], importance, "target_roles")
				score += points
				reason = f"Совпадает роль: {', '.join(matched_roles[:3])}"
				reasons.append(reason)
				score_breakdown.append({"criterion": "role_match", "points": points, "evidence": _compact_evidence(matched_roles[:3])})
			elif any(word in role_context_tokens for word in ROLE_POSITIVE_WORDS):
				if target_role_tokens and not (target_role_tokens & role_context_tokens):
					points = SCORE_WEIGHTS["target_role_mismatch"]
					score += points
					concerns.append("Должность не соответствует целевым ролям")
					score_breakdown.append({"criterion": "target_role_mismatch", "points": points, "evidence": _compact_evidence(role or title)})
				else:
					reasons.append("Профиль вакансии близок к целевому")
			else:
				points = SCORE_WEIGHTS["irrelevant_role"]
				score += points
				concerns.append("Нерелевантная роль")
				score_breakdown.append({"criterion": "irrelevant_role", "points": points, "evidence": _compact_evidence(role or title)})

		level_text = _normalize_match_text(f"{title} {role} {level} {requirements} {conditions}")
		vacancy_level_markers = _declared_level_markers(normalized)
		target_level_markers = {marker for target_level in target_levels for marker in _level_markers(target_level)}
		if target_level_markers and vacancy_level_markers & target_level_markers:
			points = _weighted_points(SCORE_WEIGHTS["level_match"], importance, "preferred_levels")
			score += points
			reasons.append("Подходит уровень")
			score_breakdown.append({"criterion": "level_match", "points": points, "evidence": _compact_evidence(level or title)})
		elif target_level_markers:
			distance = _level_distance(target_level_markers, vacancy_level_markers)
			points = SCORE_WEIGHTS["level_far_mismatch"] if distance is not None and distance >= 2 else SCORE_WEIGHTS["level_mismatch"]
			score += points
			concerns.append("Уровень вне предпочтений")
			score_breakdown.append({"criterion": "level_mismatch", "points": points, "evidence": _compact_evidence(level or title)})
		elif any(negative_level in level_text for negative_level in ROLE_NEGATIVE_WORDS):
			points = SCORE_WEIGHTS["senior_lead_middle"]
			score += points
			concerns.append("Senior / Lead / Middle")
			score_breakdown.append({"criterion": "senior_lead_middle", "points": points, "evidence": _compact_evidence(level or title)})
		elif any(level_word in level_text for level_word in LEVEL_MATCH_WORDS):
			points = _weighted_points(SCORE_WEIGHTS["level_match"], importance, "preferred_levels")
			score += points
			reasons.append("Подходит уровень Internship / Junior / Entry")
			score_breakdown.append({"criterion": "level_match", "points": points, "evidence": _compact_evidence(level or title)})

		skill_matches: list[str] = []
		if not llm_match_scoring:
			skill_matches = sorted(
				{target_skill for target_skill in target_skills if _phrase_matches(target_skill, normalized_combined, combined_tokens)}
			)
			if skill_matches:
				points = _weighted_points(min(SCORE_WEIGHTS["skills_match_max"], 5 * len(skill_matches)), importance, "skills")
				score += points
				reasons.append(f"Совпадают навыки: {', '.join(skill_matches[:5])}")
				score_breakdown.append({"criterion": "skills_match", "points": points, "evidence": _compact_evidence(skill_matches[:8])})
			if target_skills and not skill_matches:
				points = _boundary_penalty(importance, "skills")
				score += points
				concerns.append("Нет совпадений по ключевым навыкам")
				score_breakdown.append({"criterion": "skills_mismatch", "points": points, "evidence": _compact_evidence(target_skills[:8])})
		missing_target_skills = [skill for skill in target_skills if skill not in skill_matches][:8]

		vacancy_format = _as_text(normalized.get("work_format") or normalized.get("format") or normalized.get("employment_type")).lower()
		target_format_markers = {marker for target_format in target_formats for marker in _format_markers(target_format)}
		vacancy_format_markers = _format_markers(vacancy_format)
		if target_format_markers and target_format_markers & vacancy_format_markers:
			points = _weighted_points(SCORE_WEIGHTS["work_format"], importance, "preferred_formats")
			score += points
			reasons.append("Подходит формат работы")
			score_breakdown.append({"criterion": "work_format", "points": points, "evidence": _compact_evidence(vacancy_format)})
		elif target_format_markers:
			points = _boundary_penalty(importance, "preferred_formats")
			score += points
			concerns.append("Формат работы вне предпочтений")
			score_breakdown.append({"criterion": "work_format_mismatch", "points": points, "evidence": _compact_evidence(vacancy_format or "empty format")})

		vacancy_location = _as_text(normalized.get("location") or normalized.get("city") or normalized.get("region")).lower()
		location_status, location_evidence = _location_match_status(target_locations, vacancy_location, vacancy_format_markers, target_format_markers)
		if location_status is True:
			points = _weighted_points(SCORE_WEIGHTS["city"], importance, "preferred_cities")
			score += points
			reasons.append("Подходит город")
			score_breakdown.append({"criterion": "city", "points": points, "evidence": _compact_evidence(location_evidence)})
		elif location_status is False:
			points = SCORE_WEIGHTS["city_mismatch"]
			score += points
			concerns.append("Город/удаленка вне предпочтений")
			score_breakdown.append({"criterion": "city_mismatch", "points": points, "evidence": _compact_evidence(vacancy_location or "empty location")})

		vacancy_salary = _parse_salary(_as_text(normalized.get("salary") or normalized.get("salary_rub") or normalized.get("compensation") or normalized.get("pay")))
		if target_salary is not None and vacancy_salary is not None and vacancy_salary >= target_salary:
			points = _weighted_points(SCORE_WEIGHTS["salary"], importance, "min_salary")
			score += points
			reasons.append("Зарплата не ниже минимума")
			score_breakdown.append({"criterion": "salary", "points": points, "evidence": _compact_evidence(vacancy_salary)})
		elif target_salary is not None and vacancy_salary is not None:
			points = _salary_penalty(target_salary, vacancy_salary, importance)
			score += points
			concerns.append("Зарплата ниже минимума")
			score_breakdown.append({"criterion": "salary_below_min", "points": points, "evidence": _compact_evidence(vacancy_salary)})
		elif target_salary is not None and penalize_missing_salary:
			points = -SALARY_BOUNDARY_PENALTIES.get(importance.get("min_salary", DEFAULT_IMPORTANCE["min_salary"]), SALARY_BOUNDARY_PENALTIES["low"])
			score += points
			concerns.append("Зарплата не указана")
			score_breakdown.append({"criterion": "salary_missing", "points": points, "evidence": _compact_evidence("empty salary")})

		vacancy_english = _as_text(normalized.get("english") or normalized.get("english_level") or normalized.get("language")).lower()
		candidate_english_rank = _english_rank(candidate_english)
		vacancy_english_rank = _english_rank(vacancy_english)
		if (
			candidate_english_rank is not None
			and vacancy_english_rank is not None
			and candidate_english_rank >= vacancy_english_rank
		):
			points = _weighted_points(SCORE_WEIGHTS["english"], importance, "english_level")
			score += points
			reasons.append("Английский подходит")
			score_breakdown.append({"criterion": "english", "points": points, "evidence": _compact_evidence(vacancy_english)})
		elif candidate_english_rank is not None and vacancy_english_rank is not None:
			points = _boundary_penalty(importance, "english_level")
			score += points
			concerns.append("Английский выше уровня кандидата")
			score_breakdown.append({"criterion": "english_mismatch", "points": points, "evidence": _compact_evidence(vacancy_english)})

		published_at = _as_text(normalized.get("published_at") or normalized.get("date") or normalized.get("created_at"))
		parsed_date = _parse_date(published_at)
		if parsed_date and now - parsed_date <= timedelta(days=14):
			points = SCORE_WEIGHTS["fresh"]
			score += points
			reasons.append("Вакансия свежая")
			score_breakdown.append({"criterion": "fresh", "points": points, "evidence": _compact_evidence(published_at)})

		url = _as_text(normalized.get("url") or normalized.get("link"))
		if not url:
			points = SCORE_WEIGHTS["missing_link"]
			score += points
			concerns.append("Нет ссылки")
			score_breakdown.append({"criterion": "missing_link", "points": points, "evidence": _compact_evidence("empty url/link")})

		for stop_word in stop_words:
			if _normalize_match_text(stop_word) and _normalize_match_text(stop_word) in normalized_combined:
				points = -STOP_WORD_PENALTIES.get(importance.get("stop_words", DEFAULT_IMPORTANCE["stop_words"]), STOP_WORD_PENALTIES["low"])
				score += points
				concerns.append(f"Стоп-слово в описании: {stop_word}")
				score_breakdown.append({"criterion": "stop_word", "points": points, "evidence": _compact_evidence(stop_word)})

		filter_reasons = normalized.get("filter_reasons")
		if isinstance(filter_reasons, list):
			concerns.extend(str(reason) for reason in filter_reasons if str(reason).strip())
		elif _as_text(filter_reasons):
			concerns.append(_as_text(filter_reasons))

		scored_vacancy = dict(vacancy)
		scored_vacancy["score"] = score
		scored_vacancy["matched_skills"] = skill_matches
		scored_vacancy["missing_target_skills"] = missing_target_skills
		scored_vacancy["matched_roles"] = matched_roles
		scored_vacancy["reasons"] = _dedupe_text_list(reasons)
		scored_vacancy["concerns"] = _dedupe_text_list(concerns)
		scored_vacancy["score_breakdown"] = score_breakdown
		scored_vacancy["extracted_requirements"] = _extract_requirements(normalized)
		scored_vacancy["vacancy_skills"] = _extract_vacancy_skills(normalized)
		scored_vacancy["application_priority"] = _priority_label(score)
		scored_vacancy["normalized_title"] = title
		scored_vacancy["_llm_match_input"] = {
			"title": title,
			"role": role,
			"description": description,
			"requirements": requirements,
			"responsibilities": responsibilities,
			"skills_text": _as_text(normalized.get("skills") or normalized.get("key_skills") or normalized.get("stack")),
		}
		scored_vacancies.append(scored_vacancy)
		if progress_callback and (index == total_vacancies or index % progress_step == 0):
			progress_callback(index, total_vacancies)

	if llm_match_scoring:
		_apply_llm_location_resolution(
			scored_vacancies,
			criteria,
			llm_client,
			importance=importance,
			target_locations=target_locations,
			target_format_markers=target_format_markers,
			progress_callback=llm_progress_callback,
		)
		_apply_llm_match_scoring(
			scored_vacancies,
			criteria,
			llm_client,
			importance=importance,
			target_roles=target_roles,
			target_skills=target_skills,
			include_role_description=include_role_description,
			progress_callback=llm_progress_callback,
		)

	for vacancy in scored_vacancies:
		vacancy.pop("_llm_match_input", None)

	scored_vacancies.sort(
		key=lambda item: (
			item.get("score", 0),
			_parse_date(_as_text(item.get("published_at"))) or datetime.min.replace(tzinfo=timezone.utc),
		),
		reverse=True,
	)
	if apply_llm_review:
		_apply_llm_score_review(scored_vacancies, criteria, llm_client, limit=llm_limit, progress_callback=llm_progress_callback)
	return ScoringResult(scored_vacancies=scored_vacancies, trace=_build_trace(scored_vacancies))


def rank_vacancies(
	scoring_result: ScoringResult,
	criteria: dict[str, Any],
	llm_client: Any | None = None,
	llm_limit: int = 5,
	llm_progress_callback: Any | None = None,
) -> list[dict[str, Any]]:
	scoring_result.scored_vacancies.sort(
		key=lambda item: (
			item.get("score", 0),
			_parse_date(_as_text(item.get("published_at"))) or datetime.min.replace(tzinfo=timezone.utc),
		),
		reverse=True,
	)
	_apply_llm_rank_review(scoring_result.scored_vacancies, criteria, llm_client, limit=llm_limit, progress_callback=llm_progress_callback)
	scoring_result.trace = _build_trace(scoring_result.scored_vacancies)
	return scoring_result.scored_vacancies


def _apply_llm_location_resolution(
	scored_vacancies: list[dict[str, Any]],
	criteria: dict[str, Any],
	llm_client: Any,
	*,
	importance: dict[str, str],
	target_locations: list[str],
	target_format_markers: set[str],
	progress_callback: Any | None = None,
) -> None:
	if not getattr(llm_client, "enabled", False) or not target_locations:
		return
	candidates = [
		vacancy
		for vacancy in scored_vacancies
		if not any(
			item.get("criterion") in {"city", "city_mismatch"}
			for item in vacancy.get("score_breakdown", [])
			if isinstance(item, dict)
		)
	]
	if not candidates:
		return

	city_points = _weighted_points(SCORE_WEIGHTS["city"], importance, "preferred_cities")
	by_id = {_vacancy_key(vacancy): vacancy for vacancy in candidates}

	def request_batch(batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
		return llm_client.json_task(
			stage="llm_location_match",
			system_prompt=(
				"You are a strict location matcher for job vacancies. Return only valid JSON matching expected_json_shape. "
				"Use only supplied target locations and vacancy fields. Do not infer hidden office locations from company knowledge."
			),
			payload={
				"candidate_criteria": {
					"preferred_cities": target_locations,
					"preferred_formats": sorted(target_format_markers),
				},
				"instruction": (
					"Decide whether each vacancy location satisfies preferred_cities. "
					"Return city_match=true only for explicit same city/region or explicit remote when remote is preferred. "
					"Return city_match=false only for explicit different city, for example Москва vs Челябинск. "
					"Return city_match='unknown' when the text is ambiguous, missing, only says hybrid without city, or cannot be resolved from the supplied fields."
				),
				"vacancies": [
					{
						"vacancy_id": _vacancy_key(vacancy),
						"title": vacancy.get("title") or vacancy.get("normalized_title") or vacancy.get("role"),
						"city": vacancy.get("city") or vacancy.get("location") or vacancy.get("region"),
						"work_format": vacancy.get("work_format") or vacancy.get("format"),
						"address": vacancy.get("address"),
						"description": _clean_llm_source_text(vacancy.get("description"), limit=350),
					}
					for vacancy in batch
				],
				"batch": {"offset": offset, "size": len(batch), "total": len(candidates)},
				"expected_json_shape": {
					"items": [
						{
							"vacancy_id": "string",
							"city_match": "true|false|unknown",
							"confidence": "high|medium|low",
							"evidence": "short Russian evidence",
						}
					]
				},
			},
		)

	def apply_result(result: dict[str, Any]) -> None:
		items = result.get("items") if isinstance(result, dict) else None
		if not isinstance(items, list):
			return
		for item in items:
			if not isinstance(item, dict):
				continue
			vacancy = by_id.get(str(item.get("vacancy_id") or "").strip())
			if not vacancy:
				continue
			confidence = _normalize_match_text(item.get("confidence"))
			if confidence not in {"high", "высокая", "высокии"}:
				continue
			raw_match = item.get("city_match")
			if isinstance(raw_match, bool):
				city_match: bool | None = raw_match
			else:
				text = _normalize_match_text(raw_match)
				city_match = True if text in {"true", "yes", "да", "match", "совпадает"} else False if text in {"false", "no", "нет", "mismatch", "не совпадает"} else None
			if city_match is None:
				continue
			evidence = _compact_evidence(item.get("evidence") or vacancy.get("city") or vacancy.get("location") or "")
			if city_match:
				vacancy["score"] = int(vacancy.get("score", 0) or 0) + city_points
				vacancy.setdefault("reasons", []).append("LLM: город соответствует предпочтениям")
				vacancy.setdefault("score_breakdown", []).append({"criterion": "city", "points": city_points, "evidence": evidence})
			else:
				vacancy["score"] = int(vacancy.get("score", 0) or 0) + SCORE_WEIGHTS["city_mismatch"]
				vacancy.setdefault("concerns", []).append("LLM: город/удаленка вне предпочтений")
				vacancy.setdefault("score_breakdown", []).append({"criterion": "city_mismatch", "points": SCORE_WEIGHTS["city_mismatch"], "evidence": evidence})
			vacancy["reasons"] = _dedupe_text_list(vacancy.get("reasons", []))
			vacancy["concerns"] = _dedupe_text_list(vacancy.get("concerns", []))

	_run_llm_batches(candidates, request_batch=request_batch, apply_items=apply_result, progress_callback=progress_callback)


def _apply_llm_match_scoring(
	scored_vacancies: list[dict[str, Any]],
	criteria: dict[str, Any],
	llm_client: Any,
	*,
	importance: dict[str, str],
	target_roles: list[str],
	target_skills: list[str],
	include_role_description: bool,
	progress_callback: Any | None = None,
) -> None:
	if not getattr(llm_client, "enabled", False):
		return
	if not scored_vacancies:
		return

	role_max = _weighted_points(SCORE_WEIGHTS["role_match"], importance, "target_roles")
	skills_max = _weighted_points(SCORE_WEIGHTS["skills_match_max"], importance, "skills")
	skills_min = _boundary_penalty(importance, "skills") if target_skills else 0
	role_min = SCORE_WEIGHTS["target_role_mismatch"] if target_roles else 0
	by_id = {_vacancy_key(vacancy): vacancy for vacancy in scored_vacancies}

	def build_payload(batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
		return {
			"candidate_criteria": {
				"target_roles": target_roles,
				"skills": target_skills,
				"target_roles_use_description": include_role_description,
			},
			"instruction": (
				"Score only target role fit and target skill fit for each vacancy. "
				"Do not score salary, city, work format, English, freshness or seniority. "
				"For role fit use only vacancy_title when target_roles_use_description is false; use vacancy_title plus vacancy_description when it is true. "
				"Do not infer a target role from technologies, frameworks or keyword overlap if the vacancy title points to a different profession, team lead, manager or other job family. "
				"Be conservative with role_points: do not give positive role_points unless the target profession is explicitly present in the title or duties. "
				"For AI/NLP/LLM/AI-agent roles, generic words like engineer, developer, intern, sales, enablement, support or customer service are not enough; AI, NLP, LLM, agent development or model-related duties must be explicit. "
				"If the title is Sales Enablement, Customer Service, support, marketing, manager, team lead or another non-AI/non-engineering job family, role_points must be negative even when AI/LLM appears in unrelated company text or skills. "
				"Skills may use vacancy skills, requirements, responsibilities and description. "
				f"role_points must be an integer from {role_min} to {role_max}. If the vacancy does not match target_roles, role_points must be negative and close to the minimum for clear mismatch. "
				f"skills_points must be an integer from {skills_min} to {skills_max}. "
				"Return grounded matched role and skill evidence only from the supplied vacancy text."
			),
			"vacancies": [
				{
					"vacancy_id": _vacancy_key(vacancy),
					"vacancy_title": vacancy.get("title") or vacancy.get("normalized_title") or vacancy.get("role"),
					"vacancy_description": _llm_role_description(vacancy) if include_role_description else "",
					"vacancy_skills_text": _as_text((vacancy.get("_llm_match_input") or {}).get("skills_text")),
					"requirements": _as_text((vacancy.get("_llm_match_input") or {}).get("requirements"))[:500],
					"responsibilities": _as_text((vacancy.get("_llm_match_input") or {}).get("responsibilities"))[:500],
					"description_for_skills": _as_text((vacancy.get("_llm_match_input") or {}).get("description"))[:700],
				}
				for vacancy in batch
			],
			"batch": {"offset": offset, "size": len(batch), "total": len(scored_vacancies)},
			"expected_json_shape": {
				"items": [
					{
						"vacancy_id": "string",
						"role_match": True,
						"role_points": f"integer {role_min}..{role_max}",
						"matched_roles": ["target role"],
						"role_comment": "short Russian evidence",
						"skills_points": f"integer {skills_min}..{skills_max}",
						"matched_skills": ["target skill"],
						"missing_target_skills": ["target skill"],
						"skills_comment": "short Russian evidence",
					}
				]
			},
		}

	def request_batch(batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
		return llm_client.json_task(
			stage="llm_match_score",
			system_prompt=(
				"You are a strict vacancy matching scorer. Return only valid JSON matching expected_json_shape. "
				"Use only supplied target roles, target skills and vacancy fields. Do not invent facts."
			),
			payload=build_payload(batch, offset),
		)

	def apply_result(result: dict[str, Any]) -> None:
		items = result.get("items") if isinstance(result, dict) else None
		if not isinstance(items, list):
			return
		for item in items:
			if not isinstance(item, dict):
				continue
			vacancy = by_id.get(str(item.get("vacancy_id") or "").strip())
			if not vacancy:
				continue
			role_match = _as_bool(item.get("role_match"), default=False)
			role_points = _safe_int(item.get("role_points"), default=role_min if target_roles else 0, minimum=role_min, maximum=role_max)
			rule_matched_roles = _matched_target_roles(target_roles, vacancy, include_description=include_role_description)
			if rule_matched_roles:
				role_match = True
				role_points = max(role_points, role_max)
			elif target_roles and role_match:
				role_match = False
				role_points = role_min
			if target_roles and not role_match:
				role_points = min(role_points, -40)
			skills_points = _safe_int(item.get("skills_points"), default=0, minimum=skills_min, maximum=skills_max)
			matched_roles = _clean_known_items(item.get("matched_roles"), target_roles)
			if rule_matched_roles and not matched_roles:
				matched_roles = rule_matched_roles[:8]
			if not role_match:
				matched_roles = []
			matched_skills = _clean_known_items(item.get("matched_skills"), target_skills)
			missing_skills = _clean_known_items(item.get("missing_target_skills"), target_skills)
			if target_skills and not missing_skills:
				missing_skills = [skill for skill in target_skills if skill not in matched_skills][:8]

			vacancy["score"] = int(vacancy.get("score", 0) or 0) + role_points + skills_points
			vacancy["matched_roles"] = matched_roles
			vacancy["matched_skills"] = matched_skills
			vacancy["missing_target_skills"] = missing_skills[:8]
			vacancy["llm_match_score_used"] = True

			role_comment = _as_text(item.get("role_comment"))
			skills_comment = _as_text(item.get("skills_comment"))
			if role_points > 0:
				vacancy.setdefault("reasons", []).append(f"LLM: должность соответствует целевым ролям{_format_llm_note(role_comment)}")
				vacancy.setdefault("score_breakdown", []).append({"criterion": "llm_role_match", "points": role_points, "evidence": _compact_evidence(matched_roles or role_comment)})
			elif target_roles:
				vacancy.setdefault("concerns", []).append(f"LLM: должность не соответствует целевым ролям{_format_llm_note(role_comment)}")
				vacancy.setdefault("score_breakdown", []).append({"criterion": "llm_role_mismatch", "points": role_points, "evidence": _compact_evidence(role_comment or target_roles[:3])})
			if skills_points > 0:
				vacancy.setdefault("reasons", []).append(f"LLM: совпадают навыки{_format_llm_note(skills_comment)}")
				vacancy.setdefault("score_breakdown", []).append({"criterion": "llm_skills_match", "points": skills_points, "evidence": _compact_evidence(matched_skills or skills_comment)})
			elif target_skills:
				vacancy.setdefault("concerns", []).append(f"LLM: слабое совпадение по навыкам{_format_llm_note(skills_comment)}")
				vacancy.setdefault("score_breakdown", []).append({"criterion": "llm_skills_mismatch", "points": skills_points, "evidence": _compact_evidence(skills_comment or target_skills[:8])})
			vacancy["reasons"] = _dedupe_text_list(vacancy.get("reasons", []))
			vacancy["concerns"] = _dedupe_text_list(vacancy.get("concerns", []))

	_run_llm_batches(scored_vacancies, request_batch=request_batch, apply_items=apply_result, progress_callback=progress_callback)


def _llm_role_description(vacancy: dict[str, Any]) -> str:
	match_input = vacancy.get("_llm_match_input") or {}
	return " ".join(
		_as_text(match_input.get(key))
		for key in ("description", "requirements", "responsibilities")
		if _as_text(match_input.get(key))
	)[:1000]


def _clean_known_items(value: Any, allowed: list[str]) -> list[str]:
	items = _split_values(value)
	if not allowed:
		return items[:8]
	allowed_by_normalized = {_normalize_match_text(item): item for item in allowed}
	cleaned: list[str] = []
	for item in items:
		key = _normalize_match_text(item)
		if key in allowed_by_normalized and allowed_by_normalized[key] not in cleaned:
			cleaned.append(allowed_by_normalized[key])
	return cleaned[:8]


def _format_llm_note(value: str) -> str:
	return f": {value}" if value else ""


def _apply_llm_score_review(
	scored_vacancies: list[dict[str, Any]],
	criteria: dict[str, Any],
	llm_client: Any | None,
	*,
	limit: int,
	progress_callback: Any | None = None,
) -> None:
	if not getattr(llm_client, "enabled", False):
		return
	target_vacancies = scored_vacancies[: max(1, min(int(limit or 5), 20))]
	by_id = {_vacancy_key(vacancy): vacancy for vacancy in scored_vacancies}

	def build_payload(batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
		return {
			"candidate_criteria": criteria,
			"instruction": (
				"Audit the rule-based score for each vacancy using only the supplied vacancy fields, score_breakdown, matched_skills, missing_target_skills and candidate_criteria. "
				"Do not change the base score and do not reorder items. Return metadata only. "
				"Set llm_adjustment as a small advisory signal from -10 to 10: positive only for clearly evidenced fit, negative for clearly evidenced mismatch or missing critical data, 0 when uncertain. "
				"Make llm_comment a factual Russian summary of the actual role and work specifics in 2-4 concise sentences. "
				"Do not invent salary, city, company, work format, English level, responsibilities or requirements; use only facts present in vacancy text. "
				"Do not duplicate structured parameters such as salary, location, company, title, work format, English level or risks inside llm_comment. "
				"Ignore job-board UI metadata in vacancy text: 'Сейчас смотрят', payment frequency, salary fragments like 'за месяц, на руки', experience counters, ratings, metro snippets and view counts. "
				"Treat canonical values as semantically stable across spelling variants, transliteration, capitalization and case: cities, work formats, seniority labels, company names, technologies and similar normalized fields should not become risks just because they are written differently. "
				"Never add a risk that contradicts positive score_breakdown evidence: if level_match is positive, do not say Middle/Senior is below expectations; if work_format or city is positive, do not mark that same field as a mismatch. "
				"Do not mention stop words, missing data, warnings or uncertainty inside llm_comment; put those only into llm_score_risks. "
				"Return one item for every input vacancy and keep vacancy_id unchanged."
			),
			"vacancies": [_compact_vacancy_for_llm(vacancy) for vacancy in batch],
			"batch": {"offset": offset, "size": len(batch), "total": len(target_vacancies)},
			"expected_json_shape": {
				"items": [
					{
						"vacancy_id": "string",
						"title": "string",
						"llm_adjustment": "integer from -10 to 10, not applied to score",
						"llm_comment": "Russian factual role description, 2-4 concise sentences, grounded in vacancy text",
						"llm_score_risks": ["specific Russian risk"],
					}
				]
			},
		}

	def request_batch(batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
		return llm_client.json_task(
			stage="calculate_score",
			system_prompt=(
				"You are a careful vacancy matching auditor. Return only valid JSON matching expected_json_shape. "
				"The rule-based score is the source of truth: never overwrite score, never reorder vacancies, never omit input items. "
				"Ground every comment and risk in provided fields; when evidence is missing, say that the data is missing instead of guessing."
			),
			payload=build_payload(batch, offset),
		)

	def apply_items(items: list[Any]) -> None:
		for item in items:
			if not isinstance(item, dict):
				continue
			vacancy = by_id.get(str(item.get("vacancy_id") or "").strip()) or by_id.get(str(item.get("title") or "").strip())
			if not vacancy:
				continue
			vacancy["llm_score_used"] = True
			vacancy["llm_adjustment"] = _safe_int(item.get("llm_adjustment"), default=0, minimum=-10, maximum=10)
			vacancy["llm_comment"] = _clean_llm_source_text(item.get("llm_comment"), limit=900)
			risks = item.get("llm_score_risks")
			if isinstance(risks, list):
				vacancy["llm_score_risks"] = [
					risk
					for risk in _dedupe_text_list(risks)
					if not _is_noise_risk(risk) and not _risk_contradicts_positive_score(risk, vacancy)
				][:5]
			else:
				vacancy["llm_score_risks"] = []

	_run_llm_batches(
		target_vacancies,
		request_batch=request_batch,
		apply_items=lambda result: apply_items(result.get("items") if isinstance(result.get("items"), list) else []),
		progress_callback=progress_callback,
	)


def _apply_llm_rank_review(
	scored_vacancies: list[dict[str, Any]],
	criteria: dict[str, Any],
	llm_client: Any | None,
	*,
	limit: int,
	progress_callback: Any | None = None,
) -> None:
	if not getattr(llm_client, "enabled", False):
		return
	target_vacancies = scored_vacancies[: max(1, min(int(limit or 5), 20))]
	by_id = {_vacancy_key(vacancy): vacancy for vacancy in scored_vacancies}

	def build_payload(batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
		return {
			"candidate_criteria": criteria,
			"instruction": (
				"Explain whether the current top ranking is reasonable for the candidate. Do not reorder vacancies and do not change score. "
				"For each vacancy, justify the current_rank using score, matched skills, missing skills, concerns, freshness and explicit vacancy facts. "
				"Compare only to nearby alternatives when the provided data supports it. "
				"Assign llm_rank_group as exactly one of: top priority, good backup, manual review. "
				"Write actionable Russian text: why this rank is justified, what weakens/strengthens it, and what to verify before applying. "
				"Return one item for every input vacancy and keep vacancy_id unchanged."
			),
			"ranked_vacancies": [
				dict(_compact_vacancy_for_llm(vacancy), current_rank=offset + index)
				for index, vacancy in enumerate(batch, start=1)
			],
			"batch": {"offset": offset, "size": len(batch), "total": len(target_vacancies)},
			"expected_json_shape": {
				"items": [
					{
						"vacancy_id": "string",
						"title": "string",
						"llm_rank_comment": "detailed Russian justification, 2-4 sentences",
						"llm_rank_group": "top priority | good backup | manual review",
					}
				],
				"top5_summary": "Russian summary of the vacancies in this batch",
			},
		}

	def request_batch(batch: list[dict[str, Any]], offset: int) -> dict[str, Any]:
		return llm_client.json_task(
			stage="rank_vacancies",
			system_prompt=(
				"You are a vacancy ranking reviewer. Return only valid JSON matching expected_json_shape. "
				"The rule-based order and score are fixed; add ranking justification only. "
				"Use only supplied evidence and explicitly mention uncertainty when data is missing."
			),
			payload=build_payload(batch, offset),
		)

	def apply_result(result: dict[str, Any]) -> None:
		items = result.get("items")
		if isinstance(items, list):
			for item in items:
				if not isinstance(item, dict):
					continue
				vacancy = by_id.get(str(item.get("vacancy_id") or "").strip()) or by_id.get(str(item.get("title") or "").strip())
				if not vacancy:
					continue
				vacancy["llm_rank_used"] = True
				vacancy["llm_rank_comment"] = _as_text(item.get("llm_rank_comment"))
				vacancy["llm_rank_group"] = _as_text(item.get("llm_rank_group"))
		top5_summary = _as_text(result.get("top5_summary"))
		if top5_summary:
			for vacancy in target_vacancies:
				vacancy["llm_top5_summary"] = top5_summary

	_run_llm_batches(target_vacancies, request_batch=request_batch, apply_items=apply_result, progress_callback=progress_callback)


def _run_llm_batches(
	target_items: list[dict[str, Any]],
	*,
	request_batch: Any,
	apply_items: Any,
	progress_callback: Any | None,
) -> None:
	if not target_items:
		return
	batches = [(offset, target_items[offset : offset + LLM_BATCH_SIZE]) for offset in range(0, len(target_items), LLM_BATCH_SIZE)]
	sent_items = sum(len(batch) for _, batch in batches)
	completed_items = 0
	if progress_callback:
		progress_callback(completed_items, len(target_items), f"отправлено: {sent_items}/{len(target_items)}, ответы: 0/{len(target_items)}")
	max_workers = min(LLM_MAX_WORKERS, len(batches))
	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		futures = {executor.submit(request_batch, batch, offset): batch for offset, batch in batches}
		for future in as_completed(futures):
			batch = futures[future]
			result = future.result()
			if isinstance(result, dict) and result:
				apply_items(result)
			completed_items += len(batch)
			if progress_callback:
				progress_callback(
					completed_items,
					len(target_items),
					f"отправлено: {sent_items}/{len(target_items)}, ответы: {completed_items}/{len(target_items)}",
				)


def _compact_vacancy_for_llm(vacancy: dict[str, Any]) -> dict[str, Any]:
	return {
		"vacancy_id": _vacancy_key(vacancy),
		"title": vacancy.get("title") or vacancy.get("normalized_title"),
		"company": vacancy.get("company"),
		"role": vacancy.get("role"),
		"level": vacancy.get("level"),
		"score": vacancy.get("score"),
		"score_breakdown": vacancy.get("score_breakdown", []),
		"matched_skills": vacancy.get("matched_skills", []),
		"missing_target_skills": vacancy.get("missing_target_skills", []),
		"concerns": vacancy.get("concerns", []),
		"format": vacancy.get("work_format"),
		"location": vacancy.get("location"),
		"salary": vacancy.get("salary"),
		"description": _clean_llm_source_text(vacancy.get("description"), limit=300),
		"requirements": _clean_llm_source_text(vacancy.get("requirements"), limit=300),
		"responsibilities": _clean_llm_source_text(vacancy.get("responsibilities"), limit=300),
		"conditions": _clean_llm_source_text(vacancy.get("conditions"), limit=300),
	}


def _vacancy_key(vacancy: dict[str, Any]) -> str:
	return _as_text(vacancy.get("vacancy_id")) or _as_text(vacancy.get("url")) or _as_text(vacancy.get("title"))


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
	try:
		number = int(value)
	except (TypeError, ValueError):
		return default
	return max(minimum, min(maximum, number))


def _build_trace(scored_vacancies: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return [
		{
			"rank": index,
			"title": vacancy.get("title") or vacancy.get("normalized_title"),
			"company": vacancy.get("company"),
			"score": vacancy.get("score", 0),
			"llm_adjustment": vacancy.get("llm_adjustment"),
			"llm_comment": vacancy.get("llm_comment"),
			"llm_rank_comment": vacancy.get("llm_rank_comment"),
			"priority": vacancy.get("application_priority"),
			"reasons": vacancy.get("reasons", []),
			"concerns": vacancy.get("concerns", []),
			"matched_skills": vacancy.get("matched_skills", []),
			"missing_target_skills": vacancy.get("missing_target_skills", []),
			"vacancy_skills": vacancy.get("vacancy_skills", []),
			"extracted_requirements": vacancy.get("extracted_requirements", []),
			"matched_roles": vacancy.get("matched_roles", []),
			"score_breakdown": vacancy.get("score_breakdown", []),
		}
		for index, vacancy in enumerate(scored_vacancies, start=1)
	]
