from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse
from typing import Any


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


@dataclass(slots=True)
class LLMClient:
	enabled: bool
	mode: str
	model: str = ""
	base_url: str = ""
	reason: str = ""
	timeout: int = 60
	max_tokens: int = 1200
	call_trace: list[dict[str, Any]] = field(default_factory=list)
	_api_key: str = ""
	_trace_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

	@classmethod
	def from_env(cls, *, dry_run: bool = False) -> "LLMClient":
		if dry_run:
			return cls(enabled=False, mode="dry_run", reason="--dry-run enabled")

		api_key = (
			os.environ.get("OPENAI_API_KEY")
			or os.environ.get("OPENAI_COMPATIBLE_API_KEY")
			or os.environ.get("POLZA_API_KEY")
			or ""
		)
		if not api_key:
			return cls(enabled=False, mode="dry_run", reason="OPENAI_API_KEY / OPENAI_COMPATIBLE_API_KEY / POLZA_API_KEY is not set")

		base_url = _normalize_base_url(
			os.environ.get("OPENAI_BASE_URL")
			or os.environ.get("OPENAI_COMPATIBLE_BASE_URL")
			or os.environ.get("POLZA_BASE_URL")
			or DEFAULT_BASE_URL
		)
		model = os.environ.get("OPENAI_MODEL") or os.environ.get("OPENAI_COMPATIBLE_MODEL") or DEFAULT_MODEL
		timeout = int(os.environ.get("OPENAI_TIMEOUT", "900"))
		max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "1200"))
		return cls(
			enabled=True,
			mode="llm",
			model=model,
			base_url=base_url,
			reason="LLM enabled by API key",
			timeout=timeout,
			max_tokens=max_tokens,
			_api_key=api_key,
		)

	def json_task(self, *, stage: str, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
		if not self.enabled:
			self._append_trace({"stage": stage, "mode": self.mode, "ok": False, "reason": self.reason})
			return {}

		user_prompt = (
			"Верни строго JSON без markdown. "
			"Не меняй rule-based score, если в задании просится только комментарий или adjustment.\n\n"
			+ json.dumps(payload, ensure_ascii=False)
		)
		request_payload = {
			"model": self.model,
			"messages": [
				{"role": "system", "content": system_prompt},
				{"role": "user", "content": user_prompt},
			],
			"temperature": 0.2,
			"max_tokens": self.max_tokens,
			"response_format": {"type": "json_object"},
		}

		try:
			response = self._post_chat_completion(request_payload)
		except urllib.error.HTTPError as error:
			if error.code != 400:
				return self._record_error(stage, error)
			request_payload.pop("response_format", None)
			try:
				response = self._post_chat_completion(request_payload)
			except Exception as retry_error:  # noqa: BLE001 - fallback must survive provider differences.
				return self._record_error(stage, retry_error)
		except Exception as error:  # noqa: BLE001 - LLM layer must never break dry-run fallback.
			return self._record_error(stage, error)

		content = _extract_content(response)
		parsed = _parse_json_object(content)
		if not parsed:
			parsed = self._retry_json_task(stage=stage, system_prompt=system_prompt, payload=payload)
		if not parsed:
			self._append_trace(
				{
					"stage": stage,
					"mode": self.mode,
					"model": self.model,
					"base_url": self.base_url,
					"ok": False,
					"reason": "LLM returned non-JSON content",
					"response_id": response.get("id"),
					"content_preview": content[:300],
				}
			)
			return {}

		self._append_trace(
			{"stage": stage, "mode": self.mode, "model": self.model, "base_url": self.base_url, "ok": True, "response_id": response.get("id")}
		)
		return parsed

	def _append_trace(self, item: dict[str, Any]) -> None:
		with self._trace_lock:
			self.call_trace.append(item)

	def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
		endpoint = f"{self.base_url}/chat/completions"
		request = urllib.request.Request(
			endpoint,
			data=json.dumps(payload).encode("utf-8"),
			headers={
				"Authorization": f"Bearer {self._api_key}",
				"Content-Type": "application/json",
			},
			method="POST",
		)
		with urllib.request.urlopen(request, timeout=self.timeout) as response:
			return json.loads(response.read().decode("utf-8"))

	def _record_error(self, stage: str, error: Exception) -> dict[str, Any]:
		trace = {
			"stage": stage,
			"mode": self.mode,
			"model": self.model,
			"base_url": self.base_url,
			"ok": False,
			"reason": f"{type(error).__name__}: {error}",
		}
		if isinstance(error, urllib.error.HTTPError):
			trace["http_status"] = error.code
		self._append_trace(trace)
		return {}

	def _retry_json_task(self, *, stage: str, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
		expected_shape = payload.get("expected_json_shape") if isinstance(payload, dict) else None
		retry_payload = {
			"model": self.model,
			"messages": [
				{"role": "system", "content": f"{system_prompt}\nReturn only one valid JSON object. No markdown, no prose."},
				{
					"role": "user",
					"content": (
						"Предыдущий ответ не был валидным JSON. Повтори задачу и верни строго один JSON-объект. "
						"Если данных мало, заполни поля краткими значениями без выдумывания фактов.\n\n"
						+ json.dumps(
							{
								"expected_json_shape": expected_shape,
								"payload": payload,
							},
							ensure_ascii=False,
						)
					),
				},
			],
			"temperature": 0.0,
			"max_tokens": max(self.max_tokens, 1800),
			"response_format": {"type": "json_object"},
		}
		try:
			response = self._post_chat_completion(retry_payload)
		except urllib.error.HTTPError as error:
			if error.code != 400:
				return self._record_error(stage, error)
			retry_payload.pop("response_format", None)
			try:
				response = self._post_chat_completion(retry_payload)
			except Exception as retry_error:  # noqa: BLE001 - fallback must survive provider differences.
				return self._record_error(stage, retry_error)
		except Exception as error:  # noqa: BLE001 - LLM layer must never break dry-run fallback.
			return self._record_error(stage, error)
		parsed = _parse_json_object(_extract_content(response))
		if parsed:
			self._append_trace(
				{
					"stage": stage,
					"mode": self.mode,
					"model": self.model,
					"base_url": self.base_url,
					"ok": True,
					"response_id": response.get("id"),
					"retry": True,
				}
			)
		return parsed


def _extract_content(response: dict[str, Any]) -> str:
	choices = response.get("choices")
	if isinstance(choices, list) and choices:
		message = choices[0].get("message") or {}
		content = message.get("content")
		if isinstance(content, str):
			return content
		if isinstance(content, list):
			parts = []
			for item in content:
				if not isinstance(item, dict):
					continue
				text = item.get("text")
				if isinstance(text, str):
					parts.append(text)
					continue
				nested_text = item.get("content")
				if isinstance(nested_text, str):
					parts.append(nested_text)
			return "\n".join(parts)
	return ""


def _parse_json_object(content: str) -> dict[str, Any]:
	text = content.strip()
	if not text:
		return {}

	fenced_match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
	if fenced_match:
		text = fenced_match.group(1).strip()

	try:
		parsed = json.loads(text)
	except json.JSONDecodeError:
		try:
			parsed = json.loads(text, strict=False)
		except json.JSONDecodeError:
			start = text.find("{")
			end = text.rfind("}")
			if start == -1 or end == -1 or end <= start:
				return {}
			try:
				parsed = json.loads(text[start : end + 1])
			except json.JSONDecodeError:
				try:
					parsed = json.loads(text[start : end + 1], strict=False)
				except json.JSONDecodeError:
					return {}

	return parsed if isinstance(parsed, dict) else {}


def _normalize_base_url(value: str) -> str:
	base_url = value.rstrip("/")
	parsed = urlparse(base_url)
	if parsed.netloc == "api.polza.ai":
		return "https://polza.ai/api/v1"
	if parsed.netloc == "polza.ai" and parsed.path.rstrip("/") == "/api":
		return "https://polza.ai/api/v1"
	return base_url
