from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import unescape
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .base import clean_text, join_values, normalize_format, normalize_level, normalize_salary, safe_output_path, write_vacancies_csv


DEFAULT_USER_AGENT = "AI-Vacancy-Match-Agent/1.0 (+local educational parser)"


@dataclass(slots=True)
class FetchedHTML:
    url: str
    source_site: str
    status: int | None
    ok: bool
    html: str = ""
    error: str = ""


class HTMLFetcher:
    def __init__(
        self,
        *,
        timeout: int = 15,
        delay: float = 0.7,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.delay = max(0.0, delay)
        self.user_agent = user_agent
        self._sleep = sleep
        self._last_request_at = 0.0

    def fetch(self, url: str) -> FetchedHTML:
        normalized_url = clean_text(url)
        source_site = source_from_url(normalized_url)
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return FetchedHTML(url=normalized_url, source_site=source_site, status=None, ok=False, error="Broken URL")

        self._wait_if_needed()
        request = Request(
            normalized_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - user-provided job URLs, no captcha bypass.
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                html = body.decode(charset, errors="replace")
                return FetchedHTML(url=normalized_url, source_site=source_site, status=response.status, ok=True, html=html)
        except HTTPError as error:
            return FetchedHTML(url=normalized_url, source_site=source_site, status=error.code, ok=False, error=str(error))
        except (URLError, TimeoutError, OSError) as error:
            return FetchedHTML(url=normalized_url, source_site=source_site, status=None, ok=False, error=str(error))

    def _wait_if_needed(self) -> None:
        if self.delay <= 0:
            self._last_request_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.delay:
            self._sleep(self.delay - elapsed)
        self._last_request_at = time.monotonic()


class HTMLCleaner:
    def __init__(self, *, max_chars: int = 15000) -> None:
        self.max_chars = max_chars

    def clean(self, html: str) -> str:
        value = str(html or "")
        value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
        value = re.sub(r"<(script|style|noscript|svg|canvas)\b.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(r"<(nav|footer|header|aside|form)\b.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        value = re.sub(
            r"<[^>]+(?:advert|banner|cookie|modal|popup|recommend|similar|promo)[^>]*>.*?</[^>]+>",
            " ",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"</(p|div|li|section|article|h[1-6])>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"<[^>]+>", " ", value)
        text = unescape(value)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return self._relevant_fragment(text)

    def _relevant_fragment(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        lowered = text.lower().replace("ё", "е")
        terms = [
            "ваканси",
            "описание",
            "требования",
            "обязанности",
            "условия",
            "зарплат",
            "junior",
            "intern",
            "analyst",
            "developer",
        ]
        positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
        start = max(0, min(positions) - self.max_chars // 4) if positions else 0
        return text[start : start + self.max_chars].strip()


class LLMVacancyExtractor:
    def __init__(self, llm_client: Any | None) -> None:
        self.llm_client = llm_client

    def extract(self, *, text: str, url: str, source_site: str) -> tuple[dict[str, Any] | None, str]:
        if not getattr(self.llm_client, "enabled", False):
            return None, "LLM is disabled; URL HTML extraction requires LLM"
        if not text.strip():
            return None, "Cleaned HTML is empty"

        result = self.llm_client.json_task(
            stage="url_html_extract",
            system_prompt=(
                "You extract exactly one job vacancy from a cleaned vacancy-page HTML/text fragment. "
                "Return only valid JSON matching expected_json_shape. No markdown or prose. "
                "Use only text present in cleaned_text. Do not infer hidden facts from the source name, URL, or common market knowledge. "
                "If this fragment is not an individual vacancy page, return {\"vacancy\": {}}. "
                "For absent text fields use an empty string; use 'unknown' only for enum fields where no value is visible."
            ),
            payload={
                "source_site": source_site,
                "vacancy_url": url,
                "cleaned_text": text[:15000],
                "instruction": (
                    "Extract the vacancy title, employer, city, work format, employment type, experience level, salary range/currency, skills, description, requirements, responsibilities, conditions and publication date. "
                    "Keep long text fields concise but factual. Prefer exact phrases from the page over paraphrase. "
                    "Do not include navigation, similar vacancies, ads, cookie banners or unrelated company marketing."
                ),
                "expected_json_shape": {
                    "vacancy": {
                        "title": "string",
                        "company": "string",
                        "city": "string",
                        "work_format": "remote|hybrid|office|unknown",
                        "employment_type": "full-time|part-time|internship|contract|unknown",
                        "experience_level": "internship|junior|middle|senior|unknown",
                        "salary_min": "number_or_empty",
                        "salary_max": "number_or_empty",
                        "salary_currency": "RUB|USD|EUR|unknown",
                        "skills": ["string"],
                        "description": "string",
                        "requirements": "string",
                        "responsibilities": "string",
                        "conditions": "string",
                        "published_at": "YYYY-MM-DD_or_empty",
                    }
                },
            },
        )
        vacancy = self._coerce_vacancy(result)
        if not vacancy:
            return None, "LLM returned no valid vacancy JSON"
        return vacancy, ""

    def _coerce_vacancy(self, result: dict[str, Any]) -> dict[str, Any] | None:
        raw = result.get("vacancy")
        if isinstance(raw, dict):
            vacancy = raw
        elif isinstance(result.get("vacancies"), list) and result["vacancies"] and isinstance(result["vacancies"][0], dict):
            vacancy = result["vacancies"][0]
        elif isinstance(result, dict) and any(key in result for key in ("title", "company", "description")):
            vacancy = result
        else:
            return None

        if not any(clean_text(vacancy.get(key)) for key in ("title", "company", "description", "requirements")):
            return None
        return vacancy


class VacancyNormalizer:
    def normalize(self, raw: dict[str, Any], *, source_site: str, url: str) -> dict[str, Any]:
        title = clean_text(raw.get("title"))
        skills = raw.get("skills")
        skills_text = join_values(skills if isinstance(skills, list) else [skills])
        description_parts = [
            raw.get("description"),
            raw.get("requirements"),
            raw.get("responsibilities"),
            raw.get("conditions"),
        ]
        salary = self._salary(raw)
        level_raw = raw.get("experience_level") or raw.get("level") or f"{title} {raw.get('description') or ''}"
        format_raw = raw.get("work_format") or raw.get("format") or raw.get("conditions") or raw.get("description")
        return {
            "vacancy_id": f"url:{abs(hash(url)) % 100_000_000}",
            "source": source_site,
            "title": title,
            "company": clean_text(raw.get("company")),
            "role": title,
            "level": normalize_level(level_raw) or "unknown",
            "format": normalize_format(format_raw) or "unknown",
            "city": clean_text(raw.get("city")) or "unknown",
            "relocation_possible": "",
            "published_at": clean_text(raw.get("published_at")),
            "deadline": "",
            "salary_rub": salary,
            "stack": skills_text,
            "key_skills": skills_text,
            "english_level": "",
            "link": url,
            "description": clean_text(" ".join(str(part or "") for part in description_parts))[:1800],
            "requirements": clean_text(raw.get("requirements"))[:900],
            "responsibilities": clean_text(raw.get("responsibilities"))[:900],
            "conditions": clean_text(raw.get("conditions"))[:900],
            "employment_type": clean_text(raw.get("employment_type")),
        }

    def _salary(self, raw: dict[str, Any]) -> str:
        if raw.get("salary_rub"):
            return normalize_salary(raw.get("salary_rub"))
        minimum = raw.get("salary_min")
        maximum = raw.get("salary_max")
        currency = clean_text(raw.get("salary_currency")) or "RUB"
        values = [clean_text(minimum), clean_text(maximum)]
        values = [value for value in values if value and value.lower() not in {"none", "null", "unknown"}]
        if not values:
            return ""
        return "-".join(values) + (f" {currency}" if currency else "")


class VacancyCSVWriter:
    def write(self, vacancies: list[dict[str, Any]], *, output_dir: str, filename: str = "vacancies.csv") -> str:
        output_path = safe_output_path(output_dir, filename)
        write_vacancies_csv(vacancies, output_path)
        return str(output_path)


def source_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "hh.ru" in host:
        return "hh-url"
    if "superjob.ru" in host:
        return "superjob-url"
    if "rabota.ru" in host:
        return "rabota-url"
    if "zarplata.ru" in host:
        return "zarplata-url"
    if "avito.ru" in host:
        return "avito-url"
    if "career.habr.com" in host:
        return "habr-career-url"
    if "geekjob.ru" in host:
        return "geekjob-url"
    return host or "url"


def parsed_at() -> str:
    return datetime.now().isoformat(timespec="seconds")
