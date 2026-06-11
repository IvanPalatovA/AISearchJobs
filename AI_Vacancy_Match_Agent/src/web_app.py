from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import os
import json
from pathlib import Path
import re
import selectors
import subprocess
import sys
import threading
import time
import traceback
import uuid
import csv
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from llm_client import LLMClient
from sources.base import VACANCY_COLUMNS, write_vacancies_csv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = PROJECT_ROOT / "data" / "file_metadata.json"
SUBSCRIPTIONS_PATH = PROJECT_ROOT / "data" / "email_subscriptions.json"
CRITERIA_DIR = PROJECT_ROOT / "data" / "criteria"
FILTERS_DIR = PROJECT_ROOT / "data" / "filters"
PYTHON = sys.executable
JOBS: dict[str, dict[str, Any]] = {}
JOB_QUEUE: list[str] = []
JOBS_LOCK = threading.Lock()
JOB_WORKER_RUNNING = False
SUBSCRIPTIONS_LOCK = threading.Lock()
SCHEDULER_STARTED = False
TELEGRAM_BOT_STARTED = False
TELEGRAM_DIALOGS: dict[str, dict[str, Any]] = {}
TELEGRAM_DIALOGS_LOCK = threading.Lock()
QUICK_ALLOWED_SOURCES = ["hh", "superjob"]


def main() -> int:
    host = "127.0.0.1"
    port = _find_port(8000)
    _start_email_scheduler()
    _start_telegram_bot()
    server = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"UI started: http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nUI stopped.")
    return 0


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "AIVacancyUI/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(_build_index_html())
            return
        if parsed.path == "/api/files":
            criteria_files = _list_criteria_files()
            filter_files = _list_filter_files()
            self._send_json({
                "files": _list_vacancy_files(),
                "criteria_files": criteria_files,
                "filter_files": filter_files,
                "criteria": criteria_files[0] if criteria_files else {},
                "filter": filter_files[0] if filter_files else {},
            })
            return
        if parsed.path == "/api/vacancy-file-summary":
            params = parse_qs(parsed.query)
            self._send_json(_vacancy_file_summary(params.get("path", [""])[0]))
            return
        if parsed.path == "/api/vacancy-file-cards":
            params = parse_qs(parsed.query)
            self._send_json(_vacancy_file_cards(params.get("path", [""])[0]))
            return
        if parsed.path == "/api/trace-cards":
            params = parse_qs(parsed.query)
            self._send_json(_trace_cards(params.get("path", [""])[0]))
            return
        if parsed.path == "/api/file":
            params = parse_qs(parsed.query)
            path_value = params.get("path", [""])[0]
            path = _safe_project_path(path_value, allow_outputs=True) or _safe_criteria_path(path_value) or _safe_filter_path(path_value)
            if not path or not path.exists():
                self._send_json({"error": "file not found"}, status=404)
                return
            self._send_text(path.read_text(encoding="utf-8", errors="replace"))
            return
        if parsed.path == "/api/job":
            params = parse_qs(parsed.query)
            self._send_json(_job_status(params.get("id", [""])[0]))
            return
        if parsed.path == "/api/email-subscriptions":
            self._send_json({"ok": True, "subscriptions": _public_email_subscriptions()})
            return
        if parsed.path == "/api/telegram-bot":
            self._send_json(_telegram_bot_info())
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._read_json()
        if parsed.path == "/api/rank":
            self._send_json(_start_job("rank", payload))
            return
        if parsed.path == "/api/fetch":
            self._send_json(_start_job("fetch", payload))
            return
        if parsed.path == "/api/quick-search":
            self._send_json(_start_job("quick", payload))
            return
        if parsed.path == "/api/cancel-job":
            self._send_json(_cancel_job(str(payload.get("job_id") or "")))
            return
        if parsed.path == "/api/email-subscriptions":
            self._send_json(_create_email_subscription(payload))
            return
        if parsed.path == "/api/delete-email-subscription":
            self._send_json(_delete_email_subscription(payload))
            return
        if parsed.path == "/api/metadata":
            self._send_json(_save_metadata(payload))
            return
        if parsed.path == "/api/delete-file":
            self._send_json(_delete_csv_file(payload))
            return
        if parsed.path == "/api/generate-criteria":
            self._send_json(_generate_criteria_file(payload))
            return
        self._send_json({"error": "not found"}, status=404)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_job(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    queued_position = 1
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "progress": 0,
            "stage": "В очереди",
            "result": None,
            "error": "",
            "payload": payload,
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        }
        JOB_QUEUE.append(job_id)
        queued_position = _job_queue_position_locked(job_id)
    _ensure_job_worker()
    return {"ok": True, "job_id": job_id, "status": "queued", "queue_position": queued_position}


def _ensure_job_worker() -> None:
    global JOB_WORKER_RUNNING
    with JOBS_LOCK:
        if JOB_WORKER_RUNNING:
            return
        JOB_WORKER_RUNNING = True
    threading.Thread(target=_job_worker_loop, daemon=True).start()


def _job_worker_loop() -> None:
    global JOB_WORKER_RUNNING
    while True:
        with JOBS_LOCK:
            job_id = ""
            while JOB_QUEUE:
                candidate = JOB_QUEUE.pop(0)
                job = JOBS.get(candidate) or {}
                if job.get("status") == "queued":
                    job_id = candidate
                    break
            if not job_id:
                JOB_WORKER_RUNNING = False
                return
            job = JOBS.get(job_id) or {}
            kind = str(job.get("kind") or "")
            payload = dict(job.get("payload") or {})
        _run_job(job_id, kind, payload)


def _cancel_job(job_id: str) -> dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id is required"}
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}
        if job.get("status") != "queued":
            return {"ok": False, "error": "Only queued jobs can be cancelled."}
        JOB_QUEUE[:] = [item for item in JOB_QUEUE if item != job_id]
        job.update({"status": "cancelled", "stage": "Удалено из очереди", "error": "Задача удалена из очереди."})
    return {"ok": True, "job_id": job_id, "status": "cancelled"}


def _run_job(job_id: str, kind: str, payload: dict[str, Any]) -> None:
    try:
        _update_job(job_id, status="running", progress=0, stage="Запуск", started_at=datetime.now().isoformat(timespec="seconds"))
        if kind == "rank":
            result = _run_rank(payload, job_id=job_id)
        elif kind == "fetch":
            result = _run_fetch(payload, job_id=job_id)
        elif kind == "email":
            result = _run_email_subscription_job(str(payload.get("subscription_id") or ""))
        else:
            result = _run_quick_search(payload, job_id=job_id)
        _update_job(job_id, status="done", progress=100, stage="Готово", result=result)
    except Exception as error:  # noqa: BLE001 - UI job must report failures as JSON.
        _update_job(
            job_id,
            status="error",
            stage="Ошибка",
            error=f"{type(error).__name__}: {error}",
            error_details=_format_job_exception_details(kind=kind, payload=payload, error=error),
            debug_log=f"{datetime.now().isoformat(timespec='seconds')} unhandled job exception\n{traceback.format_exc()}",
        )


def _job_status(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {})
        queue_position = _job_queue_position_locked(job_id)
    if not job:
        return {"ok": False, "error": "job not found", "status": "error", "progress": 0, "stage": "Не найдено"}
    job["ok"] = job.get("status") != "error"
    job["queue_position"] = queue_position
    return job


def _job_queue_position_locked(job_id: str) -> int:
    if not job_id:
        return 0
    running_offset = 1 if any((job.get("status") == "running" and current_id != job_id) for current_id, job in JOBS.items()) else 0
    try:
        return JOB_QUEUE.index(job_id) + 1 + running_offset
    except ValueError:
        job = JOBS.get(job_id) or {}
        return 1 if job.get("status") == "running" else 0


def _update_job(job_id: str | None, **updates: Any) -> None:
    if not job_id:
        return
    with JOBS_LOCK:
        if job_id in JOBS:
            if "progress" in updates:
                try:
                    updates["progress"] = max(int(JOBS[job_id].get("progress") or 0), int(updates.get("progress") or 0))
                except (TypeError, ValueError):
                    updates.pop("progress", None)
            JOBS[job_id].update(updates)


def _run_rank(payload: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    selected = str(payload.get("vacancies") or "")
    vacancies_path = _safe_project_path(selected)
    if not vacancies_path or not vacancies_path.exists() or vacancies_path.suffix.lower() != ".csv":
        return {"ok": False, "error": "Choose an existing vacancies CSV."}

    selected_criteria = str(payload.get("criteria") or "criteria.csv")
    criteria_path = _safe_criteria_path(selected_criteria)
    if not criteria_path or not criteria_path.exists() or criteria_path.suffix.lower() != ".csv":
        return {"ok": False, "error": "Choose an existing criteria CSV."}

    mode = str(payload.get("mode") or "dry_run")
    llm_score = _coerce_bool(payload.get("llm_score"), default=mode == "llm")
    llm_explanation = _coerce_bool(payload.get("llm_explanation"), default=mode == "llm")
    top_k = _bounded_int(payload.get("top_k"), default=5, minimum=1, maximum=20)
    output_dir = PROJECT_ROOT / "output" / f"ui_rank_{_timestamp()}"
    cmd = [
        PYTHON,
        "-u",
        "src/main.py",
        "--vacancies",
        str(vacancies_path.relative_to(PROJECT_ROOT)),
        "--criteria",
        str(criteria_path.relative_to(PROJECT_ROOT)),
        "--output",
        str(output_dir.relative_to(PROJECT_ROOT)),
        "--top-k",
        str(top_k),
    ]
    if not llm_score and not llm_explanation:
        cmd.append("--dry-run")
    if llm_score:
        cmd.append("--llm-score")
    if llm_explanation:
        cmd.append("--llm-explanation")

    result = _run_command(
        cmd,
        job_id=job_id,
        progress_start=_bounded_int(payload.get("progress_start"), default=0, minimum=0, maximum=100),
        progress_end=_bounded_int(payload.get("progress_end"), default=100, minimum=0, maximum=100),
        timeout=_bounded_int(payload.get("command_timeout"), default=900, minimum=30, maximum=3600),
    )
    report_path = output_dir / "report.md"
    methodology_path = output_dir / "methodology.md"
    log_path = output_dir / "run.log"
    trace_path = output_dir / "trace.json"
    run_details = _rank_run_details(cmd, result, log_path)
    return {
        "ok": result.returncode == 0,
        "command": " ".join(cmd),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "report_path": _relative(report_path),
        "methodology_path": _relative(methodology_path),
        "log_path": _relative(log_path),
        "trace_path": _relative(trace_path),
        "report_preview": _read_preview(report_path, 8000),
        "methodology_preview": _read_preview(methodology_path, 8000),
        "trace_preview": _read_preview(trace_path, 12000),
        "run_log_preview": _read_preview(log_path, 8000),
        "run_details": run_details,
        "trace_summary": _trace_summary(trace_path),
        "card_vacancies": _rank_card_vacancies(trace_path),
    }


def _run_fetch(payload: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    selected_sources = _clean_sources(payload.get("sources"))
    if not selected_sources:
        return {"ok": False, "error": "Choose at least one vacancy source.", "status": "error", "status_label": "Выберите источник"}
    sources = ",".join(selected_sources)
    source_priorities = ",".join(
        f"{source}:{priority}" for source, priority in _clean_source_priorities(payload.get("source_priorities"), selected_sources).items()
    )
    keywords = _clean_keywords(payload.get("keywords") or payload.get("text"))
    if not keywords:
        return {"ok": False, "error": "Choose at least one search keyword.", "status": "error", "status_label": "Добавьте запрос"}
    selected_criteria = str(payload.get("criteria") or "filters.csv")
    criteria_path = _safe_filter_path(selected_criteria)
    hard_filters_enabled = bool(payload.get("hard_filters", True))
    if hard_filters_enabled and (not criteria_path or not criteria_path.exists() or criteria_path.suffix.lower() != ".csv"):
        return {"ok": False, "error": "Choose an existing criteria CSV.", "status": "error", "status_label": "Выберите критерии"}
    use_llm_html = bool(payload.get("use_llm_html", False))
    llm_max_limit = _bounded_int(payload.get("llm_max_limit"), default=50, minimum=1, maximum=300)
    max_limit = 50
    max_vacancies = _bounded_int(payload.get("max_vacancies"), default=max_limit, minimum=1, maximum=max_limit)
    request_timeout = _bounded_int(payload.get("timeout"), default=900, minimum=3, maximum=3600)
    pages = _bounded_int(payload.get("pages"), default=0, minimum=0, maximum=20)
    per_page = _bounded_int(payload.get("per_page"), default=0, minimum=0, maximum=50)
    command_timeout = _bounded_int(payload.get("command_timeout"), default=900, minimum=10, maximum=3600)
    output_dir = PROJECT_ROOT / "data" / "collected"

    cmd = [
        PYTHON,
        "-u",
        "src/fetch_vacancies.py",
        "--sources",
        sources,
        "--source-priorities",
        source_priorities,
        "--queries",
        json.dumps(keywords, ensure_ascii=False),
        "--max-vacancies",
        str(max_vacancies),
        "--output-dir",
        str(output_dir.relative_to(PROJECT_ROOT)),
        "--filename",
        "vacancies.csv",
        "--timeout",
        str(request_timeout),
    ]
    if pages:
        cmd.extend(["--pages", str(pages)])
    if per_page:
        cmd.extend(["--per-page", str(per_page)])
    if hard_filters_enabled and criteria_path:
        cmd.extend(["--criteria", str(criteria_path.relative_to(PROJECT_ROOT))])
    if not hard_filters_enabled:
        cmd.append("--skip-criteria-filters")
    if use_llm_html:
        cmd.append("--llm-only-html")
        if llm_max_limit != 50:
            cmd.extend(["--llm-max-cap", str(llm_max_limit)])
    else:
        cmd.append("--no-llm-html")

    before = {path.resolve() for path in output_dir.glob("vacancies*.csv")}
    result = _run_command(
        cmd,
        job_id=job_id,
        progress_start=_bounded_int(payload.get("progress_start"), default=0, minimum=0, maximum=100),
        progress_end=_bounded_int(payload.get("progress_end"), default=100, minimum=0, maximum=100),
        timeout=command_timeout,
    )
    after = sorted(
        (path for path in output_dir.glob("vacancies*.csv") if path.resolve() not in before),
        key=lambda path: path.stat().st_mtime,
    )
    created = after[-1] if after else _extract_created_path(result.stdout)
    trace_path = created.with_suffix(".trace.json") if created else None
    rows = _count_csv_rows(created)
    trace_summary = _trace_summary(trace_path)
    status = _fetch_status(returncode=result.returncode, rows=rows, trace_summary=trace_summary)
    error = _fetch_error_message(result=result, status=status, rows=rows, trace_summary=trace_summary)
    fetch_result = {
        "ok": status["ok"],
        "command": " ".join(cmd),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "command_ok": result.returncode == 0,
        **status,
        "created_path": _relative(created),
        "trace_path": _relative(trace_path),
        "rows": rows,
        "trace_summary": trace_summary,
        "source_breakdown": _csv_source_breakdown(created),
    }
    if error:
        fetch_result["error"] = error
    if hard_filters_enabled and criteria_path and rows < max_vacancies and not bool(payload.get("_disable_relaxed_completion")):
        relaxed_result = _run_relaxed_fetch_completion(
            payload=payload,
            base_result=fetch_result,
            criteria_path=criteria_path,
            target_rows=max_vacancies,
            job_id=job_id,
            progress_start=_bounded_int(payload.get("progress_start"), default=0, minimum=0, maximum=100),
            progress_end=_bounded_int(payload.get("progress_end"), default=100, minimum=0, maximum=100),
        )
        if relaxed_result.get("created_path"):
            return relaxed_result
    return fetch_result


def _run_quick_search(payload: dict[str, Any], *, job_id: str | None = None) -> dict[str, Any]:
    user_text = " ".join(str(payload.get("text") or "").split())
    if not user_text:
        return {"ok": False, "error": "Введите пожелания соискателя.", "status": "error", "status_label": "Нет запроса"}
    debug_log: list[str] = [f"{datetime.now().isoformat(timespec='seconds')} quick-search start text={user_text[:300]}"]

    _update_job(job_id, progress=4, stage="LLM анализ запроса")
    plan_result = _quick_search_plan(user_text)
    if not plan_result.get("ok"):
        debug_log.append(f"plan failed: {plan_result.get('error') or plan_result}")
        return {
            **plan_result,
            "status": "error",
            "status_label": "LLM недоступна",
            "debug_log": debug_log,
            "error_details": _build_quick_error_details(user_text=user_text, plan_result=plan_result, debug_log=debug_log),
        }
    plan = _normalize_quick_plan(dict(plan_result["plan"]))
    debug_log.append(
        f"plan ok: keywords={plan.get('keywords')} sources={plan.get('sources')} "
        f"rank_mode={plan.get('rank_mode')} max={plan.get('max_vacancies')} top_k={plan.get('top_k')}"
    )

    _update_job(job_id, progress=10, stage="Генерация CSV")
    quick_fetch_filters = _quick_fetch_filters(plan["hard_filters"])
    parameters_report = _quick_parameters_report(plan, quick_fetch_filters)
    _update_job(job_id, progress=10, stage="Генерация CSV", quick_plan=plan, parameters_report=parameters_report)
    filter_path = _write_quick_csv("filter", quick_fetch_filters, name=plan["filter_name"], description=plan["filter_description"])
    criteria_path = _write_quick_csv("criteria", plan["criteria"], name=plan["criteria_name"], description=plan["criteria_description"])
    debug_log.append(f"criteria generated: filter={_relative(filter_path)} criteria={_relative(criteria_path)}")

    _update_job(job_id, progress=16, stage="Сбор вакансий")
    fetch_result = _run_staged_quick_fetch(
        plan=plan,
        all_filters=quick_fetch_filters,
        all_filter_path=filter_path,
        job_id=job_id,
        progress_start=16,
        progress_end=72,
    )
    debug_log.append(_email_fetch_log("quick fetch staged", fetch_result))
    for stage in fetch_result.get("staged_fetch", []):
        debug_log.append(
            f"fetch stage {stage.get('stage')}: filters={stage.get('filter_count')} rows={stage.get('rows')} "
            f"added={stage.get('added_rows')} cumulative={stage.get('cumulative_rows')} status={stage.get('status')} "
            f"error={stage.get('error') or ''}"
        )
    source_report = _quick_sources_report(fetch_result.get("trace_summary") or {}, fetch_result.get("source_breakdown") or {})
    _update_job(job_id, progress=72, stage="Ранжирование", source_report=source_report)
    if not fetch_result.get("ok") or not fetch_result.get("created_path"):
        debug_log.append(f"quick fetch failed: {fetch_result.get('error') or fetch_result.get('status_label') or 'unknown'}")
        return {
            "ok": False,
            "status": fetch_result.get("status", "error"),
            "status_label": fetch_result.get("status_label", "Сбор не завершен"),
            "error": fetch_result.get("error") or "Сбор вакансий не дал результата.",
            "plan": plan,
            "filter_path": _relative(filter_path),
            "criteria_path": _relative(criteria_path),
            "fetch_result": fetch_result,
            "parameters_report": parameters_report,
            "source_report": source_report,
            "debug_log": debug_log,
            "error_details": _build_quick_error_details(
                user_text=user_text,
                plan=plan,
                fetch_result=fetch_result,
                criteria_path=criteria_path,
                filter_path=filter_path,
                debug_log=debug_log,
            ),
        }

    rank_result = _run_rank(
        {
            "vacancies": fetch_result["created_path"],
            "criteria": _relative(criteria_path),
            "mode": plan["rank_mode"],
            "top_k": plan["top_k"],
            "command_timeout": 900,
            "progress_start": 72,
            "progress_end": 100,
        },
        job_id=job_id,
    )
    debug_log.append(_email_rank_log("quick rank", rank_result))
    ok = bool(rank_result.get("ok"))
    if not ok:
        debug_log.append(f"quick rank failed: {rank_result.get('error') or rank_result.get('returncode') or 'unknown'}")
    return {
        **rank_result,
        "ok": ok,
        "status": "success" if ok else "error",
        "status_label": "Готово" if ok else "Ошибка",
        "quick_plan": plan,
        "filter_path": _relative(filter_path),
        "criteria_path": _relative(criteria_path),
        "created_path": fetch_result.get("created_path", ""),
        "fetch_result": fetch_result,
        "rank_result": rank_result,
        "parameters_report": parameters_report,
        "source_report": source_report,
        "debug_log": debug_log,
        "error_details": "" if ok else _build_quick_error_details(
            user_text=user_text,
            plan=plan,
            fetch_result=fetch_result,
            rank_result=rank_result,
            criteria_path=criteria_path,
            filter_path=filter_path,
            debug_log=debug_log,
        ),
    }


def _run_email_digest(subscription: dict[str, Any]) -> dict[str, Any]:
    run_log: list[str] = []
    user_text = str(subscription.get("text") or "").strip()
    if not user_text:
        return {"ok": False, "error": "Пустой текст подписки.", "run_log": run_log}

    run_log.append(f"{datetime.now().isoformat(timespec='seconds')} start subscription={subscription.get('id')} k={subscription.get('k')}")
    plan_result = _quick_search_plan(user_text)
    if not plan_result.get("ok"):
        run_log.append(f"plan failed: {plan_result.get('error') or plan_result}")
        return {**plan_result, "run_log": run_log}
    plan = _normalize_quick_plan(dict(plan_result["plan"]))
    digest_k = _bounded_int(subscription.get("k"), default=5, minimum=1, maximum=20)
    rank_limit = min(20, max(digest_k * 3, digest_k))
    plan["top_k"] = rank_limit
    plan["max_vacancies"] = max(_bounded_int(plan.get("max_vacancies"), default=50, minimum=1, maximum=50), min(50, digest_k * 8))
    run_log.append(f"plan ok: keywords={plan.get('keywords')} sources={plan.get('sources')} rank_mode={plan.get('rank_mode')} max={plan.get('max_vacancies')}")

    quick_fetch_filters = _quick_fetch_filters(plan["hard_filters"])
    filter_path = _write_quick_csv("filter", quick_fetch_filters, name=plan["filter_name"], description=plan["filter_description"])
    criteria_path = _write_quick_csv("criteria", plan["criteria"], name=plan["criteria_name"], description=plan["criteria_description"])
    fetch_result = _run_staged_quick_fetch(
        plan=plan,
        all_filters=quick_fetch_filters,
        all_filter_path=filter_path,
    )
    run_log.append(_email_fetch_log("fetch staged", fetch_result))
    for stage in fetch_result.get("staged_fetch", []):
        run_log.append(
            f"fetch stage {stage.get('stage')}: filters={stage.get('filter_count')} rows={stage.get('rows')} "
            f"added={stage.get('added_rows')} cumulative={stage.get('cumulative_rows')} skipped={stage.get('skipped')}"
        )
    if not fetch_result.get("ok") or not fetch_result.get("created_path"):
        return {
            "ok": False,
            "error": fetch_result.get("error") or fetch_result.get("status_label") or "Сбор вакансий не дал результата.",
            "fetch_result": _compact_result(fetch_result),
            "run_log": run_log,
        }

    rank_payload = {
        "vacancies": fetch_result["created_path"],
        "criteria": _relative(criteria_path),
        "mode": plan["rank_mode"],
        "top_k": rank_limit,
    }
    rank_result = _run_rank(rank_payload)
    run_log.append(_email_rank_log("rank primary", rank_result))
    if not rank_result.get("ok") and plan["rank_mode"] == "llm":
        run_log.append("rank retry: switched to dry_run after LLM rank failure")
        rank_result = _run_rank({**rank_payload, "mode": "dry_run"})
        run_log.append(_email_rank_log("rank retry", rank_result))
    if not rank_result.get("ok"):
        return {
            "ok": False,
            "error": rank_result.get("error") or _first_non_empty(rank_result.get("stderr"), rank_result.get("stdout")) or "Ранжирование не завершилось.",
            "rank_result": _compact_result(rank_result),
            "run_log": run_log,
        }

    sent_keys = set(subscription.get("sent_vacancy_keys") or [])
    cards = [card for card in _rank_card_vacancies(_safe_project_path(rank_result.get("trace_path", ""), allow_outputs=True)) if _vacancy_digest_key(card) not in sent_keys]
    selected_cards = cards[:digest_k]
    run_log.append(f"cards selected: available_new={len(cards)} selected={len(selected_cards)} already_sent={len(sent_keys)}")
    if not selected_cards:
        return {"ok": True, "sent": 0, "message": "Новых вакансий для отправки нет.", "plan": plan, "run_log": run_log}

    delivery_result = _send_digest_notifications(subscription, selected_cards)
    run_log.extend(str(line) for line in delivery_result.get("run_log") or [])
    if not delivery_result.get("ok"):
        return {**delivery_result, "sent": 0, "plan": plan, "run_log": run_log}
    run_log.append(f"delivery ok: sent={len(selected_cards)} channels={delivery_result.get('channels')}")
    return {
        "ok": True,
        "sent": len(selected_cards),
        "sent_keys": [_vacancy_digest_key(card) for card in selected_cards],
        "cards": selected_cards,
        "plan": plan,
        "delivery": delivery_result,
        "run_log": run_log,
    }


def _email_fetch_log(label: str, result: dict[str, Any]) -> str:
    trace = result.get("trace_summary") or {}
    metrics = trace.get("fetch_metrics") or {}
    return (
        f"{label}: ok={result.get('ok')} status={result.get('status')} label={result.get('status_label')} "
        f"returncode={result.get('returncode')} rows={result.get('rows')} raw={trace.get('raw_rows')} unique={trace.get('unique_rows')} "
        f"considered={metrics.get('total_considered')} created={result.get('created_path')} error={result.get('error') or ''} "
        f"stderr={str(result.get('stderr') or '')[:500]} stdout={str(result.get('stdout') or '')[:500]}"
    )


def _email_rank_log(label: str, result: dict[str, Any]) -> str:
    summary = result.get("trace_summary") or {}
    return (
        f"{label}: ok={result.get('ok')} returncode={result.get('returncode')} cards={len(result.get('card_vacancies') or [])} "
        f"ranked={summary.get('trace_context', {}).get('ranked_count')} error={result.get('error') or ''} "
        f"stderr={str(result.get('stderr') or '')[:500]} stdout={str(result.get('stdout') or '')[:500]}"
    )


def _build_quick_error_details(
    *,
    user_text: str,
    debug_log: list[str],
    plan_result: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    fetch_result: dict[str, Any] | None = None,
    rank_result: dict[str, Any] | None = None,
    criteria_path: Path | None = None,
    filter_path: Path | None = None,
) -> str:
    payload = {
        "user_text": user_text,
        "plan_result": _compact_result(plan_result or {}) if plan_result else {},
        "plan": plan or {},
        "filter_path": _relative(filter_path),
        "criteria_path": _relative(criteria_path),
        "fetch_result": _compact_result(fetch_result or {}) if fetch_result else {},
        "rank_result": _compact_result(rank_result or {}) if rank_result else {},
        "debug_log": debug_log[-120:],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)[:60000]


def _format_job_exception_details(*, kind: str, payload: dict[str, Any], error: Exception) -> str:
    body = {
        "kind": kind,
        "payload": payload,
        "error": f"{type(error).__name__}: {error}",
        "traceback": traceback.format_exc(),
    }
    return json.dumps(body, ensure_ascii=False, indent=2)[:60000]


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "ok",
        "status",
        "status_label",
        "error",
        "returncode",
        "created_path",
        "trace_path",
        "rows",
        "command",
        "stdout",
        "stderr",
        "trace_summary",
        "source_breakdown",
        "staged_fetch",
        "failed_requests",
        "successful_requests",
        "api_failures",
        "html_successes",
        "error_details",
        "debug_log",
    }
    compact = {key: value for key, value in result.items() if key in keep}
    for key in ("stdout", "stderr", "command"):
        if key in compact:
            compact[key] = str(compact[key] or "")[:3000]
    return compact


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text[:600]
    return ""


def _create_email_subscription(payload: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(payload.get("text") or "").split())
    emails = _clean_emails(payload.get("emails"))
    telegram_recipients = _clean_telegram_recipients(payload.get("telegram_recipients"))
    email_theme = "dark" if str(payload.get("email_theme") or "").strip().lower() == "dark" else "light"
    interval_value = _bounded_int(payload.get("interval_value"), default=24, minimum=1, maximum=365)
    interval_unit = "days" if str(payload.get("interval_unit") or "").lower().startswith("day") else "hours"
    k = _bounded_int(payload.get("k"), default=5, minimum=1, maximum=20)
    send_now = _coerce_bool(payload.get("send_now"), default=True)
    if not text:
        return {"ok": False, "error": "Опишите, какие вакансии нужно искать."}
    if not emails and not telegram_recipients:
        return {"ok": False, "error": "Добавьте хотя бы один email или Telegram-получателя."}

    now = datetime.now()
    subscription = {
        "id": uuid.uuid4().hex,
        "text": text,
        "emails": emails,
        "telegram_recipients": telegram_recipients,
        "email_theme": email_theme,
        "interval_value": interval_value,
        "interval_unit": interval_unit,
        "k": k,
        "enabled": True,
        "created_at": now.isoformat(timespec="seconds"),
        "next_run_at": (now + _subscription_interval({"interval_value": interval_value, "interval_unit": interval_unit})).isoformat(timespec="seconds"),
        "last_run_at": "",
        "last_status": "Ожидает первой отправки",
        "last_error_details": "",
        "last_run_log": "",
        "sent_vacancy_keys": [],
        "running": False,
    }
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        data["subscriptions"] = [subscription, *data.get("subscriptions", [])]
        _write_email_subscription_data_unlocked(data)
    result = {"ok": True, "subscription": _public_email_subscription(subscription)}
    if send_now:
        result.update(_start_job("email", {"subscription_id": subscription["id"]}))
    return result


def _delete_email_subscription(payload: dict[str, Any]) -> dict[str, Any]:
    subscription_id = str(payload.get("id") or "").strip()
    if not subscription_id:
        return {"ok": False, "error": "Не передан id рассылки."}
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        before = len(data.get("subscriptions", []))
        data["subscriptions"] = [item for item in data.get("subscriptions", []) if item.get("id") != subscription_id]
        if len(data["subscriptions"]) == before:
            return {"ok": False, "error": "Рассылка не найдена."}
        _write_email_subscription_data_unlocked(data)
        return {"ok": True, "subscriptions": [_public_email_subscription(item) for item in data.get("subscriptions", [])]}


def _start_email_scheduler() -> None:
    global SCHEDULER_STARTED
    if SCHEDULER_STARTED:
        return
    SCHEDULER_STARTED = True
    thread = threading.Thread(target=_email_scheduler_loop, daemon=True)
    thread.start()


def _email_scheduler_loop() -> None:
    while True:
        try:
            due = _claim_due_email_subscriptions()
            for subscription in due:
                _start_job("email", {"subscription_id": subscription["id"]})
        except Exception:
            pass
        time.sleep(30)


def _claim_due_email_subscriptions() -> list[dict[str, Any]]:
    now = datetime.now()
    due: list[dict[str, Any]] = []
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        changed = False
        for subscription in data.get("subscriptions", []):
            if not subscription.get("enabled") or subscription.get("running"):
                continue
            next_run = _parse_datetime(subscription.get("next_run_at"))
            if next_run and next_run <= now:
                subscription["running"] = True
                subscription["last_status"] = "Выполняется поиск"
                due.append(dict(subscription))
                changed = True
        if changed:
            _write_email_subscription_data_unlocked(data)
    return due


def _process_email_subscription(subscription_id: str) -> None:
    _run_email_subscription_job(subscription_id)


def _run_email_subscription_job(subscription_id: str) -> dict[str, Any]:
    subscription = _find_email_subscription(subscription_id)
    if not subscription:
        return {"ok": False, "error": "Рассылка не найдена.", "subscription_id": subscription_id}
    try:
        result = _run_email_digest(subscription)
    except Exception as error:  # noqa: BLE001 - background jobs must persist diagnostics.
        result = {
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
            "run_log": [
                f"{datetime.now().isoformat(timespec='seconds')} unhandled error: {type(error).__name__}: {error}",
                traceback.format_exc(),
            ],
        }
    now = datetime.now()
    sent_keys = result.get("sent_keys") if result.get("ok") else []
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        for item in data.get("subscriptions", []):
            if item.get("id") != subscription_id:
                continue
            existing_keys = list(item.get("sent_vacancy_keys") or [])
            for key in sent_keys or []:
                if key and key not in existing_keys:
                    existing_keys.append(key)
            item["sent_vacancy_keys"] = existing_keys[-1000:]
            item["last_run_at"] = now.isoformat(timespec="seconds")
            item["next_run_at"] = (now + _subscription_interval(item)).isoformat(timespec="seconds")
            item["last_status"] = _subscription_status_text(result)
            item["last_error_details"] = _subscription_error_details(result)
            item["last_run_log"] = "\n".join(str(line) for line in result.get("run_log") or [])[-12000:]
            item["running"] = False
            break
        _write_email_subscription_data_unlocked(data)
    return {**result, "subscription_id": subscription_id}


def _subscription_status_text(result: dict[str, Any]) -> str:
    if result.get("ok") and result.get("sent", 0) > 0:
        channels = ", ".join((result.get("delivery") or {}).get("channels") or [])
        channel_text = f" ({channels})" if channels else ""
        warnings = (result.get("delivery") or {}).get("warnings") or []
        warning_text = f"; предупреждения: {'; '.join(warnings[:2])}" if warnings else ""
        return f"Отправлено вакансий: {result.get('sent')}{channel_text}{warning_text}"
    if result.get("ok"):
        return str(result.get("message") or "Новых вакансий нет")
    return f"Ошибка: {result.get('error') or 'не удалось отправить'}"


def _subscription_error_details(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return ""
    payload = {
        "error": result.get("error"),
        "fetch_result": result.get("fetch_result"),
        "rank_result": result.get("rank_result"),
        "delivery": result.get("delivery"),
        "run_log": result.get("run_log"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)[:12000]


def _start_telegram_bot() -> None:
    global TELEGRAM_BOT_STARTED
    if TELEGRAM_BOT_STARTED:
        return
    _load_env_file(PROJECT_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        if _coerce_bool(os.environ.get("REQUIRE_TELEGRAM_BOT"), default=False):
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required when REQUIRE_TELEGRAM_BOT=1.")
        print("Telegram bot disabled: TELEGRAM_BOT_TOKEN is not configured.")
        return
    TELEGRAM_BOT_STARTED = True
    thread = threading.Thread(target=_telegram_bot_start_worker, args=(token,), daemon=True)
    thread.start()
    print("Telegram bot startup scheduled.")


def _telegram_bot_start_worker(token: str) -> None:
    if _coerce_bool(os.environ.get("REQUIRE_TELEGRAM_BOT"), default=False):
        health = _telegram_api_request(token, "getMe", {})
        if not health.get("ok"):
            print(f"Telegram bot health check warning: {health.get('description') or health.get('error') or health}")
    _configure_telegram_bot(token)
    print("Telegram bot polling started.")
    _telegram_bot_loop()


def _configure_telegram_bot(token: str) -> None:
    _telegram_api_request(token, "deleteWebhook", {"drop_pending_updates": False})
    _telegram_api_request(
        token,
        "setMyCommands",
        {
            "commands": [
                {"command": "start", "description": "Открыть пульт"},
                {"command": "menu", "description": "Открыть пульт"},
                {"command": "cancel", "description": "Отменить текущее действие"},
            ]
        },
    )


def _telegram_bot_loop() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    offset = 0
    while True:
        try:
            result = _telegram_api_request(token, "getUpdates", {"timeout": 25, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])})
            if not result.get("ok"):
                time.sleep(5)
                continue
            for update in result.get("result") or []:
                update_id = int(update.get("update_id") or 0)
                offset = max(offset, update_id + 1)
                _handle_telegram_update(token, update)
        except Exception:
            time.sleep(5)


def _handle_telegram_update(token: str, update: dict[str, Any]) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        _handle_telegram_callback(token, callback)
        return
    message = update.get("message")
    if isinstance(message, dict):
        _handle_telegram_message(token, message)


def _handle_telegram_message(token: str, message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return
    user = message.get("from") or {}
    _remember_telegram_user(chat_id, username=str(user.get("username") or chat.get("username") or ""))
    text = " ".join(str(message.get("text") or "").split())
    if not text:
        _telegram_send_control_panel(token, chat_id, "Пришлите текст запроса или выберите действие.")
        return
    dialog = _telegram_get_dialog(chat_id)
    if text.lower() in {"/start", "/menu", "пульт", "меню"}:
        _telegram_clear_dialog(chat_id)
        _telegram_send_control_panel(token, chat_id, "Пульт поиска вакансий")
        return
    if text.lower() in {"/cancel", "отмена"}:
        _telegram_clear_dialog(chat_id)
        _telegram_send_control_panel(token, chat_id, "Действие отменено.")
        return
    if dialog:
        _handle_telegram_dialog_message(token, chat_id, text, dialog)
        return
    _telegram_send_control_panel(token, chat_id, "Выберите действие в пульте.")


def _handle_telegram_callback(token: str, callback: dict[str, Any]) -> None:
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    user = callback.get("from") or {}
    username = str(user.get("username") or chat.get("username") or "").strip()
    callback_id = str(callback.get("id") or "")
    if callback_id:
        _telegram_api_request(token, "answerCallbackQuery", {"callback_query_id": callback_id})
    if not chat_id:
        return
    _remember_telegram_user(chat_id, username=username)
    if data == "panel":
        _telegram_clear_dialog(chat_id)
        _telegram_send_control_panel(token, chat_id, "Пульт поиска вакансий")
    elif data == "subs":
        _telegram_clear_dialog(chat_id)
        _telegram_send_subscriptions(token, chat_id, username=username)
    elif data.startswith("delete_sub:"):
        _telegram_clear_dialog(chat_id)
        _telegram_delete_subscription(token, chat_id, data.split(":", 1)[1], username=username)
    elif data == "add_sub":
        _telegram_set_dialog(chat_id, {"kind": "subscription", "step": "text"})
        _telegram_send_message(token, chat_id, "Опишите пожелания соискателя для рассылки.")
    elif data == "quick":
        _telegram_set_dialog(chat_id, {"kind": "quick", "step": "text"})
        _telegram_send_message(token, chat_id, "Опишите пожелания соискателя для быстрого поиска.")


def _handle_telegram_dialog_message(token: str, chat_id: str, text: str, dialog: dict[str, Any]) -> None:
    kind = dialog.get("kind")
    step = dialog.get("step")
    if kind == "quick":
        _telegram_clear_dialog(chat_id)
        _telegram_send_message(token, chat_id, "Запускаю быстрый поиск. Это может занять пару минут.")
        threading.Thread(target=_telegram_run_quick_search, args=(token, chat_id, text), daemon=True).start()
        return
    if kind != "subscription":
        _telegram_clear_dialog(chat_id)
        _telegram_send_control_panel(token, chat_id, "Диалог сброшен.")
        return
    if step == "text":
        dialog.update({"step": "k", "text": text})
        _telegram_set_dialog(chat_id, dialog)
        _telegram_send_message(token, chat_id, "Сколько вакансий включать в сводку? Например: 5")
    elif step == "k":
        dialog.update({"step": "interval", "k": _bounded_int(text, default=5, minimum=1, maximum=20)})
        _telegram_set_dialog(chat_id, dialog)
        _telegram_send_message(token, chat_id, "Интервал в часах. Например: 24")
    elif step == "interval":
        dialog.update({"step": "send_time", "interval_value": _bounded_int(text, default=24, minimum=1, maximum=365)})
        _telegram_set_dialog(chat_id, dialog)
        _telegram_send_message(token, chat_id, "Когда отправлять первую сводку? Напишите: сразу или потом")
    elif step == "send_time":
        send_now = "потом" not in text.lower() and "later" not in text.lower()
        payload = {
            "text": dialog.get("text"),
            "telegram_recipients": [chat_id],
            "k": dialog.get("k"),
            "interval_value": dialog.get("interval_value"),
            "interval_unit": "hours",
            "send_now": send_now,
        }
        result = _create_email_subscription(payload)
        _telegram_clear_dialog(chat_id)
        if result.get("ok"):
            _telegram_send_control_panel(token, chat_id, "Telegram-рассылка создана.")
        else:
            _telegram_send_control_panel(token, chat_id, f"Не удалось создать рассылку: {result.get('error') or 'ошибка'}")


def _telegram_get_dialog(chat_id: str) -> dict[str, Any]:
    with TELEGRAM_DIALOGS_LOCK:
        return dict(TELEGRAM_DIALOGS.get(chat_id) or {})


def _telegram_set_dialog(chat_id: str, dialog: dict[str, Any]) -> None:
    with TELEGRAM_DIALOGS_LOCK:
        TELEGRAM_DIALOGS[chat_id] = dict(dialog)


def _telegram_clear_dialog(chat_id: str) -> None:
    with TELEGRAM_DIALOGS_LOCK:
        TELEGRAM_DIALOGS.pop(chat_id, None)


def _telegram_send_control_panel(token: str, chat_id: str, text: str) -> None:
    _telegram_send_message(
        token,
        chat_id,
        text,
        reply_markup={
            "inline_keyboard": [
                [{"text": "Мои рассылки", "callback_data": "subs"}],
                [{"text": "Добавить рассылку", "callback_data": "add_sub"}],
                [{"text": "Быстрый поиск", "callback_data": "quick"}],
            ]
        },
    )


def _telegram_send_subscriptions(token: str, chat_id: str, *, username: str = "") -> None:
    subscriptions = _telegram_chat_subscriptions(chat_id, username=username)
    if not subscriptions:
        _telegram_send_control_panel(token, chat_id, "Для этого аккаунта рассылок нет.")
        return
    keyboard = [[{"text": f"Удалить: {_limit_text(item.get('text'), 32)}", "callback_data": f"delete_sub:{item.get('id')}"}] for item in subscriptions[:20]]
    keyboard.append([{"text": "Назад", "callback_data": "panel"}])
    lines = ["Ваши Telegram-рассылки:"]
    for index, item in enumerate(subscriptions, start=1):
        lines.append(
            f"{index}. {_limit_text(item.get('text'), 120)}\n"
            f"{item.get('k') or 0} вакансий, раз в {item.get('interval_value') or '-'} ч.; статус: {item.get('last_status') or 'ожидание'}"
        )
    _telegram_send_message(token, chat_id, "\n\n".join(lines), reply_markup={"inline_keyboard": keyboard})


def _telegram_chat_subscriptions(chat_id: str, *, username: str = "") -> list[dict[str, Any]]:
    account_keys = _telegram_account_keys(chat_id, username)
    return [
        item
        for item in _public_email_subscriptions()
        if account_keys & {str(recipient).strip().lower() for recipient in item.get("telegram_recipients") or []}
    ]


def _telegram_account_keys(chat_id: str, username: str = "") -> set[str]:
    keys = {str(chat_id or "").strip().lower()}
    clean_username = str(username or "").strip().lstrip("@").lower()
    if clean_username:
        keys.add(f"@{clean_username}")
        keys.add(clean_username)
    return {key for key in keys if key}


def _telegram_delete_subscription(token: str, chat_id: str, subscription_id: str, *, username: str = "") -> None:
    subscriptions = _telegram_chat_subscriptions(chat_id, username=username)
    if subscription_id not in {str(item.get("id") or "") for item in subscriptions}:
        _telegram_send_control_panel(token, chat_id, "Рассылка не найдена для этого аккаунта.")
        return
    result = _delete_email_subscription({"id": subscription_id})
    _telegram_send_control_panel(token, chat_id, "Рассылка удалена." if result.get("ok") else f"Не удалось удалить: {result.get('error') or 'ошибка'}")


def _telegram_run_quick_search(token: str, chat_id: str, text: str) -> None:
    try:
        result = _run_quick_search({"text": text})
    except Exception as error:  # noqa: BLE001 - telegram user should get diagnostics.
        _telegram_send_control_panel(token, chat_id, f"Быстрый поиск завершился ошибкой: {type(error).__name__}: {error}")
        return
    if not result.get("ok"):
        _telegram_send_control_panel(token, chat_id, f"Быстрый поиск не завершен: {result.get('error') or result.get('status_label') or 'ошибка'}")
        return
    cards = result.get("card_vacancies") or []
    if not cards:
        _telegram_send_control_panel(token, chat_id, "Быстрый поиск завершен, но карточек вакансий нет.")
        return
    for message in _build_digest_telegram_messages(cards[:5]):
        _telegram_send_message(token, chat_id, message, parse_mode="HTML")
    _telegram_send_control_panel(token, chat_id, f"Быстрый поиск готов. Показано вакансий: {min(len(cards), 5)}.")


def _send_digest_notifications(subscription: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    channels: list[str] = []
    errors: list[str] = []
    run_log: list[str] = []
    emails = subscription.get("emails") or []
    telegram_recipients = subscription.get("telegram_recipients") or []

    if emails:
        try:
            email_result = _send_digest_email(subscription, cards)
        except Exception as error:  # noqa: BLE001 - delivery diagnostics must not stop other channels.
            email_result = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            }
        if email_result.get("ok"):
            channels.append("email")
            run_log.append(f"resend ok: recipients={len(emails)}")
        else:
            errors.append(f"email: {email_result.get('error') or 'не удалось отправить'}")
            run_log.append(f"email failed: {email_result.get('error')}")
            if email_result.get("traceback"):
                run_log.append(str(email_result.get("traceback")))

    if telegram_recipients:
        try:
            telegram_result = _send_digest_telegram(subscription, cards)
        except Exception as error:  # noqa: BLE001 - keep diagnostics and continue reporting.
            telegram_result = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
                "run_log": [],
            }
        if telegram_result.get("ok"):
            channels.append("telegram")
            run_log.append(f"telegram ok: recipients={telegram_result.get('sent_count', 0)}")
        else:
            errors.append(f"telegram: {telegram_result.get('error') or 'не удалось отправить'}")
            run_log.append(f"telegram failed: {telegram_result.get('error')}")
            if telegram_result.get("traceback"):
                run_log.append(str(telegram_result.get("traceback")))
        run_log.extend(str(line) for line in telegram_result.get("run_log") or [])

    if channels:
        result: dict[str, Any] = {"ok": True, "channels": channels, "run_log": run_log}
        if errors:
            result["warnings"] = errors
        return result
    return {"ok": False, "error": "; ".join(errors) or "Не задан ни один канал доставки.", "run_log": run_log}


def _send_digest_email(subscription: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    _load_env_file(PROJECT_ROOT / ".env")
    return _send_digest_resend(subscription, cards)


def _send_digest_resend(subscription: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.environ.get("RESEND_API")
    if not api_key:
        return {"ok": False, "error": "RESEND_API не найден в .env."}
    from_email = _normalize_resend_from(os.environ.get("RESEND_FROM"))
    if not from_email:
        return {
            "ok": False,
            "error": "RESEND_FROM не найден или задан в неверном формате. Для Resend нужен email@example.com или Name <email@example.com>.",
        }
    payload = {
        "from": from_email,
        "to": subscription.get("emails") or [],
        "subject": f"Сводка вакансий: {len(cards)} новых",
        "html": _build_digest_email_html(subscription, cards),
    }
    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AI-Vacancy-Match-Agent/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTPError {error.code}: {error.reason}; body={body[:2000]}"}
    except Exception as error:  # noqa: BLE001 - scheduler must persist status.
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, "resend_response": body}


def _normalize_resend_from(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    email_pattern = r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+"
    if re.fullmatch(email_pattern, text):
        return text
    if re.fullmatch(rf"[^<>]+ <{email_pattern}>", text):
        return text
    if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        return f"Vacancy Finder <digest@{text.lower()}>"
    return ""


def _send_digest_telegram(subscription: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    _load_env_file(PROJECT_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN не найден в .env."}
    recipients = _clean_telegram_recipients(subscription.get("telegram_recipients"))
    if not recipients:
        return {"ok": False, "error": "Telegram-получатели не заданы."}

    errors: list[str] = []
    run_log: list[str] = []
    sent_count = 0
    messages = _build_digest_telegram_messages(cards)
    if not messages:
        return {"ok": False, "error": "Нет карточек для Telegram-отправки.", "run_log": run_log}
    for recipient in recipients:
        chat_id = _resolve_telegram_chat_id(token, recipient)
        if not chat_id:
            errors.append(f"{recipient}: chat_id не найден; пользователь должен открыть бота и отправить сообщение")
            run_log.append(f"telegram resolve failed: recipient={recipient}")
            continue
        recipient_ok = True
        for message_index, text in enumerate(messages, start=1):
            result = _telegram_api_request(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False})
            if result.get("ok"):
                continue
            recipient_ok = False
            description = result.get("description") or result.get("error") or "sendMessage failed"
            errors.append(f"{recipient}: card {message_index}: {description}")
            run_log.append(f"telegram send failed: recipient={recipient} chat_id={chat_id} card={message_index} error={description}")
            break
        if recipient_ok:
            sent_count += 1
            run_log.append(f"telegram send ok: recipient={recipient} chat_id={chat_id} cards={len(messages)}")

    if sent_count:
        return {"ok": True, "sent_count": sent_count, "errors": errors, "run_log": run_log}
    return {"ok": False, "error": "; ".join(errors) or "Telegram-сообщение не отправлено.", "run_log": run_log}


def _build_digest_telegram_messages(cards: list[dict[str, Any]]) -> list[str]:
    return [_build_digest_telegram_card(card, index, len(cards)) for index, card in enumerate(cards, start=1)]


def _build_digest_telegram_text(subscription: dict[str, Any], cards: list[dict[str, Any]]) -> str:
    return "\n\n".join(_build_digest_telegram_messages(cards))[:3900]


def _build_digest_telegram_card(card: dict[str, Any], index: int, total: int) -> str:
    title = _html_escape(str(card.get("title") or card.get("normalized_title") or "Без названия"))
    company = _html_escape(str(card.get("company") or "Компания не указана"))
    link = str(card.get("url") or card.get("link") or "").strip()
    title_part = f'<a href="{_html_attr(link)}">{title}</a>' if link else title
    score = _html_escape(str(card.get("score") or card.get("match_score") or 0))
    salary = _html_escape(_display_salary_line(card.get("salary") or card.get("salary_rub") or card.get("compensation") or card.get("pay")))
    facts = [
        ("Город", _display_optional(card.get("location") or card.get("city") or card.get("region"))),
        ("Формат", _display_work_format(card.get("work_format") or card.get("format") or card.get("employment_type"))),
        ("Английский", _display_optional(card.get("english") or card.get("english_level") or card.get("language"), fallback="?")),
        ("Уровень", _display_level(card.get("level") or card.get("experience") or card.get("experience_level"))),
    ]
    fact_lines = [f"<b>{label}</b>: {_html_escape(value)}" for label, value in facts if value]
    skills = _telegram_skills_text(card)
    description = _limit_text(_email_card_description(card), 1000)
    risks = _limit_text(
        _format_list(card.get("llm_risks") or card.get("llm_score_risks") or card.get("concerns") or card.get("risks"), fallback="Критичных рисков не выявлено."),
        700,
    )
    parts = [
        f"<b>{title_part}</b>",
        company,
        f"<b>Score</b>: {score}",
        salary,
    ]
    if fact_lines:
        parts.extend(["", *fact_lines])
    if skills:
        parts.extend(["", f"<b>Навыки</b>: {_html_escape(skills)}"])
    parts.extend(
        [
            "",
            "<b>Описание (LLM)</b>",
            _html_escape(description),
            "",
            "<b>Риски / минусы</b>",
            _html_escape(risks),
        ]
    )
    return _telegram_fit_message("\n".join(parts))


def _html_escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _html_attr(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _clean_card_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _display_salary_line(value: Any) -> str:
    text = _clean_card_text(value)
    return f"ЗП - {text}" if text else "ЗП - ?"


def _display_optional(value: Any, *, fallback: str = "") -> str:
    return _clean_card_text(value) or fallback


def _display_work_format(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    if re.search(r"remote|удален", text):
        return "Удаленка"
    if re.search(r"hybrid|гибрид", text):
        return "Гибрид"
    if re.search(r"onsite|office|офис|полный день", text):
        return "Офис"
    return _display_optional(value)


def _display_level(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    if re.search(r"intern|internship|стаж[её]р|стажиров", text):
        return "Internship"
    if "entry" in text:
        return "Entry"
    if re.search(r"junior|джун", text):
        return "Junior"
    if re.search(r"middle|мидл", text):
        return "Middle"
    if re.search(r"senior|lead|сеньор|лид", text):
        return "Senior/Lead"
    return _display_optional(value)


def _telegram_skills_text(card: dict[str, Any]) -> str:
    matched = _list_values(card.get("matched_skills"))
    vacancy_skills = _list_values(card.get("vacancy_skills") or card.get("key_skills") or card.get("stack") or card.get("skills") or card.get("extracted_requirements"))
    values = []
    if matched:
        values.extend(matched)
    for skill in vacancy_skills:
        if skill.lower() not in {item.lower() for item in values}:
            values.append(skill)
    return _limit_text(", ".join(values), 260)


def _list_values(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, (tuple, set)):
        raw = list(value)
    else:
        raw = re.split(r"[;,]", str(value or ""))
    return [_clean_card_text(item) for item in raw if _clean_card_text(item)]


def _limit_text(value: Any, limit: int) -> str:
    text = _clean_card_text(value)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut or text[:limit].rstrip()}..."


def _telegram_fit_message(value: str) -> str:
    if len(value) <= 3900:
        return value
    cut = value[:3850].rsplit("\n", 1)[0].rstrip()
    return f"{cut}\n..."


def _resolve_telegram_chat_id(token: str, recipient: str) -> str:
    value = str(recipient or "").strip()
    if re.fullmatch(r"-?\d+", value):
        return value
    username = value.lstrip("@").lower()
    if not username:
        return ""
    cached = _telegram_known_chat_id(username)
    if cached:
        return cached
    updates = _telegram_api_request(token, "getUpdates", {})
    for item in updates.get("result") or []:
        message = item.get("message") or item.get("edited_message") or {}
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        update_chat_id = str(chat.get("id") or user.get("id") or "")
        update_username = str(user.get("username") or chat.get("username") or "").strip()
        if update_chat_id:
            _remember_telegram_user(update_chat_id, username=update_username)
        if update_username.lower() == username:
            return update_chat_id
    return ""


def _remember_telegram_user(chat_id: str, *, username: str = "") -> None:
    clean_chat_id = str(chat_id or "").strip()
    clean_username = str(username or "").strip().lstrip("@").lower()
    if not clean_chat_id or not clean_username:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        users = data.setdefault("telegram_users", {})
        if not isinstance(users, dict):
            users = {}
            data["telegram_users"] = users
        users[clean_username] = {"chat_id": clean_chat_id, "username": clean_username, "updated_at": now}
        users[f"@{clean_username}"] = {"chat_id": clean_chat_id, "username": clean_username, "updated_at": now}
        _write_email_subscription_data_unlocked(data)


def _telegram_known_chat_id(username: str) -> str:
    key = str(username or "").strip().lstrip("@").lower()
    if not key:
        return ""
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        users = data.get("telegram_users") if isinstance(data, dict) else {}
        if not isinstance(users, dict):
            return ""
        record = users.get(key) or users.get(f"@{key}")
        if isinstance(record, dict):
            return str(record.get("chat_id") or "").strip()
        return str(record or "").strip()


def _telegram_bot_info() -> dict[str, Any]:
    _load_env_file(PROJECT_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    configured = _configured_telegram_bot_info()
    if not token:
        return {
            "ok": bool(configured.get("bot_url")),
            "error": "" if configured.get("bot_url") else "TELEGRAM_BOT_TOKEN не найден в .env.",
            **configured,
        }
    result = _telegram_api_request(token, "getMe", {})
    user = result.get("result") if result.get("ok") else {}
    username = str((user or {}).get("username") or "").strip()
    bot_url = f"https://t.me/{username}" if username else str(configured.get("bot_url") or "")
    return {
        "ok": bool(bot_url),
        "username": username or str(configured.get("username") or ""),
        "bot_url": bot_url,
        "error": "" if username else str(result.get("description") or result.get("error") or "Не удалось получить username бота."),
    }


def _configured_telegram_bot_info() -> dict[str, str]:
    raw_url = str(os.environ.get("TELEGRAM_BOT_URL") or "").strip()
    raw_username = str(os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    if raw_url:
        username = raw_url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        return {"username": username, "bot_url": raw_url}
    if raw_username:
        return {"username": raw_username, "bot_url": f"https://t.me/{raw_username}"}
    return {"username": "", "bot_url": ""}


def _telegram_api_request(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    headers = {"User-Agent": "AI-Vacancy-Match-Agent/1.0"}
    if payload and any(isinstance(value, (dict, list)) for value in payload.values()):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        data = urlencode(payload).encode("utf-8") if payload else None
    request = Request(url, data=data, headers=headers, method="POST" if payload else "GET")
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as error:  # noqa: BLE001 - delivery diagnostics are stored in subscription status.
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def _telegram_send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": _telegram_fit_message(text),
        "disable_web_page_preview": False,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _telegram_api_request(token, "sendMessage", payload)


def _build_digest_email_html(subscription: dict[str, Any], cards: list[dict[str, Any]]) -> str:
    dark = str(subscription.get("email_theme") or "light").strip().lower() == "dark"
    palette = {
        "page_bg": "#0f1722" if dark else "#f4f6f8",
        "page_text": "#f5f7fa" if dark else "#111820",
        "muted": "#a8b3c0" if dark else "#42505f",
        "card_bg": "#151f2c" if dark else "#ffffff",
        "card_border": "#344255" if dark else "#d4dbe4",
        "score_bg": "#f2c94c" if dark else "#111820",
        "score_text": "#101820" if dark else "#ffffff",
        "score_border": "#f2c94c" if dark else "#ffdd2d",
        "fact_bg": "#223147" if dark else "#f7f9fb",
        "fact_border": "#344255" if dark else "#e1e6ec",
        "fact_label": "#a8b3c0" if dark else "#627181",
        "description_border": "#344255" if dark else "#e1e6ec",
        "risk_bg": "#2b2028" if dark else "#fff6f6",
        "risk_border": "#6b3942" if dark else "#f0c9c9",
        "risk_label": "#f29ca3" if dark else "#9a2f2f",
        "link": "#f2c94c" if dark else "#111820",
    }
    card_html = "\n".join(_build_digest_card_html(card, palette=palette) for card in cards)
    query = html.escape(str(subscription.get("text") or ""))
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="color-scheme" content="dark light">
  <meta name="supported-color-schemes" content="dark light">
</head>
<body bgcolor="{palette['page_bg']}" style="margin:0;padding:0;background-color:{palette['page_bg']};font-family:Arial,sans-serif;color:{palette['page_text']};">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="{palette['page_bg']}" style="width:100%;border-collapse:collapse;background-color:{palette['page_bg']};">
<tr>
<td align="center" style="padding:24px;">
<div style="max-width:760px;width:100%;margin:0 auto;">
  <h1 style="font-size:24px;margin:0 0 8px;">Новые релевантные вакансии</h1>
  <p style="margin:0 0 18px;color:{palette['muted']};">Запрос: {query}</p>
  {card_html}
</div>
</td>
</tr>
</table>
</body></html>"""


def _build_digest_card_html(card: dict[str, Any], *, palette: dict[str, str]) -> str:
    title = html.escape(str(card.get("title") or card.get("normalized_title") or "Без названия"))
    company = html.escape(str(card.get("company") or "Компания не указана"))
    url = html.escape(str(card.get("url") or card.get("link") or ""))
    score = html.escape(str(card.get("score") or card.get("match_score") or 0))
    salary = html.escape(str(card.get("salary") or card.get("salary_rub") or "ЗП не указана"))
    location = html.escape(str(card.get("location") or card.get("city") or "Город не указан"))
    work_format = html.escape(str(card.get("work_format") or card.get("format") or "Формат не указан"))
    description = html.escape(_email_card_description(card))
    risks = html.escape(_format_list(card.get("llm_risks") or card.get("llm_score_risks") or card.get("concerns") or card.get("risks"), fallback="Критичных рисков не выявлено."))
    title_html = f'<a href="{url}" style="color:{palette["page_text"]};text-decoration:none;">{title}</a>' if url else title
    link_html = f'<a href="{url}" style="display:inline-block;margin-top:12px;color:{palette["link"]};font-weight:700;">Открыть вакансию</a>' if url else ""
    return f"""
  <article style="background:{palette['card_bg']};border:1px solid {palette['card_border']};border-radius:18px;padding:20px;margin:0 0 16px;">
    <div style="display:flex;justify-content:space-between;gap:14px;align-items:flex-start;">
      <div>
        <h2 style="font-size:22px;line-height:1.15;margin:0 0 8px;">{title_html}</h2>
        <div style="font-size:15px;font-weight:700;color:{palette['muted']};">{company}</div>
      </div>
      <div style="white-space:nowrap;background:{palette['score_bg']};color:{palette['score_text']};border:2px solid {palette['score_border']};border-radius:999px;padding:8px 12px;font-weight:800;">Score: {score}</div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0;">
      <div style="background:{palette['fact_bg']};border:1px solid {palette['fact_border']};border-radius:10px;padding:10px;"><b style="display:block;font-size:11px;color:{palette['fact_label']};">Город</b>{location}</div>
      <div style="background:{palette['fact_bg']};border:1px solid {palette['fact_border']};border-radius:10px;padding:10px;"><b style="display:block;font-size:11px;color:{palette['fact_label']};">Формат</b>{work_format}</div>
      <div style="background:{palette['fact_bg']};border:1px solid {palette['fact_border']};border-radius:10px;padding:10px;"><b style="display:block;font-size:11px;color:{palette['fact_label']};">Зарплата</b>{salary}</div>
    </div>
    <div style="border:1px solid {palette['description_border']};border-radius:12px;padding:12px;margin-bottom:10px;"><b style="display:block;font-size:12px;color:{palette['fact_label']};margin-bottom:6px;">Описание</b>{description}</div>
    <div style="border:1px solid {palette['risk_border']};background:{palette['risk_bg']};border-radius:12px;padding:12px;"><b style="display:block;font-size:12px;color:{palette['risk_label']};margin-bottom:6px;">Риски / минусы</b>{risks}</div>
    {link_html}
  </article>"""


def _email_card_description(card: dict[str, Any]) -> str:
    for key in ("llm_explanation_comment", "llm_comment", "llm_rank_comment", "why_fit", "description", "summary"):
        value = " ".join(str(card.get(key) or "").split())
        if value:
            return value[:900]
    return "Описание не найдено."


def _format_list(value: Any, *, fallback: str = "нет") -> str:
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item or "").strip()]
    elif isinstance(value, (tuple, set)):
        cleaned = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        text = str(value or "").strip()
        cleaned = [text] if text else []
    return ", ".join(cleaned) if cleaned else fallback


def _vacancy_digest_key(card: dict[str, Any]) -> str:
    value = str(card.get("url") or card.get("link") or "").strip().lower()
    if value:
        return value
    title = str(card.get("title") or card.get("normalized_title") or "").strip().lower()
    company = str(card.get("company") or "").strip().lower()
    return f"{title}|{company}"


def _clean_emails(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value or ""))
    emails: list[str] = []
    seen: set[str] = set()
    for item in raw:
        email = str(item or "").strip().lower()
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) and email not in seen:
            seen.add(email)
            emails.append(email)
    return emails[:10]


def _clean_telegram_recipients(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value or ""))
    recipients: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        if text.startswith("https://t.me/"):
            text = "@" + text.rstrip("/").rsplit("/", 1)[-1]
        if not (re.fullmatch(r"-?\d{5,}", text) or re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", text)):
            continue
        if not text.startswith("@") and not re.fullmatch(r"-?\d+", text):
            text = f"@{text}"
        key = text.lower()
        if key not in seen:
            seen.add(key)
            recipients.append(text)
    return recipients[:10]


def _public_email_subscriptions() -> list[dict[str, Any]]:
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        return [_public_email_subscription(item) for item in data.get("subscriptions", [])]


def _public_email_subscription(subscription: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": subscription.get("id"),
        "text": subscription.get("text"),
        "emails": subscription.get("emails") or [],
        "telegram_recipients": subscription.get("telegram_recipients") or [],
        "email_theme": subscription.get("email_theme") or "light",
        "interval_value": subscription.get("interval_value"),
        "interval_unit": subscription.get("interval_unit"),
        "k": subscription.get("k"),
        "enabled": bool(subscription.get("enabled")),
        "created_at": subscription.get("created_at") or "",
        "next_run_at": subscription.get("next_run_at") or "",
        "last_run_at": subscription.get("last_run_at") or "",
        "last_status": subscription.get("last_status") or "",
        "last_error_details": subscription.get("last_error_details") or "",
        "last_run_log": subscription.get("last_run_log") or "",
        "sent_count": len(subscription.get("sent_vacancy_keys") or []),
        "running": bool(subscription.get("running")),
    }


def _find_email_subscription(subscription_id: str) -> dict[str, Any] | None:
    with SUBSCRIPTIONS_LOCK:
        data = _load_email_subscription_data_unlocked()
        for subscription in data.get("subscriptions", []):
            if subscription.get("id") == subscription_id:
                return dict(subscription)
    return None


def _load_email_subscription_data_unlocked() -> dict[str, Any]:
    if not SUBSCRIPTIONS_PATH.exists():
        return {"subscriptions": []}
    try:
        data = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"subscriptions": []}
    if not isinstance(data, dict) or not isinstance(data.get("subscriptions"), list):
        return {"subscriptions": []}
    return data


def _write_email_subscription_data_unlocked(data: dict[str, Any]) -> None:
    SUBSCRIPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _subscription_interval(subscription: dict[str, Any]) -> timedelta:
    value = _bounded_int(subscription.get("interval_value"), default=24, minimum=1, maximum=365)
    if str(subscription.get("interval_unit") or "") == "days":
        return timedelta(days=value)
    return timedelta(hours=value)


def _quick_search_plan(user_text: str) -> dict[str, Any]:
    _load_env_file(PROJECT_ROOT / ".env")
    llm_client = LLMClient.from_env()
    if not llm_client.enabled:
        return {"ok": False, "error": f"LLM недоступна: {llm_client.reason}"}

    sources = _quick_allowed_sources()
    result = llm_client.json_task(
        stage="quick_search_plan",
        system_prompt=(
            "You convert a user's natural-language job search request into a safe vacancy collection and ranking plan. "
            "Return only valid JSON matching expected_json_shape. No markdown or prose. "
            "Quick search uses staged hard-filter relaxation: first all hard_filters, then a compact 2-3 filter subset, then a 1-2 filter subset, then no hard filters. "
            "Put explicit collection constraints into hard_filters; the application will relax them automatically if strict stages do not collect enough unique vacancies. "
            "Also put preferences and ranking signals into criteria so ranking can explain tradeoffs. "
            "Do not invent skills, cities, salary, English level or sources that the user did not mention. "
            "Build keywords only after filling hard_filters and criteria. "
            "Keywords must be 1-3 concise role/title search queries likely to retrieve vacancies; include Russian/English role variants only when useful. "
            "Never duplicate structured constraints inside keywords: if seniority, city, format, salary, salary visibility, schedule, contract type or company accreditation is present in hard_filters or criteria, it must be absent from keywords. "
            "Keywords should answer only 'what job title/role should the job board search for?', not 'which filters should be applied?'. "
            "Bad keywords example: ['фронтенд разработчик middle senior', 'frontend developer Москва офис']; good keywords example for the same request: ['фронтенд разработчик', 'frontend developer']. "
            "For quick search the only allowed sources are hh and superjob. "
            "Defaults unless explicitly overridden: max_vacancies=50, allowed quick sources, default priorities, use_llm_html=false, rank_mode=llm, top_k=15."
        ),
        payload={
            "user_text": user_text[:4000],
            "allowed_sources": sources,
            "default_source_priorities": _default_source_priorities(sources),
            "criteria_columns": _criteria_columns("criteria"),
            "filter_columns": _criteria_columns("filter"),
            "planning_rules": {
                "keywords": (
                    "Role/title terms only. Strip any words already represented by hard_filters or criteria, including city names, remote/hybrid/office, "
                    "junior/middle/senior/lead, salary numbers, 'with salary', schedule, contract type and accreditation. "
                    "Prefer broad role phrases: 'фронтенд разработчик', 'frontend developer'."
                ),
                "hard_filters": (
                    "Structured collection constraints stated by the user: levels, formats, cities, min_salary, search_fields, salary_defined, working_hours, employment_contract, accredited_it. "
                    "These values must not be repeated in keywords."
                ),
                "criteria": (
                    "Ranking preferences and explanations. Values can mirror hard_filters, but they still must not be repeated in keywords unless they are part of the role title itself."
                ),
                "deduplication_rule": "Every semantic requirement should live in exactly one search layer: role/title in keywords; filters/preferences in hard_filters/criteria.",
            },
            "expected_json_shape": {
                "keywords": ["search query"],
                "hard_filters": {column: "string" for column in _criteria_columns("filter")},
                "criteria": {column: "string" for column in _criteria_columns("criteria")},
                "max_vacancies": 50,
                "sources": sources,
                "source_priorities": {"hh": "high"},
                "use_llm_html": False,
                "rank_mode": "llm|dry_run",
                "top_k": 15,
                "filter_name": "short Russian file name",
                "filter_description": "Russian description",
                "criteria_name": "short Russian file name",
                "criteria_description": "Russian description",
            },
        },
    )
    plan = result if isinstance(result, dict) else {}
    cleaned_sources = [source for source in _clean_sources(plan.get("sources")) if source in set(sources)] or sources
    raw_keywords = _clean_keywords(plan.get("keywords")) or _clean_keywords(plan.get("search_queries")) or [user_text[:120]]
    max_vacancies = _bounded_int(plan.get("max_vacancies"), default=50, minimum=1, maximum=50)
    top_k = _bounded_int(plan.get("top_k"), default=15, minimum=1, maximum=20)
    rank_mode = "llm" if str(plan.get("rank_mode") or "").lower() in {"llm", "auto + llm", "with_llm", "с llm"} else "dry_run"
    hard_filters = _coerce_generated_criteria(plan.get("hard_filters"), kind="filter")
    criteria = _coerce_generated_criteria(plan.get("criteria"), kind="criteria")
    keywords = _sanitize_quick_keywords(raw_keywords, hard_filters, criteria, fallback=user_text)
    priorities = _default_source_priorities(cleaned_sources)
    if isinstance(plan.get("source_priorities"), dict):
        for source, priority in plan["source_priorities"].items():
            source_key = str(source or "").strip().lower()
            priority_value = str(priority or "").strip().lower()
            if source_key in priorities and priority_value in {"high", "medium", "low"}:
                priorities[source_key] = priority_value
    return {
        "ok": True,
        "plan": {
            "keywords": keywords[:5],
            "hard_filters": hard_filters,
            "criteria": criteria,
            "max_vacancies": max_vacancies,
            "sources": cleaned_sources,
            "source_priorities": priorities,
            "use_llm_html": _coerce_bool(plan.get("use_llm_html"), default=False),
            "rank_mode": "llm",
            "top_k": top_k,
            "filter_name": _sanitize_metadata_text(plan.get("filter_name"), limit=80) or "Быстрый поиск: фильтры",
            "filter_description": _sanitize_metadata_text(plan.get("filter_description"), limit=300) or "Жесткие фильтры из быстрого поиска.",
            "criteria_name": _sanitize_metadata_text(plan.get("criteria_name"), limit=80) or "Быстрый поиск: критерии",
            "criteria_description": _sanitize_metadata_text(plan.get("criteria_description"), limit=300) or "Критерии ранжирования из быстрого поиска.",
        },
    }


def _quick_allowed_sources() -> list[str]:
    return list(QUICK_ALLOWED_SOURCES)


def _normalize_quick_plan(plan: dict[str, Any]) -> dict[str, Any]:
    allowed = set(_quick_allowed_sources())
    sources = [source for source in _clean_sources(plan.get("sources")) if source in allowed] or _quick_allowed_sources()
    priorities = _default_source_priorities(sources)
    if isinstance(plan.get("source_priorities"), dict):
        for source, priority in plan["source_priorities"].items():
            source_key = str(source or "").strip().lower()
            priority_value = str(priority or "").strip().lower()
            if source_key in priorities and priority_value in {"high", "medium", "low"}:
                priorities[source_key] = priority_value
    plan["sources"] = sources
    plan["source_priorities"] = priorities
    plan["use_llm_html"] = False
    plan["rank_mode"] = "llm"
    plan["max_vacancies"] = _bounded_int(plan.get("max_vacancies"), default=50, minimum=1, maximum=50)
    plan["keywords"] = _sanitize_quick_keywords(
        _clean_keywords(plan.get("keywords")) or _clean_keywords(plan.get("search_queries")),
        _quick_fetch_filters(plan.get("hard_filters") or {}),
        plan.get("criteria") or {},
        fallback=", ".join(_clean_keywords(plan.get("keywords")) or _clean_keywords(plan.get("search_queries"))),
    )
    return plan


def _non_empty_criteria_values(values: dict[str, str]) -> dict[str, str]:
    return {key: str(value).strip() for key, value in (values or {}).items() if str(value or "").strip()}


def _sanitize_quick_keywords(keywords: list[str], hard_filters: dict[str, str], criteria: dict[str, str], *, fallback: str) -> list[str]:
    removal_values: list[str] = []
    for source in (hard_filters or {}, criteria or {}):
        for key in ("preferred_levels", "preferred_formats", "preferred_cities", "min_salary", "salary_defined", "working_hours", "employment_contract", "accredited_it"):
            removal_values.extend(_split_report_values(source.get(key)))
    removal_values.extend(["office", "офис", "onsite", "on site", "middle", "senior", "junior", "lead", "мидл", "сеньор", "джуниор"])
    removal_patterns = [_keyword_removal_pattern(value) for value in removal_values if _keyword_removal_pattern(value)]

    cleaned: list[str] = []
    for keyword in keywords or []:
        text = str(keyword or "")
        for pattern in removal_patterns:
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" ,;")
        if text:
            cleaned.append(text[:120])
    cleaned = _clean_keywords(cleaned)
    if cleaned:
        return cleaned[:5]

    fallback_roles = _split_report_values((criteria or {}).get("target_roles"))
    fallback_keywords = _clean_keywords(fallback_roles) or _clean_keywords([fallback])
    return fallback_keywords[:5] or ["вакансия"]


def _split_report_values(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[;,|]+", str(value or "")) if part.strip()]


def _keyword_removal_pattern(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.lower().replace("ё", "е")
    if normalized in {"true", "yes", "1", "at_least", "defined", "any", "permanent", "full_day"}:
        return ""
    escaped = re.escape(text)
    return rf"(?<![\wа-яА-Я]){escaped}(?![\wа-яА-Я])"


def _quick_fetch_filters(values: dict[str, str]) -> dict[str, str]:
    return dict(values or {})


def _run_staged_quick_fetch(
    *,
    plan: dict[str, Any],
    all_filters: dict[str, str],
    all_filter_path: Path,
    job_id: str | None = None,
    progress_start: int = 16,
    progress_end: int = 72,
) -> dict[str, Any]:
    target_rows = _bounded_int(plan.get("max_vacancies"), default=50, minimum=1, maximum=50)
    stages = _quick_filter_stages(all_filters)
    collected_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    stage_results: list[dict[str, Any]] = []
    request_log: list[dict[str, Any]] = []
    warnings: list[str] = []
    fetch_metrics = {"total_considered": 0, "html_without_llm": 0, "html_with_llm": 0, "html_mixed": 0}
    source_stats: dict[str, dict[str, Any]] = {}
    stage_count = max(1, len(stages))

    for index, stage in enumerate(stages):
        if len(collected_rows) >= target_rows:
            break
        stage_start = progress_start + round((progress_end - progress_start) * index / stage_count)
        stage_end = progress_start + round((progress_end - progress_start) * (index + 1) / stage_count)
        filters = stage["filters"]
        filter_path = all_filter_path
        if filters != _non_empty_criteria_values(all_filters):
            filter_path = _write_quick_csv(
                "filter",
                filters,
                name=f"{plan.get('filter_name') or 'Быстрый поиск'}: {stage['title']}",
                description=f"Стадия staged-сбора: {stage['description']}",
            )
        _update_job(job_id, progress=stage_start, stage=f"Сбор вакансий: {stage['title']}")
        result = _run_fetch(
            {
                "sources": plan["sources"],
                "source_priorities": plan["source_priorities"],
                "keywords": plan["keywords"],
                "max_vacancies": target_rows,
                "use_llm_html": False,
                "llm_max_limit": 100,
                "criteria": _relative(filter_path),
                "hard_filters": bool(filters),
                "timeout": 900,
                "pages": 2,
                "per_page": 20,
                "command_timeout": 900,
                "_disable_relaxed_completion": True,
                "progress_start": stage_start,
                "progress_end": stage_end,
            },
            job_id=job_id,
        )
        rows = _read_vacancy_csv_rows(_safe_project_path(result.get("created_path", "")))
        added = 0
        for row in rows:
            key = _vacancy_merge_key(row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            collected_rows.append(row)
            added += 1
            if len(collected_rows) >= target_rows:
                break
        trace = result.get("trace_summary") or {}
        request_log.extend(item for item in trace.get("request_log", []) if isinstance(item, dict))
        warnings.extend(str(item) for item in trace.get("warnings", []) if str(item).strip())
        _merge_fetch_metrics(fetch_metrics, trace.get("fetch_metrics") or {})
        _merge_trace_source_stats(source_stats, trace.get("source_stats") or {})
        stage_results.append(
            {
                "stage": stage["key"],
                "title": stage["title"],
                "description": stage["description"],
                "filters": filters,
                "filter_count": len(filters),
                "filter_path": _relative(filter_path),
                "created_path": result.get("created_path", ""),
                "ok": bool(result.get("ok")),
                "status": result.get("status", ""),
                "rows": len(rows),
                "added_rows": added,
                "cumulative_rows": len(collected_rows),
                "skipped": False,
                "error": result.get("error", ""),
                "command": result.get("command", ""),
                "stdout": str(result.get("stdout") or "")[-3000:],
                "stderr": str(result.get("stderr") or "")[-3000:],
                "returncode": result.get("returncode"),
            }
        )

    for stage in stages[len(stage_results):]:
        stage_results.append(
            {
                "stage": stage["key"],
                "title": stage["title"],
                "description": stage["description"],
                "filters": stage["filters"],
                "filter_count": len(stage["filters"]),
                "filter_path": "",
                "created_path": "",
                "ok": True,
                "status": "skipped",
                "rows": 0,
                "added_rows": 0,
                "cumulative_rows": len(collected_rows),
                "skipped": True,
                "skip_reason": "target reached",
            }
        )

    if not collected_rows:
        last_result = next((item for item in reversed(stage_results) if item.get("created_path")), {})
        last_error = next((str(item.get("error") or "").strip() for item in reversed(stage_results) if str(item.get("error") or "").strip()), "")
        error = _staged_fetch_empty_error(request_log=request_log, warnings=warnings, last_error=last_error)
        trace = {"staged_fetch": stage_results, "request_log": request_log, "warnings": warnings, "source_stats": source_stats, "fetch_metrics": fetch_metrics}
        trace_path = _staged_failure_trace_path()
        trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        if last_error:
            error = f"{error} Last fetch error: {last_error}"
        return {
            "ok": False,
            "status": "empty",
            "status_label": "Данных нет",
            "error": error,
            "rows": 0,
            "created_path": "",
            "trace_path": _relative(trace_path),
            "trace_summary": trace,
            "source_breakdown": {},
            "staged_fetch": stage_results,
            "last_created_path": last_result.get("created_path", ""),
        }

    output_path = _staged_vacancies_path()
    write_vacancies_csv(collected_rows[:target_rows], output_path)
    source_breakdown = _csv_source_breakdown(output_path)
    total_stage_rows = sum(int(stage.get("rows") or 0) for stage in stage_results)
    if not fetch_metrics.get("total_considered"):
        fetch_metrics["total_considered"] = total_stage_rows
    trace = {
        "queries": plan.get("keywords") or [],
        "max_vacancies": target_rows,
        "raw_rows": total_stage_rows,
        "unique_rows": len(collected_rows[:target_rows]),
        "request_log": request_log,
        "warnings": warnings,
        "staged_fetch": stage_results,
        "source_breakdown": source_breakdown,
        "source_stats": source_stats,
        "fetch_metrics": fetch_metrics,
    }
    trace_path = output_path.with_suffix(".trace.json")
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    status = _fetch_status(returncode=0, rows=len(collected_rows[:target_rows]), trace_summary=trace)
    return {
        "ok": status["ok"],
        "command": "staged quick fetch",
        "stdout": "",
        "stderr": "",
        "returncode": 0,
        "command_ok": True,
        **status,
        "created_path": _relative(output_path),
        "trace_path": _relative(trace_path),
        "rows": len(collected_rows[:target_rows]),
        "trace_summary": trace,
        "source_breakdown": source_breakdown,
        "staged_fetch": stage_results,
    }


def _run_relaxed_fetch_completion(
    *,
    payload: dict[str, Any],
    base_result: dict[str, Any],
    criteria_path: Path,
    target_rows: int,
    job_id: str | None = None,
    progress_start: int = 0,
    progress_end: int = 100,
) -> dict[str, Any]:
    all_filters = _read_filter_csv_values(criteria_path)
    stages = _quick_filter_stages(all_filters)
    if stages and stages[0].get("key") == "all":
        stages = stages[1:]
    if not stages:
        return base_result

    collected_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    base_rows = _read_vacancy_csv_rows(_safe_project_path(base_result.get("created_path", "")))
    for row in base_rows:
        key = _vacancy_merge_key(row)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        collected_rows.append(row)
        if len(collected_rows) >= target_rows:
            return base_result

    stage_results: list[dict[str, Any]] = [
        {
            "stage": "all",
            "title": "Все фильтры",
            "description": "Первичный строгий сбор расширенного поиска.",
            "filters": _non_empty_criteria_values(all_filters),
            "filter_count": len(_non_empty_criteria_values(all_filters)),
            "filter_path": _relative(criteria_path),
            "created_path": base_result.get("created_path", ""),
            "ok": bool(base_result.get("ok")),
            "status": base_result.get("status", ""),
            "rows": len(base_rows),
            "added_rows": len(collected_rows),
            "cumulative_rows": len(collected_rows),
            "skipped": False,
        }
    ]
    request_log: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_stats: dict[str, dict[str, Any]] = {}
    fetch_metrics = {"total_considered": 0, "html_without_llm": 0, "html_with_llm": 0, "html_mixed": 0}
    base_trace = base_result.get("trace_summary") or {}
    request_log.extend(item for item in base_trace.get("request_log", []) if isinstance(item, dict))
    warnings.extend(str(item) for item in base_trace.get("warnings", []) if str(item).strip())
    _merge_fetch_metrics(fetch_metrics, base_trace.get("fetch_metrics") or {})
    _merge_trace_source_stats(source_stats, base_trace.get("source_stats") or {})

    stage_count = max(1, len(stages))
    for index, stage in enumerate(stages):
        if len(collected_rows) >= target_rows:
            break
        filters = stage.get("filters") or {}
        stage_start = progress_start + round((progress_end - progress_start) * index / stage_count)
        stage_end = progress_start + round((progress_end - progress_start) * (index + 1) / stage_count)
        filter_path = criteria_path
        if filters != _non_empty_criteria_values(all_filters):
            filter_path = _write_quick_csv(
                "filter",
                filters,
                name=f"Расширенный поиск: {stage['title']}",
                description=f"Добор при недоборе строгих фильтров: {stage['description']}",
            )
        _update_job(job_id, progress=stage_start, stage=f"Добор вакансий: {stage['title']}")
        result = _run_fetch(
            {
                **payload,
                "criteria": _relative(filter_path),
                "hard_filters": bool(filters),
                "max_vacancies": target_rows,
                "timeout": 900,
                "pages": 2,
                "per_page": 20,
                "command_timeout": 900,
                "progress_start": stage_start,
                "progress_end": stage_end,
                "_disable_relaxed_completion": True,
            },
            job_id=job_id,
        )
        rows = _read_vacancy_csv_rows(_safe_project_path(result.get("created_path", "")))
        added = 0
        for row in rows:
            key = _vacancy_merge_key(row)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            collected_rows.append(row)
            added += 1
            if len(collected_rows) >= target_rows:
                break
        trace = result.get("trace_summary") or {}
        request_log.extend(item for item in trace.get("request_log", []) if isinstance(item, dict))
        warnings.extend(str(item) for item in trace.get("warnings", []) if str(item).strip())
        _merge_fetch_metrics(fetch_metrics, trace.get("fetch_metrics") or {})
        _merge_trace_source_stats(source_stats, trace.get("source_stats") or {})
        stage_results.append(
            {
                "stage": stage["key"],
                "title": stage["title"],
                "description": stage["description"],
                "filters": filters,
                "filter_count": len(filters),
                "filter_path": _relative(filter_path),
                "created_path": result.get("created_path", ""),
                "ok": bool(result.get("ok")),
                "status": result.get("status", ""),
                "rows": len(rows),
                "added_rows": added,
                "cumulative_rows": len(collected_rows),
                "skipped": False,
                "error": result.get("error", ""),
            }
        )

    for stage in stages[len(stage_results) - 1:]:
        if len(collected_rows) < target_rows:
            break
        stage_results.append(
            {
                "stage": stage["key"],
                "title": stage["title"],
                "description": stage["description"],
                "filters": stage["filters"],
                "filter_count": len(stage["filters"]),
                "filter_path": "",
                "created_path": "",
                "ok": True,
                "status": "skipped",
                "rows": 0,
                "added_rows": 0,
                "cumulative_rows": len(collected_rows),
                "skipped": True,
                "skip_reason": "target reached",
            }
        )

    if len(collected_rows) <= len(base_rows):
        return base_result

    output_path = _relaxed_vacancies_path()
    write_vacancies_csv(collected_rows[:target_rows], output_path)
    source_breakdown = _csv_source_breakdown(output_path)
    total_stage_rows = sum(int(stage.get("rows") or 0) for stage in stage_results)
    if not fetch_metrics.get("total_considered"):
        fetch_metrics["total_considered"] = total_stage_rows
    warnings.append(
        f"Недобор по жестким фильтрам: строгий сбор дал {len(base_rows)} из {target_rows}; "
        f"добрано до {len(collected_rows[:target_rows])} через менее строгую фильтрацию."
    )
    trace = {
        **base_trace,
        "max_vacancies": target_rows,
        "raw_rows": total_stage_rows,
        "unique_rows": len(collected_rows[:target_rows]),
        "request_log": request_log,
        "warnings": warnings,
        "staged_fetch": stage_results,
        "relaxed_hard_filters": True,
        "relaxed_notice": warnings[-1],
        "source_breakdown": source_breakdown,
        "source_stats": source_stats,
        "fetch_metrics": fetch_metrics,
    }
    trace_path = output_path.with_suffix(".trace.json")
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    status = _fetch_status(returncode=0, rows=len(collected_rows[:target_rows]), trace_summary=trace)
    return {
        **base_result,
        "ok": status["ok"],
        **status,
        "status_label": "Добрано мягче",
        "created_path": _relative(output_path),
        "trace_path": _relative(trace_path),
        "rows": len(collected_rows[:target_rows]),
        "trace_summary": trace,
        "source_breakdown": source_breakdown,
        "staged_fetch": stage_results,
        "relaxed_hard_filters": True,
        "relaxed_notice": warnings[-1],
    }


def _quick_filter_stages(filters: dict[str, str]) -> list[dict[str, Any]]:
    all_filters = _non_empty_criteria_values(filters)
    if not all_filters:
        return [
            {
                "key": "none",
                "title": "Без фильтров",
                "description": "Сбор без жестких фильтров.",
                "filters": {},
            }
        ]
    candidates = [
        ("all", "Все фильтры", "Сбор с полным набором жестких фильтров.", all_filters),
        ("medium", "2-3 фильтра", "Добор с наиболее важными 2-3 жесткими фильтрами.", _select_quick_stage_filters(all_filters, 3)),
        ("light", "1-2 фильтра", "Добор с наиболее важными 1-2 жесткими фильтрами.", _select_quick_stage_filters(all_filters, 2)),
        ("none", "Без фильтров", "Остаточный добор без жестких фильтров.", {}),
    ]
    stages: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for key, title, description, stage_filters in candidates:
        signature = tuple(sorted((str(k), str(v)) for k, v in stage_filters.items()))
        if signature in seen:
            continue
        seen.add(signature)
        stages.append({"key": key, "title": title, "description": description, "filters": stage_filters})
    return stages


def _select_quick_stage_filters(filters: dict[str, str], limit: int) -> dict[str, str]:
    priority = [
        "stop_words",
        "preferred_cities",
        "preferred_levels",
        "preferred_formats",
        "min_salary",
        "salary_defined",
        "search_fields",
        "working_hours",
        "employment_contract",
        "accredited_it",
        "english_level",
    ]
    selected: dict[str, str] = {}
    for key in priority:
        value = str(filters.get(key) or "").strip()
        if value:
            selected[key] = value
        if len(selected) >= limit:
            return selected
    for key, value in filters.items():
        if key in selected:
            continue
        if str(value or "").strip():
            selected[key] = str(value).strip()
        if len(selected) >= limit:
            break
    return selected


def _merge_fetch_metrics(target: dict[str, int], source: dict[str, Any]) -> None:
    for key in ("total_considered", "html_without_llm", "html_with_llm", "html_mixed"):
        try:
            target[key] = int(target.get(key) or 0) + int(source.get(key) or 0)
        except (TypeError, ValueError):
            continue


def _merge_trace_source_stats(target: dict[str, dict[str, Any]], source: dict[str, Any]) -> None:
    for source_name, stats in source.items():
        if not isinstance(stats, dict):
            continue
        target_item = target.setdefault(
            str(source_name),
            {"vacancies": 0, "requests": 0, "successful_requests": 0, "limit": 0, "priority": stats.get("priority", "medium")},
        )
        for key in ("vacancies", "requests", "successful_requests", "limit"):
            try:
                target_item[key] = int(target_item.get(key) or 0) + int(stats.get(key) or 0)
            except (TypeError, ValueError):
                continue
        if stats.get("priority"):
            target_item["priority"] = stats.get("priority")


def _read_vacancy_csv_rows(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as file:
        for row in csv.DictReader(file):
            if isinstance(row, dict):
                rows.append({column: str(row.get(column) or "").strip() for column in VACANCY_COLUMNS})
    return rows


def _read_filter_csv_values(path: Path | None) -> dict[str, str]:
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return {}
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            if isinstance(row, dict):
                return {
                    str(key or "").strip().lstrip("\ufeff"): str(value or "").strip()
                    for key, value in row.items()
                    if str(key or "").strip() and str(value or "").strip()
                }
    return {}


def _vacancy_merge_key(vacancy: dict[str, Any]) -> str:
    source = _base_source_name(vacancy.get("source") or "")
    vacancy_id = str(vacancy.get("vacancy_id") or "").strip().lower()
    link = str(vacancy.get("link") or "").strip().lower().split("?")[0]
    if vacancy_id or link:
        return f"{source}|{vacancy_id}|{link}"
    title = re.sub(r"\s+", " ", str(vacancy.get("title") or "").lower()).strip()
    company = re.sub(r"\s+", " ", str(vacancy.get("company") or vacancy.get("employer_name") or "").lower()).strip()
    city = re.sub(r"\s+", " ", str(vacancy.get("city") or "").lower()).strip()
    return f"{source}|{title}|{company}|{city}"


def _staged_vacancies_path() -> Path:
    output_dir = PROJECT_ROOT / "data" / "collected"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = output_dir / f"vacancies_staged_{timestamp}.csv"
    counter = 1
    while candidate.exists():
        candidate = output_dir / f"vacancies_staged_{timestamp}_{counter}.csv"
        counter += 1
    return candidate


def _relaxed_vacancies_path() -> Path:
    output_dir = PROJECT_ROOT / "data" / "collected"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = output_dir / f"vacancies_relaxed_{timestamp}.csv"
    counter = 1
    while candidate.exists():
        candidate = output_dir / f"vacancies_relaxed_{timestamp}_{counter}.csv"
        counter += 1
    return candidate


def _staged_failure_trace_path() -> Path:
    output_dir = PROJECT_ROOT / "data" / "collected"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = output_dir / f"vacancies_staged_failed_{timestamp}.trace.json"
    counter = 1
    while candidate.exists():
        candidate = output_dir / f"vacancies_staged_failed_{timestamp}_{counter}.trace.json"
        counter += 1
    return candidate


def _staged_fetch_empty_error(*, request_log: list[dict[str, Any]], warnings: list[str], last_error: str) -> str:
    prefix = "Staged fetch produced no unique vacancies."
    details: list[str] = []
    if last_error:
        details.append(f"Last fetch error: {last_error}")
    failed_requests = [item for item in request_log if isinstance(item, dict) and not item.get("ok")]
    if failed_requests:
        reasons: list[str] = []
        for item in failed_requests[:3]:
            source = str(item.get("source") or "unknown")
            method = str(item.get("method") or "request")
            reason = str(item.get("error") or item.get("status") or item.get("reason") or "failed").strip()
            reasons.append(f"{source}/{method}: {reason}")
        details.append(f"Request failures: {'; '.join(reasons)}")
    elif warnings:
        details.append(f"Warning: {str(warnings[0]).splitlines()[0][:300]}")
    elif request_log:
        sampled = []
        for item in request_log[:3]:
            source = str(item.get('source') or 'unknown')
            method = str(item.get('method') or 'request')
            status = str(item.get('status') or 'no-status')
            sampled.append(f"{source}/{method}: {status}")
        details.append(f"Requests completed but returned no rows: {'; '.join(sampled)}")
    return f"{prefix} {' '.join(details)}".strip()


def _quick_parameters_report(plan: dict[str, Any], fetch_filters: dict[str, str]) -> str:
    lines = [
        f"Ключевые запросы: {_join_report_values(plan.get('keywords'))}",
        f"Источники: {_join_report_values(plan.get('sources'))}",
        f"Приоритеты источников: {_join_priority_values(plan.get('source_priorities'))}",
        f"Максимум вакансий для сбора: {plan.get('max_vacancies') or '-'}",
        f"Режим ранжирования: {'auto + LLM' if plan.get('rank_mode') == 'llm' else 'auto'}",
        f"Целевое число карточек: {plan.get('top_k') or '-'}",
        f"LLM HTML parsing: {'включен' if plan.get('use_llm_html') else 'выключен'}",
        "",
        "Жесткие фильтры сбора:",
        _criteria_report_block(fetch_filters),
        "",
        "Стратегия staged-добора:",
        _quick_stage_report_block(_quick_filter_stages(fetch_filters)),
        "",
        "Критерии ранжирования:",
        _criteria_report_block(plan.get("criteria") or {}),
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def _quick_sources_report(trace_summary: dict[str, Any], source_breakdown: dict[str, int]) -> str:
    raw_rows = trace_summary.get("raw_rows", 0)
    unique_rows = trace_summary.get("unique_rows", 0)
    metrics = trace_summary.get("fetch_metrics") or {}
    lines = [
        f"Рассмотрено до фильтров: {raw_rows}",
        f"Итоговых строк CSV: {unique_rows}",
        f"Автопарсинг: {metrics.get('html_without_llm', 0)}",
        f"LLM-парсинг: {metrics.get('html_with_llm', 0)}",
    ]
    staged_fetch = trace_summary.get("staged_fetch") or []
    if staged_fetch:
        lines.append("")
        lines.append("Стадии staged-добора:")
        for stage in staged_fetch:
            if not isinstance(stage, dict):
                continue
            if stage.get("skipped"):
                lines.append(f"- {stage.get('title')}: пропущено, уже набрано {stage.get('cumulative_rows', 0)}")
                continue
            lines.append(
                f"- {stage.get('title')}: фильтров {stage.get('filter_count', 0)}, "
                f"строк {stage.get('rows', 0)}, новых {stage.get('added_rows', 0)}, всего {stage.get('cumulative_rows', 0)}"
            )
    if source_breakdown:
        lines.append("")
        lines.append("Итоговые вакансии по источникам:")
        lines.extend(f"- {source}: {count}" for source, count in sorted(source_breakdown.items()))
    source_stats = trace_summary.get("source_stats") or {}
    if source_stats:
        lines.append("")
        lines.append("Сбор по источникам:")
        for source, stats in sorted(source_stats.items()):
            lines.append(
                f"- {source}: найдено {stats.get('vacancies', 0)}, запросов {stats.get('requests', 0)}, успешных {stats.get('successful_requests', 0)}"
            )
    warnings = trace_summary.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Основные предупреждения:")
        lines.extend(f"- {str(warning).splitlines()[0][:160]}" for warning in warnings[:8])
    return "\n".join(lines).strip()


def _quick_stage_report_block(stages: list[dict[str, Any]]) -> str:
    if not stages:
        return "- нет"
    lines: list[str] = []
    for stage in stages:
        filters = stage.get("filters") or {}
        filter_names = ", ".join(filters.keys()) if filters else "нет"
        lines.append(f"- {stage.get('title')}: {filter_names}")
    return "\n".join(lines)


def _criteria_report_block(values: dict[str, Any]) -> str:
    non_empty = _non_empty_criteria_values(values)
    if not non_empty:
        return "- нет"
    labels = {
        "target_roles": "Роли",
        "target_roles_use_description": "Учитывать описание роли",
        "preferred_levels": "Уровни",
        "preferred_formats": "Форматы",
        "preferred_cities": "Города",
        "skills": "Навыки",
        "min_salary": "Минимальная зарплата",
        "salary_missing_penalty": "Штраф за отсутствие зарплаты",
        "english_level": "Английский",
        "stop_words": "Стоп-слова",
        "search_fields": "Где искать",
        "salary_defined": "Указан доход",
        "working_hours": "Рабочие часы",
        "employment_contract": "Оформление",
        "accredited_it": "Аккредитованная ИТ-компания",
    }
    return "\n".join(f"- {labels.get(key, key)}: {value}" for key, value in non_empty.items())


def _join_report_values(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item).strip()) or "-"
    return str(value or "-")


def _join_priority_values(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    return ", ".join(f"{source}: {priority}" for source, priority in sorted(value.items()))


def _write_quick_csv(kind: str, values: dict[str, str], *, name: str, description: str) -> Path:
    output_dir = FILTERS_DIR if kind == "filter" else CRITERIA_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = _unique_filter_path() if kind == "filter" else _unique_criteria_path()
    columns = _criteria_columns(kind)
    row = {column: values.get(column, "") for column in columns}
    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerow(row)
    metadata = _load_file_metadata()
    metadata[_relative(output_path)] = {"name": name or output_path.name, "description": description}
    _write_file_metadata(metadata)
    return output_path


def _run_command(
    cmd: list[str],
    *,
    job_id: str | None = None,
    progress_start: int = 0,
    progress_end: int = 100,
    timeout: int = 400,
) -> subprocess.CompletedProcess[str]:
    timeout = max(1, min(int(timeout or 400), 3600))
    if not job_id:
        try:
            return subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = str(error.stdout or "")
            stderr = str(error.stderr or "")
            return subprocess.CompletedProcess(cmd, 124, stdout=stdout + f"\nProcess timed out after {timeout} seconds.\n", stderr=stderr)

    process = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    stdout_parts: list[str] = []
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    if process.stdout:
        selector.register(process.stdout, selectors.EVENT_READ)
    timed_out = False
    while True:
        if process.poll() is not None:
            if process.stdout:
                tail = process.stdout.read()
                if tail:
                    stdout_parts.append(tail)
                    for line in tail.splitlines():
                        _update_job_from_output(job_id, line, progress_start=progress_start, progress_end=progress_end)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            break
        for key, _ in selector.select(timeout=0.2):
            line = key.fileobj.readline()
            if not line:
                continue
            stdout_parts.append(line)
            _update_job_from_output(job_id, line, progress_start=progress_start, progress_end=progress_end)
    if process.stdout:
        try:
            selector.unregister(process.stdout)
        except Exception:
            pass
    selector.close()
    if timed_out:
        remaining, _ = process.communicate()
        stdout_parts.append(remaining or "")
        stdout_parts.append(f"\nProcess timed out after {timeout} seconds.\n")
        returncode = 124
    else:
        returncode = process.returncode
    if process.stdout:
        process.stdout.close()
    stdout = "".join(stdout_parts)
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")


def _update_job_from_output(job_id: str, line: str, *, progress_start: int = 0, progress_end: int = 100) -> None:
    match = re.search(r"\]\s*(?P<percent>\d+)%\s*(?P<stage>.+)$", line.strip())
    if not match:
        return
    child_progress = max(0, min(100, int(match.group("percent"))))
    start = max(0, min(100, progress_start))
    end = max(start, min(100, progress_end))
    progress = start + round((end - start) * child_progress / 100)
    stage = match.group("stage").strip()
    _update_job(job_id, progress=progress, stage=stage)


def _rank_run_details(cmd: list[str], result: subprocess.CompletedProcess[str], log_path: Path) -> str:
    parts = [
        "Command:",
        " ".join(cmd),
        "",
        f"Return code: {result.returncode}",
        "",
        "Process stdout:",
        result.stdout.strip() or "-",
    ]
    if result.stderr.strip():
        parts.extend(["", "Process stderr:", result.stderr.strip()])
    log_text = _read_preview(log_path, 8000)
    if log_text:
        parts.extend(["", "run.log:", log_text.strip()])
    return "\n".join(parts).strip()


def _rank_card_vacancies(trace_path: Path | None) -> list[dict[str, Any]]:
    if not trace_path or not trace_path.exists():
        return []
    try:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    cards = trace.get("card_vacancies")
    if isinstance(cards, list) and cards:
        return [card for card in cards if isinstance(card, dict)]

    scoring_trace = trace.get("scoring_trace")
    if not isinstance(scoring_trace, list):
        return []
    explanations = {
        str(item.get("title") or "").strip().lower(): item
        for item in trace.get("agent_explanations", [])
        if isinstance(item, dict)
    }
    fallback_cards: list[dict[str, Any]] = []
    for item in scoring_trace:
        if not isinstance(item, dict):
            continue
        card = dict(item)
        explanation = explanations.get(str(card.get("title") or "").strip().lower())
        if isinstance(explanation, dict):
            card.update(
                {
                    "llm_explanation_comment": explanation.get("llm_comment") or card.get("llm_comment"),
                    "llm_risks": explanation.get("risks") or card.get("concerns"),
                    "why_fit": explanation.get("why_fit"),
                }
            )
        fallback_cards.append(card)
    return fallback_cards


def _list_vacancy_files() -> list[dict[str, Any]]:
    metadata = _load_file_metadata()
    roots = [PROJECT_ROOT / "data" / "collected", PROJECT_ROOT / "data" / "collect"]
    files: list[Path] = []
    root_vacancies = PROJECT_ROOT / "vacancies.csv"
    if root_vacancies.exists():
        files.append(root_vacancies)
    for root in roots:
        if root.exists():
            files.extend(sorted(root.glob("vacancies*.csv")))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen or path.name.startswith("."):
            continue
        seen.add(resolved)
        unique.append(path)
    return [
        _csv_file_metadata(
            path,
            description=f"CSV-файл вакансий для ранжирования, строк с данными: {_count_csv_rows(path)}.",
            metadata=metadata,
        )
        for path in unique
        if path.exists() and path.suffix.lower() == ".csv"
    ]


def _list_criteria_files() -> list[dict[str, Any]]:
    files: list[Path] = []
    root_criteria = PROJECT_ROOT / "criteria.csv"
    if root_criteria.exists():
        files.append(root_criteria)
    if CRITERIA_DIR.exists():
        files.extend(sorted(CRITERIA_DIR.glob("criteria*.csv")))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen or path.name.startswith("."):
            continue
        seen.add(resolved)
        unique.append(path)
    return [_criteria_file_metadata(path) for path in unique if path.exists() and path.suffix.lower() == ".csv"]


def _list_filter_files() -> list[dict[str, Any]]:
    files: list[Path] = []
    root_filters = PROJECT_ROOT / "filters.csv"
    if root_filters.exists():
        files.append(root_filters)
    if FILTERS_DIR.exists():
        files.extend(sorted(FILTERS_DIR.glob("filters*.csv")))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen or path.name.startswith("."):
            continue
        seen.add(resolved)
        unique.append(path)
    return [_filter_file_metadata(path) for path in unique if path.exists() and path.suffix.lower() == ".csv"]


def _criteria_file_metadata(path: Path | None = None) -> dict[str, Any]:
    metadata = _load_file_metadata()
    criteria_path = path or PROJECT_ROOT / "criteria.csv"
    if not criteria_path.exists():
        return {}
    return _csv_file_metadata(
        criteria_path,
        description=f"CSV-файл критериев кандидата для оценки вакансий, строк с данными: {_count_csv_rows(criteria_path)}.",
        metadata=metadata,
    )


def _filter_file_metadata(path: Path | None = None) -> dict[str, Any]:
    metadata = _load_file_metadata()
    filter_path = path or PROJECT_ROOT / "filters.csv"
    if not filter_path.exists():
        return {}
    return _csv_file_metadata(
        filter_path,
        description=f"CSV-файл жестких фильтров для сбора вакансий, строк с данными: {_count_csv_rows(filter_path)}.",
        metadata=metadata,
    )


def _csv_file_metadata(path: Path, *, description: str, metadata: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    stat = path.stat()
    created_at = getattr(stat, "st_birthtime", stat.st_ctime)
    custom = (metadata or {}).get(_relative(path), {})
    return {
        "path": _relative(path),
        "name": custom.get("name") or path.name,
        "filename": path.name,
        "description": custom.get("description") or description,
        "rows": _count_csv_rows(path),
        "size": stat.st_size,
        "created": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"),
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def _vacancy_file_summary(path_value: str) -> dict[str, Any]:
    path = _safe_project_path(str(path_value or ""))
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return {"ok": False, "error": "Choose an existing vacancies CSV."}
    trace_path = path.with_suffix(".trace.json")
    rows = _count_csv_rows(path)
    trace_summary = _trace_summary(trace_path)
    status = _fetch_status(returncode=0, rows=rows, trace_summary=trace_summary)
    return {
        "ok": True,
        **status,
        "path": _relative(path),
        "trace_path": _relative(trace_path) if trace_path.exists() else "",
        "rows": rows,
        "trace_summary": trace_summary,
        "source_breakdown": _csv_source_breakdown(path),
    }


def _vacancy_file_cards(path_value: str) -> dict[str, Any]:
    path = _safe_project_path(str(path_value or ""))
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return {"ok": False, "error": "Choose an existing vacancies CSV.", "cards": []}
    cards: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            for index, row in enumerate(reader, start=1):
                if not isinstance(row, dict):
                    continue
                card = {
                    str(key or "").strip(): str(value or "").strip()
                    for key, value in row.items()
                    if str(key or "").strip() and str(value or "").strip()
                }
                if not card:
                    continue
                card["_csv_row"] = index
                cards.append(card)
    except csv.Error as error:
        return {"ok": False, "error": f"Cannot read CSV: {error}", "cards": []}
    except OSError as error:
        return {"ok": False, "error": f"Cannot read file: {error}", "cards": []}
    return {"ok": True, "path": _relative(path), "count": len(cards), "cards": cards}


def _trace_cards(path_value: str) -> dict[str, Any]:
    path = _safe_project_path(str(path_value or ""), allow_outputs=True)
    if not path or not path.exists() or path.suffix.lower() != ".json":
        return {"ok": False, "error": "Choose an existing trace JSON.", "cards": []}
    cards = _rank_card_vacancies(path)
    return {"ok": True, "path": _relative(path), "count": len(cards), "cards": cards}


def _save_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    path_value = str(payload.get("path") or "")
    path = _safe_project_path(path_value) or _safe_criteria_path(path_value) or _safe_filter_path(path_value)
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return {"ok": False, "error": "Choose an existing CSV."}

    csv_content = payload.get("csv_content")
    if csv_content is not None:
        is_criteria = bool(_safe_criteria_path(path_value))
        is_filter = bool(_safe_filter_path(path_value))
        if not is_criteria and not is_filter:
            return {"ok": False, "error": "CSV editing is available only for criteria and filter files."}
        validation_error = _validate_criteria_csv_text(str(csv_content), kind="criteria" if is_criteria else "filter")
        if validation_error:
            return {"ok": False, "error": validation_error}
        path.write_text(str(csv_content).strip() + "\n", encoding="utf-8-sig")

    relative_path = _relative(path)
    metadata = _load_file_metadata()
    metadata[relative_path] = {
        "name": _sanitize_metadata_text(payload.get("name"), limit=80) or path.name,
        "description": _sanitize_metadata_text(payload.get("description"), limit=300),
    }
    _write_file_metadata(metadata)
    criteria_files = _list_criteria_files()
    filter_files = _list_filter_files()
    return {
        "ok": True,
        "files": _list_vacancy_files(),
        "criteria_files": criteria_files,
        "filter_files": filter_files,
        "criteria": criteria_files[0] if criteria_files else {},
        "filter": filter_files[0] if filter_files else {},
    }


def _delete_csv_file(payload: dict[str, Any]) -> dict[str, Any]:
    path_value = str(payload.get("path") or "")
    path = _safe_project_path(path_value) or _safe_criteria_path(path_value) or _safe_filter_path(path_value)
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return {"ok": False, "error": "Choose an existing CSV."}

    relative_path = _relative(path)
    try:
        path.unlink()
    except OSError as error:
        return {"ok": False, "error": f"Cannot delete file: {error}"}

    metadata = _load_file_metadata()
    metadata.pop(relative_path, None)
    _write_file_metadata(metadata)
    criteria_files = _list_criteria_files()
    filter_files = _list_filter_files()
    return {
        "ok": True,
        "deleted_path": relative_path,
        "files": _list_vacancy_files(),
        "criteria_files": criteria_files,
        "filter_files": filter_files,
        "criteria": criteria_files[0] if criteria_files else {},
        "filter": filter_files[0] if filter_files else {},
    }


def _generate_criteria_file(payload: dict[str, Any]) -> dict[str, Any]:
    user_text = " ".join(str(payload.get("text") or "").split())
    kind = "filter" if str(payload.get("kind") or "").lower() == "filter" else "criteria"
    columns = _criteria_columns(kind)
    empty_request = not user_text
    if empty_request:
        criteria = {column: "" for column in columns}
        result = {
            "name": "Пустой файл фильтров" if kind == "filter" else "Пустой файл критериев",
            "description": "Файл создан без критериев, фильтры не учитываются.",
        }
    else:
        _load_env_file(PROJECT_ROOT / ".env")
        llm_client = LLMClient.from_env()
        if not llm_client.enabled:
            return {"ok": False, "error": f"LLM недоступна: {llm_client.reason}"}

        result = llm_client.json_task(
            stage="criteria_from_natural_language",
            system_prompt=(
                "You convert a candidate's natural-language job preferences into exactly one CSV-compatible criteria row. "
                "Return only valid JSON matching expected_json_shape. No markdown or prose. "
                "Extract only preferences stated or directly implied by the user; do not invent missing roles, skills, cities, salary, English level or stop words. "
                "If the user did not specify a preference, return an empty string for that field so scoring ignores it. "
                "Use semicolon-separated values for multi-value fields. Keep values short and normalized for matching."
            ),
            payload={
                "user_text": user_text[:4000],
                "columns": columns,
                "field_rules": {
                    "target_roles": "Desired roles separated by '; '. Empty if absent.",
                    "target_roles_use_description": "Use 'yes' only when target role matching should consider vacancy title plus vacancy description; otherwise empty.",
                    "preferred_levels": "Internship; Junior; Entry; Middle; Senior etc. Empty if absent.",
                    "preferred_formats": "remote; hybrid; onsite. Empty if absent.",
                    "preferred_cities": "Cities or 'Удаленно' separated by '; '. Empty if absent.",
                    "skills": "Skills/technologies separated by '; '. Empty if absent.",
                    "min_salary": "Minimum salary as digits only, RUB by default. Empty if absent.",
                    "salary_missing_penalty": "Use 'yes' when missing vacancy salary should reduce score. Empty if absent.",
                    "english_level": "A1/A2/A2+/B1/B2/C1/C2. Empty if absent.",
                    "stop_words": "Undesired levels, formats, requirements or words separated by '; '. Empty if absent.",
                    "search_fields": "For hard filters only: where the search query must match. Use 'name', 'description' or 'name; description'. Empty if absent.",
                    "salary_defined": "For hard filters only: use 'yes' only when vacancies must have visible salary. Empty if absent.",
                    "working_hours": "For hard filters only: working hours per day such as '4; 6; 8; flexible'. Empty if absent.",
                    "employment_contract": "For hard filters only: 'labor_contract' or 'gph_or_part_time'. Empty if absent.",
                    "accredited_it": "For hard filters only: use 'yes' only when an accredited IT company is mandatory. Empty if absent.",
                    "criterion_importance": (
                        "For criteria only: min_salary priority in format min_salary:low|medium|high. "
                        "Infer high only when salary is mandatory or critical; low when optional; otherwise use min_salary:low."
                    ),
                },
                "expected_json_shape": {
                    "criteria": {column: "string" for column in columns},
                    "name": "short Russian file display name",
                    "description": "Russian description of extracted and omitted preferences",
                },
            },
        )
        criteria = _coerce_generated_criteria(result.get("criteria") if isinstance(result.get("criteria"), dict) else result, kind=kind)
        if not any(value for value in criteria.values()):
            return {"ok": False, "error": "LLM не смогла извлечь ни одного критерия из текста."}

    output_dir = FILTERS_DIR if kind == "filter" else CRITERIA_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = _unique_filter_path() if kind == "filter" else _unique_criteria_path()
    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerow(criteria)

    relative_path = _relative(output_path)
    metadata = _load_file_metadata()
    metadata[relative_path] = {
        "name": _sanitize_metadata_text(result.get("name"), limit=80) or output_path.name,
        "description": _sanitize_metadata_text(result.get("description"), limit=300) or _generated_criteria_description(criteria),
    }
    _write_file_metadata(metadata)
    criteria_files = _list_criteria_files()
    filter_files = _list_filter_files()
    created = _filter_file_metadata(output_path) if kind == "filter" else _criteria_file_metadata(output_path)
    return {
        "ok": True,
        "created_path": relative_path,
        "created": created,
        "files": _list_vacancy_files(),
        "criteria_files": criteria_files,
        "filter_files": filter_files,
        "criteria": created,
        "filter": created if kind == "filter" else (filter_files[0] if filter_files else {}),
        "llm_trace": [] if empty_request else llm_client.call_trace,
    }


def _criteria_columns(kind: str = "criteria") -> list[str]:
    columns = [
        "target_roles",
        "target_roles_use_description",
        "preferred_levels",
        "preferred_formats",
        "preferred_cities",
        "skills",
        "min_salary",
        "salary_missing_penalty",
        "english_level",
        "stop_words",
    ]
    if kind == "filter":
        filter_columns = [column for column in columns if column not in {"target_roles", "target_roles_use_description", "skills", "salary_missing_penalty"}]
        return filter_columns + [
            "search_fields",
            "salary_defined",
            "working_hours",
            "employment_contract",
            "accredited_it",
        ]
    return columns + ["criterion_importance"]


def _required_criteria_columns(kind: str = "criteria") -> list[str]:
    optional = {"criterion_importance", "target_roles_use_description", "salary_missing_penalty"}
    return [column for column in _criteria_columns(kind) if column not in optional]


def _validate_criteria_csv_text(value: str, *, kind: str = "criteria") -> str:
    text = value.strip()
    if not text:
        return "CSV критериев не должен быть пустым."
    try:
        reader = csv.DictReader(text.splitlines())
        rows = list(reader)
    except csv.Error as error:
        return f"CSV критериев не читается: {error}"
    header = [str(field or "").strip().lstrip("\ufeff") for field in (reader.fieldnames or [])]
    if not header:
        return "CSV критериев должен содержать заголовок."
    missing = [column for column in _required_criteria_columns(kind) if column not in header]
    if missing:
        return f"В CSV критериев не хватает колонок: {', '.join(missing)}."
    if not rows:
        return "CSV критериев должен содержать хотя бы одну строку данных."
    return ""


def _coerce_generated_criteria(raw: Any, *, kind: str = "criteria") -> dict[str, str]:
    source = raw if isinstance(raw, dict) else {}
    criteria: dict[str, str] = {}
    for column in _criteria_columns(kind):
        value = _sanitize_criteria_value(source.get(column), limit=900)
        if column == "min_salary":
            value = "".join(re.findall(r"\d+", value))
        if column == "criterion_importance":
            value = _sanitize_criteria_importance(value)
        if column in {"target_roles_use_description", "salary_missing_penalty"}:
            value = "yes" if _coerce_bool(value, default=False) else ""
        if _is_unspecified_value(value):
            value = ""
        criteria[column] = value
    return criteria


def _sanitize_criteria_importance(value: str) -> str:
    defaults = {
        "min_salary": "low",
    }
    parsed = dict(defaults)
    for key, priority in re.findall(r"([a-zA-Z_]+)\s*[:=]\s*(low|medium|high|низк\w*|средн\w*|высок\w*)", value, flags=re.IGNORECASE):
        normalized_priority = priority.lower()
        if normalized_priority.startswith("низк"):
            normalized_priority = "low"
        elif normalized_priority.startswith("сред"):
            normalized_priority = "medium"
        elif normalized_priority.startswith("выс"):
            normalized_priority = "high"
        if key in defaults and normalized_priority in {"low", "medium", "high"}:
            parsed[key] = normalized_priority
    return "; ".join(f"{key}:{priority}" for key, priority in parsed.items())


def _sanitize_criteria_value(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").replace("|", ";").split())
    return text[:limit]


def _is_unspecified_value(value: str) -> bool:
    normalized = value.strip().lower().replace("ё", "е")
    return normalized in {
        "",
        "-",
        "none",
        "null",
        "unknown",
        "not specified",
        "not_specified",
        "не указано",
        "не указан",
        "не указана",
        "не задано",
        "не задан",
        "не важно",
        "любой",
        "любая",
        "любые",
        "без предпочтений",
        "не учитывать",
    }


def _generated_criteria_description(criteria: dict[str, str]) -> str:
    filled = [column for column, value in criteria.items() if value]
    empty = [column for column, value in criteria.items() if not value]
    parts = []
    if filled:
        parts.append(f"Из текста извлечены поля: {', '.join(filled)}.")
    if empty:
        parts.append(f"Не заданы и не учитываются: {', '.join(empty)}.")
    return " ".join(parts)[:300]


def _unique_criteria_path() -> Path:
    candidate = CRITERIA_DIR / f"criteria_{_timestamp()}.csv"
    counter = 1
    while candidate.exists():
        candidate = CRITERIA_DIR / f"criteria_{_timestamp()}_{counter}.csv"
        counter += 1
    return candidate


def _unique_filter_path() -> Path:
    candidate = FILTERS_DIR / f"filters_{_timestamp()}.csv"
    counter = 1
    while candidate.exists():
        candidate = FILTERS_DIR / f"filters_{_timestamp()}_{counter}.csv"
        counter += 1
    return candidate


def _load_file_metadata() -> dict[str, dict[str, str]]:
    if not METADATA_PATH.exists():
        return {}
    try:
        data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for path, item in data.items():
        if isinstance(path, str) and isinstance(item, dict):
            result[path] = {
                "name": str(item.get("name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }
    return result


def _write_file_metadata(metadata: dict[str, dict[str, str]]) -> None:
    METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    METADATA_PATH.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sanitize_metadata_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _safe_project_path(value: str, *, allow_outputs: bool = False) -> Path | None:
    if not value:
        return None
    candidate = (PROJECT_ROOT / value).resolve()
    allowed_roots = [PROJECT_ROOT.resolve(), (PROJECT_ROOT / "data" / "collected").resolve(), (PROJECT_ROOT / "data" / "collect").resolve()]
    if allow_outputs:
        allowed_roots.append((PROJECT_ROOT / "output").resolve())
    if candidate == PROJECT_ROOT.resolve() / "vacancies.csv":
        return candidate
    for root in allowed_roots[1:]:
        if candidate == root or root in candidate.parents:
            return candidate
    if allow_outputs and (PROJECT_ROOT / "output").resolve() in candidate.parents:
        return candidate
    return None


def _safe_criteria_path(value: str) -> Path | None:
    if not value:
        return None
    candidate = (PROJECT_ROOT / value).resolve()
    if candidate == PROJECT_ROOT.resolve() / "criteria.csv":
        return candidate
    criteria_root = CRITERIA_DIR.resolve()
    if candidate.suffix.lower() == ".csv" and criteria_root in candidate.parents:
        return candidate
    return None


def _safe_filter_path(value: str) -> Path | None:
    if not value:
        return None
    candidate = (PROJECT_ROOT / value).resolve()
    if candidate == PROJECT_ROOT.resolve() / "filters.csv":
        return candidate
    filters_root = FILTERS_DIR.resolve()
    if candidate.suffix.lower() == ".csv" and filters_root in candidate.parents:
        return candidate
    return None


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


def _count_csv_rows(path: Path | None) -> int:
    if not path or not path.exists():
        return 0
    with path.open(encoding="utf-8-sig", errors="replace") as file:
        return max(0, sum(1 for _ in file) - 1)


def _csv_source_breakdown(path: Path | None) -> dict[str, int]:
    if not path or not path.exists():
        return {}
    counts: dict[str, int] = {}
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as file:
        for row in csv.DictReader(file):
            source = _base_source_name(row.get("source") or "unknown")
            counts[source] = counts.get(source, 0) + 1
    return counts


def _base_source_name(value: Any) -> str:
    source = str(value or "unknown").strip() or "unknown"
    for suffix in ("-html-detail", "-api-detail", "-llm-html", "-mixed-html", "-html", "-json", "-detail"):
        if source.endswith(suffix):
            return source[: -len(suffix)] or "unknown"
    return source


def _read_preview(path: Path | None, limit: int) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _trace_summary(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    keys = [
        "run_mode",
        "llm_used",
        "queries",
        "query_limits",
        "raw_rows",
        "unique_rows",
        "source_limits",
        "source_stats",
        "fetch_metrics",
        "request_log",
        "llm_html_mode",
        "url_inputs",
        "warnings",
        "trace_context",
        "validation_report",
    ]
    return {key: trace.get(key) for key in keys if key in trace}


def _fetch_error_message(
    *,
    result: subprocess.CompletedProcess[str],
    status: dict[str, Any],
    rows: int,
    trace_summary: dict[str, Any],
) -> str:
    if status.get("ok"):
        return ""
    if result.returncode != 0:
        output = _first_non_empty(result.stderr, result.stdout)
        if output:
            return f"fetch_vacancies failed with code {result.returncode}: {output[-1200:]}"
        return f"fetch_vacancies failed with code {result.returncode}."
    failed_requests = [item for item in trace_summary.get("request_log") or [] if isinstance(item, dict) and not item.get("ok")]
    if failed_requests:
        item = failed_requests[0]
        source = str(item.get("source") or "unknown")
        method = str(item.get("method") or "request")
        reason = str(item.get("error") or item.get("status") or item.get("reason") or "failed").strip()
        return f"{source}/{method}: {reason}"
    if rows <= 0 and not trace_summary.get("request_log"):
        return "fetch_vacancies finished without rows and without request log; command likely failed before network requests."
    return ""


def _fetch_status(*, returncode: int, rows: int, trace_summary: dict[str, Any]) -> dict[str, Any]:
    request_log = trace_summary.get("request_log") or []
    failed_requests = [item for item in request_log if not item.get("ok")]
    successful_requests = [item for item in request_log if item.get("ok")]
    api_failures = [item for item in failed_requests if str(item.get("method") or "").startswith("api")]
    html_successes = [item for item in successful_requests if str(item.get("method") or "") in {"html", "url-html"}]
    target_rows = _fetch_target_rows(trace_summary)
    target_reached = target_rows > 0 and rows >= target_rows

    if returncode != 0:
        status = "error"
        label = "Ошибка запуска"
        ok = False
    elif rows <= 0:
        status = "empty"
        label = "Данных нет"
        ok = False
    elif target_reached:
        status = "success"
        label = "Готово"
        ok = True
    elif api_failures and html_successes:
        status = "partial"
        label = "HTML fallback"
        ok = True
    elif failed_requests:
        status = "partial"
        label = "Частично готово"
        ok = True
    else:
        status = "success"
        label = "Готово"
        ok = True

    return {
        "status": status,
        "status_label": label,
        "ok": ok,
        "failed_requests": len(failed_requests),
        "successful_requests": len(successful_requests),
        "api_failures": len(api_failures),
        "html_successes": len(html_successes),
    }


def _fetch_target_rows(trace_summary: dict[str, Any]) -> int:
    max_vacancies = trace_summary.get("max_vacancies")
    if isinstance(max_vacancies, int) and max_vacancies > 0:
        return max_vacancies
    query_limits = trace_summary.get("query_limits")
    if isinstance(query_limits, dict):
        total = 0
        for value in query_limits.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                continue
        if total > 0:
            return total
    return 0


def _extract_created_path(stdout: str) -> Path | None:
    for line in stdout.splitlines():
        if line.startswith("Created:"):
            value = line.split(":", 1)[1].strip()
            path = Path(value)
            return path if path.is_absolute() else PROJECT_ROOT / path
    return None


def _relative(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _clean_sources(value: Any) -> list[str]:
    if isinstance(value, list):
        sources = [str(item).lower() for item in value]
    else:
        sources = [part.strip().lower() for part in str(value or "").split(",") if part.strip()]
    aliases = {
        "sj": "superjob",
        "rabota": "rabota_ru",
        "rabota.ru": "rabota_ru",
        "avito_work": "avito",
        "avito-rabota": "avito",
        "zarplata.ru": "zarplata",
        "gorodrabot.ru": "gorodrabot",
        "habr_career": "habr",
        "habr-career": "habr",
        "career_habr": "habr",
        "geekjob.ru": "geekjob",
        "rabota_russia": "trudvsem",
        "rabota-rossii": "trudvsem",
        "trudvsem.ru": "trudvsem",
    }
    allowed_sources = {"hh", "superjob", "rabota_ru", "avito", "zarplata", "gorodrabot", "jooble", "habr", "geekjob", "trudvsem"}
    normalized = [aliases.get(source, source) for source in sources]
    allowed = [source for source in normalized if source in allowed_sources]
    return allowed


def _default_sources() -> list[str]:
    return ["hh", "superjob", "rabota_ru", "avito", "zarplata", "gorodrabot", "jooble", "habr", "geekjob", "trudvsem"]


def _default_source_priorities(sources: list[str]) -> dict[str, str]:
    defaults = {
        "hh": "high",
        "superjob": "high",
        "rabota_ru": "medium",
        "avito": "medium",
        "zarplata": "medium",
        "gorodrabot": "low",
        "jooble": "low",
        "habr": "high",
        "geekjob": "high",
        "trudvsem": "medium",
    }
    return {source: defaults.get(source, "medium") for source in sources}


def _clean_source_priorities(value: Any, sources: list[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {source: "medium" for source in sources}
    allowed_priorities = {"high", "medium", "low"}
    priorities: dict[str, str] = {}
    for source in sources:
        priority = str(value.get(source) or "medium").strip().lower()
        priorities[source] = priority if priority in allowed_priorities else "medium"
    return priorities


def _clean_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]
    keywords: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        keyword = " ".join(str(item or "").split())[:120]
        key = keyword.lower()
        if keyword and key not in seen:
            seen.add(key)
            keywords.append(keyword)
    return keywords


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "y", "да", "включить"}:
        return True
    if normalized in {"0", "false", "no", "off", "n", "нет", "выключить"}:
        return False
    return default


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _find_port(start: int) -> int:
    import socket

    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _build_index_html() -> str:
    return r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vacancy Finder Service</title>
  <style>
    :root {
      --yellow: #ffdd2d;
      --yellow-2: #ffd429;
      --ink: #101820;
      --muted: #657080;
      --line: #dde3ea;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --green: #0a8f5a;
      --amber: #b77900;
      --red: #c83b3b;
      --radius: 8px;
    }
    body.dark-theme {
      --ink: #f5f7fa;
      --muted: #a8b3c0;
      --line: #344255;
      --bg: #0d131c;
      --panel: #151f2c;
      --yellow: #f2c94c;
      --yellow-2: #e5bd45;
      --green: #31c48d;
      --amber: #f4c152;
      --red: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
      letter-spacing: 0;
    }
    header {
      height: 64px;
      background: var(--yellow);
      border-bottom: 1px solid rgba(16, 24, 32, .16);
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
      align-items: center;
      padding: 0 28px;
      column-gap: 16px;
    }
    body.dark-theme header {
      background: #111a27;
      color: var(--ink);
      border-bottom-color: #2c394a;
      box-shadow: 0 1px 0 rgba(255, 255, 255, .03);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 800;
      font-size: 18px;
      justify-self: start;
    }
    .brand-title-mobile {
      display: none;
    }
    .theme-toggle {
      width: 54px;
      height: 32px;
      border-radius: 999px;
      background: var(--ink);
      color: var(--yellow);
      display: inline-flex;
      align-items: center;
      padding: 3px;
      font-weight: 900;
      cursor: pointer;
      position: relative;
      flex: 0 0 auto;
    }
    .theme-toggle input {
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }
    .theme-toggle-mark {
      width: 26px;
      height: 26px;
      border-radius: 50%;
      background: var(--yellow);
      color: #101820;
      display: grid;
      place-items: center;
      transition: transform .24s ease, background .24s ease, color .24s ease;
    }
    .theme-toggle input:checked + .theme-toggle-mark {
      transform: translateX(22px);
      background: #101820;
      color: var(--yellow);
      box-shadow: inset 0 0 0 1px rgba(255, 221, 45, .55);
    }
    .header-actions {
      position: relative;
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      justify-self: end;
    }
    .mode-toggle {
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 0;
      color: rgba(16, 24, 32, .72);
      font-size: 13px;
      font-weight: 900;
      justify-self: center;
    }
    .mode-text-mobile {
      display: none;
    }
    .mode-toggle span.active {
      color: var(--ink);
    }
    .notification-wrap {
      position: relative;
      display: flex;
      align-items: center;
      gap: 8px;
      width: 40px;
      justify-content: center;
      flex: 0 0 40px;
    }
    .notification-island {
      position: absolute;
      left: 50%;
      top: 3px;
      max-width: 0;
      min-height: 34px;
      overflow: hidden;
      border-radius: 999px;
      background: var(--ink);
      color: #fff;
      display: inline-flex;
      align-items: center;
      white-space: nowrap;
      font-size: 13px;
      font-weight: 900;
      opacity: 0;
      transform: translateX(-50%) scale(.92);
      transition: max-width .34s ease, opacity .2s ease, transform .34s ease, padding .34s ease;
      padding: 0;
      pointer-events: none;
    }
    .notification-wrap.alerting .notification-island {
      max-width: 240px;
      opacity: 1;
      transform: translateX(-50%) scale(1);
      padding: 0 13px;
      pointer-events: auto;
      cursor: pointer;
    }
    .notification-button {
      position: relative;
      width: 40px;
      height: 40px;
      border: 1px solid rgba(16, 24, 32, .18);
      border-radius: 50%;
      background: rgba(255, 255, 255, .78);
      color: var(--ink);
      display: grid;
      place-items: center;
      cursor: pointer;
      transition: opacity .18s ease, transform .18s ease;
    }
    .notification-button svg {
      width: 20px;
      height: 20px;
      stroke-width: 2.4;
    }
    .notification-wrap.alerting .notification-button {
      opacity: 0;
      pointer-events: none;
      transform: scale(.88);
    }
    .notification-count {
      position: absolute;
      right: -3px;
      top: -3px;
      min-width: 17px;
      height: 17px;
      border-radius: 999px;
      background: var(--red);
      color: #fff;
      border: 2px solid var(--yellow);
      display: none;
      align-items: center;
      justify-content: center;
      font-size: 10px;
      font-weight: 900;
      line-height: 1;
    }
    .notification-count.visible {
      display: flex;
    }
    .notification-panel {
      position: absolute;
      right: 0;
      top: calc(100% + 10px);
      width: min(360px, calc(100vw - 24px));
      max-height: min(460px, calc(100vh - 90px));
      overflow: auto;
      border: 1px solid rgba(16, 24, 32, .16);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 20px 54px rgba(16, 24, 32, .22);
      z-index: 90;
      padding: 10px;
    }
    .notification-panel.hidden {
      display: none;
    }
    .notification-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 4px 4px 10px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 6px;
    }
    .notification-head h2 {
      font-size: 15px;
      margin: 0;
    }
    .notification-sound {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
    }
    .notification-sound input {
      width: 28px;
      accent-color: var(--yellow);
    }
    .notification-list {
      display: grid;
      gap: 6px;
    }
    .notification-item {
      width: 100%;
      border-radius: 7px;
      background: rgba(246, 248, 250, .92);
      color: var(--ink);
      padding: 11px 12px;
      display: grid;
      gap: 6px;
      text-align: left;
      cursor: pointer;
      border: 0;
    }
    .notification-item:hover,
    .notification-item:focus {
      outline: 2px solid rgba(255, 221, 45, .65);
      background: #fff8c7;
    }
    .notification-row {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 10px;
    }
    .notification-title {
      font-size: 14px;
      font-weight: 900;
    }
    .notification-meta {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .notification-progress {
      height: 4px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(16, 24, 32, .10);
    }
    .notification-progress-fill {
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--yellow), #7f6a1c);
      transition: width .22s ease;
    }
    .notification-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 6px;
    }
    .notification-action {
      border: 1px solid rgba(16, 24, 32, .12);
      border-radius: 999px;
      background: rgba(255, 255, 255, .92);
      color: var(--ink);
      font-size: 11px;
      font-weight: 800;
      padding: 5px 9px;
      cursor: pointer;
    }
    .notification-action.danger {
      color: #b83232;
    }
    .notification-action.success {
      border-color: rgba(31, 122, 77, .28);
      background: rgba(31, 122, 77, .12);
      color: #1f7a4d;
      cursor: default;
    }
    .notification-action.warning {
      border-color: rgba(171, 132, 16, .34);
      background: rgba(255, 221, 45, .34);
      color: #7a5f00;
      cursor: default;
    }
    .notification-action.queue-chip {
      width: 28px;
      height: 28px;
      padding: 0;
      display: inline-grid;
      place-items: center;
      border-radius: 50%;
      background: var(--yellow);
      color: #111820;
      border-color: #d4ae00;
      font-size: 12px;
      line-height: 1;
    }
    .notification-action.neutral {
      color: #635a46;
      cursor: default;
    }
    .queue-position {
      display: inline-flex;
      align-items: center;
      margin-left: 10px;
      color: #7a5f00;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }
    .notification-empty {
      color: var(--muted);
      padding: 18px 8px;
      font-size: 13px;
      font-weight: 700;
      text-align: center;
    }
    @keyframes bellRing {
      0%, 100% { transform: rotate(0); }
      15% { transform: rotate(16deg); }
      30% { transform: rotate(-14deg); }
      45% { transform: rotate(10deg); }
      60% { transform: rotate(-8deg); }
      75% { transform: rotate(4deg); }
    }
    .shell {
      max-width: 1240px;
      margin: 0 auto;
      padding: 22px;
    }
    .tabs {
      display: flex;
      gap: 6px;
      margin-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }
    .tab {
      border: 0;
      background: transparent;
      padding: 13px 18px;
      font-weight: 700;
      color: var(--muted);
      border-bottom: 3px solid transparent;
      cursor: pointer;
    }
    .tab.active {
      color: var(--ink);
      border-color: var(--ink);
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      gap: 18px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 18px;
      box-shadow: 0 10px 22px rgba(16, 24, 32, .05);
    }
    h1, h2, h3 { margin: 0; }
    h2 { font-size: 18px; margin-bottom: 16px; }
    label {
      display: block;
      font-size: 13px;
      font-weight: 700;
      color: #303946;
      margin-bottom: 7px;
    }
    .field { margin-bottom: 14px; }
    input, select {
      width: 100%;
      min-height: 42px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      padding: 0 12px;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }
    textarea {
      width: 100%;
      min-height: 76px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      padding: 10px 12px;
      font: inherit;
      resize: vertical;
      background: #fff;
      color: var(--ink);
    }
    input:focus, select:focus, textarea:focus {
      outline: 2px solid rgba(255, 221, 45, .65);
      border-color: #a88b00;
    }
    input.placeholder-fade::placeholder,
    textarea.placeholder-fade::placeholder {
      opacity: .25;
      transition: opacity .28s ease;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .checkline {
      display: flex;
      gap: 10px;
      align-items: center;
      min-height: 36px;
      font-size: 14px;
      color: #303946;
    }
    .checkline input { width: 18px; min-height: 18px; }
    .keyword-list {
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding: 2px 0 8px;
      margin-bottom: 8px;
    }
    .keyword-chip {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 30px;
      border: 1px solid #cbd3dd;
      border-radius: 999px;
      background: #fff;
      color: #303946;
      font-size: 13px;
      font-weight: 800;
      padding: 0 9px 0 12px;
      white-space: nowrap;
    }
    .keyword-remove {
      width: 18px;
      height: 18px;
      border: 0;
      border-radius: 50%;
      background: #eef2f6;
      color: var(--muted);
      cursor: pointer;
      line-height: 1;
      padding: 0;
    }
    .keyword-line {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 42px;
      gap: 8px;
      align-items: center;
    }
    .email-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      min-height: 0;
      margin-bottom: 8px;
    }
    .email-list:empty {
      display: none;
      margin: 0;
    }
    .subscription-list {
      display: grid;
      gap: 10px;
    }
    .subscription-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px 12px;
      display: grid;
      gap: 5px;
    }
    .subscription-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: start;
    }
    .subscription-title {
      border: 0;
      background: transparent;
      color: var(--ink);
      cursor: pointer;
      padding: 0;
      font: inherit;
      font-size: 14px;
      font-weight: 900;
      line-height: 1.35;
      text-align: left;
      overflow-wrap: anywhere;
    }
    .subscription-title:hover {
      text-decoration: underline;
      text-underline-offset: 3px;
    }
    .subscription-delete {
      width: 32px;
      height: 32px;
      border: 1px solid #e0c7c7;
      border-radius: 7px;
      background: #fff6f6;
      color: #b83232;
      display: grid;
      place-items: center;
      cursor: pointer;
      padding: 0;
    }
    .subscription-delete svg {
      width: 17px;
      height: 17px;
      stroke-width: 2.2;
    }
    .subscription-meta {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      line-height: 1.45;
    }
    .subscription-details {
      border: 1px solid #dfe6ee;
      border-radius: 7px;
      background: #f7f9fb;
      color: #26313d;
      padding: 8px;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .telegram-field-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 42px;
    }
    .info-wrap {
      position: relative;
      display: inline-flex;
      align-items: center;
    }
    .info-button {
      width: 28px;
      height: 28px;
      border: 1px solid #cbd3dd;
      border-radius: 50%;
      background: #fff;
      color: var(--ink);
      display: grid;
      place-items: center;
      cursor: pointer;
      font-weight: 900;
      font-style: normal;
      padding: 0;
    }
    .info-popover {
      position: absolute;
      right: calc(100% + 10px);
      top: 50%;
      transform: translateY(-50%);
      z-index: 85;
      width: min(340px, calc(100vw - 44px));
      border: 1px solid rgba(16, 24, 32, .16);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 18px 44px rgba(16, 24, 32, .18);
      padding: 12px;
      color: var(--ink);
      font-size: 13px;
      line-height: 1.45;
    }
    .info-popover.hidden {
      display: none;
    }
    .info-popover a {
      color: var(--ink);
      font-weight: 900;
    }
    .sources-control {
      position: relative;
    }
    .sources-select {
      width: 100%;
      min-height: 42px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      padding: 0 12px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      cursor: pointer;
    }
    .sources-select:focus {
      outline: 2px solid rgba(255, 221, 45, .65);
      border-color: #a88b00;
    }
    .sources-select:disabled {
      background: #eef2f6;
      color: var(--muted);
      cursor: not-allowed;
    }
    .sources-select-text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 700;
    }
    .sources-select-arrow {
      color: var(--muted);
      font-size: 14px;
      flex: 0 0 auto;
    }
    .sources-menu {
      position: fixed;
      z-index: 80;
      max-height: calc(100vh - 24px);
      overflow: auto;
      box-shadow: 0 18px 44px rgba(16, 24, 32, .18);
    }
    .sources-menu.hidden { display: none; }
    .sources-control.all-enabled .sources-menu { display: none; }
    .sources-control.all-enabled .checkbox-group {
      background: #eef2f6;
      color: var(--muted);
    }
    .sources-control.all-enabled .checkbox-option {
      color: var(--muted);
      cursor: not-allowed;
    }
    .source-option {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .checkbox-group {
      display: grid;
      gap: 8px;
      min-height: 42px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      padding: 10px 12px;
      background: #fff;
    }
    .checkbox-group:focus-within {
      outline: 2px solid rgba(255, 221, 45, .65);
      border-color: #a88b00;
    }
    .checkbox-option {
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 0;
      color: #303946;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      min-width: 0;
    }
    .checkbox-option input {
      width: 18px;
      min-height: 18px;
      margin: 0;
      flex: 0 0 auto;
    }
    .source-priority,
    .criterion-priority {
      display: inline-flex;
      align-items: end;
      gap: 4px;
      height: 28px;
    }
    .priority-step {
      width: 10px;
      border: 0;
      border-radius: 3px;
      background: #d8e0e8;
      cursor: pointer;
      padding: 0;
    }
    .priority-step.low {
      height: 10px;
    }
    .priority-step.medium {
      height: 17px;
    }
    .priority-step.high {
      height: 24px;
    }
    .source-priority[data-priority="low"] .priority-step.low {
      background: var(--red);
    }
    .source-priority[data-priority="medium"] .priority-step.medium {
      background: var(--yellow);
      border: 1px solid #d4ae00;
    }
    .source-priority[data-priority="high"] .priority-step.high {
      background: var(--green);
    }
    .criterion-priority[data-priority="low"] .priority-step.low {
      background: var(--red);
    }
    .criterion-priority[data-priority="medium"] .priority-step.medium {
      background: var(--yellow);
      border: 1px solid #d4ae00;
    }
    .criterion-priority[data-priority="high"] .priority-step.high {
      background: var(--green);
    }
    .priority-step:focus {
      outline: 2px solid rgba(255, 221, 45, .65);
      outline-offset: 2px;
    }
    .sources-control.all-enabled .priority-step {
      cursor: not-allowed;
      opacity: .65;
    }
    .source-category {
      display: grid;
      gap: 8px;
    }
    .source-category + .source-category {
      border-top: 1px solid var(--line);
      padding-top: 10px;
      margin-top: 2px;
    }
    .source-category-title {
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      line-height: 1.25;
      text-transform: uppercase;
    }
    .switchline {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      min-height: 36px;
      margin-top: 8px;
      color: #303946;
      font-size: 14px;
      font-weight: 700;
    }
    .switch {
      position: relative;
      display: inline-flex;
      width: 42px;
      height: 24px;
      flex: 0 0 auto;
    }
    .switch input {
      opacity: 0;
      width: 0;
      min-height: 0;
    }
    .switch-slider {
      position: absolute;
      inset: 0;
      border-radius: 999px;
      background: #cbd3dd;
      cursor: pointer;
      transition: background .16s ease;
    }
    .switch-slider::before {
      content: "";
      position: absolute;
      width: 18px;
      height: 18px;
      left: 3px;
      top: 3px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 1px 4px rgba(16, 24, 32, .2);
      transition: transform .16s ease;
    }
    .switch input:checked + .switch-slider {
      background: var(--ink);
    }
    .switch input:checked + .switch-slider::before {
      transform: translateX(18px);
    }
    .field-error {
      margin-top: 6px;
      color: var(--red);
      font-size: 12px;
      font-weight: 700;
      line-height: 1.35;
    }
    .field-error.hidden { display: none; }
    button.primary {
      width: 100%;
      min-height: 44px;
      border: 0;
      border-radius: 7px;
      background: var(--ink);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    button.primary:hover { background: #26313d; }
    button.secondary {
      min-height: 36px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      background: #fff;
      color: var(--ink);
      font-weight: 700;
      cursor: pointer;
      padding: 0 12px;
    }
    button.secondary.active {
      background: var(--ink);
      border-color: var(--ink);
      color: #fff;
    }
    .status {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
      margin-bottom: 14px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 6px 10px;
      background: #eef2f6;
      color: #303946;
      font-size: 13px;
      font-weight: 700;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--muted);
    }
    .dot.ok { background: var(--green); }
    .dot.warn { background: var(--amber); }
    .dot.err { background: var(--red); }
    .result-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric {
      background: #f7f9fb;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px;
      min-height: 70px;
    }
    .metric b {
      display: block;
      font-size: 22px;
      margin-bottom: 3px;
    }
    .metric span {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .progress {
      height: 10px;
      border-radius: 999px;
      background: #e8edf2;
      overflow: hidden;
      margin-bottom: 12px;
      border: 1px solid #d6dde6;
    }
    .progress-fill {
      width: 0%;
      height: 100%;
      background: var(--green);
      transition: width .18s ease;
    }
    .progress-text {
      margin: -4px 0 12px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      min-height: 16px;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: #111820;
      color: #f3f7fb;
      border-radius: 7px;
      padding: 14px;
      max-height: 440px;
      overflow: auto;
      font-size: 13px;
      line-height: 1.45;
    }
    .hidden { display: none; }
    .toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .cards-cta {
      width: 100%;
      min-height: 184px;
      border: 2px dashed #98a5b4;
      border-radius: 8px;
      background: #fff;
      color: #303946;
      cursor: pointer;
      padding: 14px 16px;
      display: grid;
      gap: 5px;
      text-align: left;
    }
    .cards-cta:disabled {
      cursor: not-allowed;
      background: #f6f8fa;
      color: #7a8795;
    }
    .cards-cta:not(:disabled) {
      border-color: #303946;
      background: linear-gradient(180deg, #fffef4, #fff);
      box-shadow: 0 12px 26px rgba(16, 24, 32, .08);
      transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
    }
    .cards-cta:not(:disabled):hover,
    .cards-cta:not(:disabled):focus {
      border-color: #0d8f56;
      box-shadow: 0 18px 34px rgba(16, 24, 32, .12);
      outline: none;
      transform: translateY(-1px);
    }
    .cards-cta strong {
      font-size: 16px;
      font-weight: 900;
    }
    .cards-cta span {
      font-size: 13px;
      font-weight: 800;
      line-height: 1.35;
    }
    .rank-mode-control {
      min-height: 54px;
      border: 1px solid #cbd3dd;
      border-radius: 8px;
      background: #fff;
      padding: 8px;
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      gap: 10px;
      align-items: center;
    }
    .rank-mode-control span {
      color: #627181;
      font-size: 13px;
      font-weight: 900;
      text-align: center;
    }
    .rank-mode-control span.active {
      color: var(--ink);
    }
    .rank-mode-control .switch {
      width: 42px;
      height: 24px;
      cursor: pointer;
    }
    .rank-mode-field {
      display: grid;
      grid-template-rows: minmax(56px, auto) auto;
      align-content: start;
    }
    .rank-mode-field > label {
      display: flex;
      align-items: flex-end;
      margin-bottom: 10px;
    }
    .number-stepper {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr) 42px;
      gap: 8px;
      align-items: center;
    }
    .number-stepper button {
      width: 42px;
      height: 42px;
      border: 1px solid #cbd3dd;
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
      font-size: 22px;
      font-weight: 900;
      line-height: 1;
    }
    .number-stepper button:hover {
      border-color: #303946;
      background: #fff9c9;
    }
    .number-stepper input {
      text-align: center;
      min-height: 42px;
      font-size: 18px;
      font-weight: 900;
      padding: 0 6px;
      appearance: textfield;
      -moz-appearance: textfield;
    }
    .number-stepper input::-webkit-outer-spin-button,
    .number-stepper input::-webkit-inner-spin-button {
      -webkit-appearance: none;
      margin: 0;
    }
    .quick-report-pills {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      margin-left: auto;
    }
    .quick-report-pill {
      min-height: 34px;
      border: 1px solid #cbd3dd;
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      padding: 0 12px;
      font-size: 12px;
      font-weight: 900;
      cursor: pointer;
      box-shadow: 0 8px 18px rgba(16, 24, 32, .08);
    }
    .quick-report-pill:hover {
      border-color: #303946;
      background: #fff9c9;
    }
    .quick-report-pill.hidden { display: none; }
    .quick-report-popover {
      position: fixed;
      top: 92px;
      right: 24px;
      z-index: 260;
      width: min(520px, calc(100vw - 32px));
      max-height: min(560px, calc(100vh - 116px));
      border: 1px solid rgba(16, 24, 32, .16);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 24px 80px rgba(16, 24, 32, .24);
      overflow: hidden;
    }
    .quick-report-popover.hidden { display: none; }
    .quick-report-popover-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid #e1e6ec;
    }
    .quick-report-popover-head h3 {
      margin: 0;
      color: #42505f;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .06em;
    }
    .quick-report-popover-head button {
      width: 28px;
      height: 28px;
      border: 1px solid #cbd3dd;
      border-radius: 50%;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
      font-weight: 900;
      line-height: 1;
    }
    .quick-report-popover pre {
      max-height: calc(min(560px, calc(100vh - 116px)) - 54px);
      margin: 0;
      padding: 14px;
      overflow: auto;
      border: 0;
      border-radius: 0;
      background: #fff;
      color: #26313d;
      font-family: inherit;
      font-size: 13px;
      font-weight: 700;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .card-stage {
      min-height: 560px;
      display: grid;
      place-items: center;
      padding: 18px 0 4px;
      overflow: hidden;
    }
    .card-stage.hidden { display: none; }
    .card-stage.card-overlay {
      position: fixed;
      inset: 0;
      z-index: 180;
      min-height: 100vh;
      overflow: hidden;
      padding: 12px 12px 14px;
      background: #f4f6f8;
      place-items: start center;
      overscroll-behavior: contain;
    }
    .card-overlay-close {
      position: fixed;
      top: 18px;
      right: 22px;
      z-index: 210;
      width: 44px;
      height: 44px;
      border: 1px solid #cbd3dd;
      border-radius: 50%;
      background: #fff;
      color: var(--ink);
      display: none;
      place-items: center;
      cursor: pointer;
      font-size: 24px;
      font-weight: 900;
      box-shadow: 0 10px 24px rgba(16, 24, 32, .14);
    }
    .card-stage.card-overlay .card-overlay-close {
      display: grid;
    }
    .vacancy-card {
      box-sizing: border-box;
      width: min(680px, calc(100vw - 44px));
      max-width: 100%;
      height: calc(100vh - 26px);
      min-height: calc(100vh - 26px);
      max-height: calc(100vh - 26px);
      overflow-x: hidden;
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      touch-action: pan-y;
      border: 1px solid #d4dbe4;
      border-radius: 24px;
      background:
        radial-gradient(circle at 88% 8%, rgba(255, 221, 45, .34), transparent 32%),
        linear-gradient(160deg, #ffffff 0%, #f8fafc 62%, #eef3f7 100%);
      box-shadow: 0 28px 70px rgba(16, 24, 32, .16);
      padding: 32px 30px 34px;
      display: flex;
      flex-direction: column;
      gap: 18px;
      position: relative;
      animation: cardIn .16s ease-out;
      will-change: transform, opacity;
    }
    .vacancy-card::before {
      content: "";
      position: absolute;
      inset: 10px;
      border: 1px solid rgba(16, 24, 32, .06);
      border-radius: 18px;
      pointer-events: none;
    }
    .vacancy-card.swipe-left { animation: swipeLeft .16s ease-in forwards; }
    .vacancy-card.swipe-right { animation: swipeRight .16s ease-in forwards; }
    .card-hint {
      position: absolute;
      z-index: 2;
      display: inline-grid;
      place-items: center;
      width: 38px;
      height: 38px;
      border-radius: 50%;
      border: 1px solid rgba(16, 24, 32, .12);
      background: rgba(255, 255, 255, .82);
      color: #111820;
      font-size: 22px;
      font-weight: 900;
      pointer-events: none;
      box-shadow: 0 10px 22px rgba(16, 24, 32, .12);
    }
    .card-hint-left {
      left: 12px;
      bottom: 38px;
      color: #b83232;
    }
    .card-hint-right {
      right: 12px;
      bottom: 38px;
      color: #08784c;
    }
    .card-hint-up {
      left: 50%;
      top: 4px;
      transform: translateX(-50%);
      color: #111820;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: start;
      position: relative;
      z-index: 80;
    }
    .card-title {
      font-size: clamp(24px, 3.7vh, 34px);
      line-height: 1.08;
      letter-spacing: 0;
      margin-bottom: 10px;
      overflow-wrap: anywhere;
    }
    .card-title-link {
      color: inherit;
      text-decoration: none;
    }
    .card-title-link[href]:hover {
      text-decoration: underline;
      text-decoration-thickness: 3px;
      text-underline-offset: 5px;
    }
    .card-company {
      color: #42505f;
      font-size: 16px;
      font-weight: 800;
    }
    .card-salary-line {
      color: #111820;
      font-size: 18px;
      font-weight: 900;
      margin-top: 8px;
    }
    .match-score {
      flex: 0 0 auto;
      border: 2px solid rgba(255, 221, 45, .95);
      border-radius: 999px;
      padding: 12px 18px;
      background: var(--ink);
      color: #fff;
      font-size: 20px;
      font-weight: 900;
      box-shadow: 0 10px 20px rgba(16, 24, 32, .16), 0 0 0 5px rgba(255, 221, 45, .24);
      cursor: pointer;
      transition: transform .14s ease, box-shadow .14s ease, background .14s ease;
    }
    .match-score:hover {
      transform: translateY(-1px);
      box-shadow: 0 14px 26px rgba(16, 24, 32, .20), 0 0 0 7px rgba(255, 221, 45, .34);
    }
    .match-score::after {
      content: "i";
      display: inline-grid;
      place-items: center;
      width: 18px;
      height: 18px;
      margin-left: 8px;
      border-radius: 50%;
      background: #ffdd2d;
      color: #111820;
      font-size: 13px;
      font-weight: 900;
    }
    .match-wrap {
      position: relative;
      z-index: 100;
    }
    .score-source-row {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      flex-wrap: nowrap;
    }
    .source-badge {
      flex: 0 0 42px;
      width: 42px;
      height: 42px;
      border-radius: 50%;
      display: inline-grid;
      place-items: center;
      border: 2px solid rgba(16, 24, 32, .12);
      background: rgba(255, 255, 255, .9);
      color: #111820;
      font-size: 12px;
      font-weight: 950;
      letter-spacing: -.02em;
      box-shadow: 0 10px 20px rgba(16, 24, 32, .12);
      text-transform: uppercase;
    }
    .source-badge.hh {
      background: #d6001c;
      color: #fff;
      border-color: rgba(214, 0, 28, .35);
    }
    .source-badge.superjob {
      background: #0a8f5a;
      color: #fff;
      border-color: rgba(10, 143, 90, .45);
    }
    .source-badge.other {
      background: #111820;
      color: #ffdd2d;
    }
    .card-facts {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      align-items: stretch;
    }
    .card-fact {
      border: 1px solid rgba(203, 211, 221, .8);
      border-radius: 14px;
      background: rgba(255, 255, 255, .78);
      padding: 10px 12px;
      min-height: 72px;
    }
    .card-fact b {
      display: block;
      color: #627181;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 5px;
    }
    .card-fact span {
      color: #182231;
      font-size: 15px;
      font-weight: 900;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .card-fact.hidden {
      display: none;
    }
    .skills-line {
      position: relative;
      z-index: 1;
      display: flex;
      align-items: flex-start;
      gap: 10px;
      overflow: visible;
      padding: 13px 14px;
      border: 1px solid rgba(203, 211, 221, .7);
      border-radius: 12px;
      background: rgba(255, 255, 255, .62);
      color: #26313d;
      font-size: 14px;
      font-weight: 800;
      line-height: 1.5;
      white-space: normal;
      min-height: 48px;
      overflow-wrap: anywhere;
    }
    .skills-line .skills-label {
      flex: 0 0 auto;
      color: #627181;
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .08em;
      line-height: 1.6;
      padding-top: 1px;
      white-space: nowrap;
    }
    .skills-line .skills-content {
      flex: 1 1 auto;
      min-width: 0;
      display: block;
      line-height: 1.5;
      overflow-wrap: anywhere;
    }
    .skills-line .matched-skill {
      color: #0a6d45;
    }
    .match-details {
      position: absolute;
      top: calc(100% + 10px);
      right: 0;
      z-index: 120;
      width: min(430px, calc(100vw - 40px));
      max-height: min(360px, calc(100vh - 160px));
      overflow: auto;
      border: 1px solid rgba(16, 24, 32, .14);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
      box-shadow: 0 22px 60px rgba(16, 24, 32, .25);
    }
    .match-details.hidden { display: none; }
    .match-details-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
    }
    .match-details h3 {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: #42505f;
      margin: 0;
    }
    .match-details-close {
      width: 26px;
      height: 26px;
      border: 1px solid #d5dde6;
      border-radius: 50%;
      background: #fff;
      color: #303946;
      cursor: pointer;
      font-weight: 900;
      line-height: 1;
    }
    .match-detail-list {
      display: grid;
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .match-detail-list li {
      display: grid;
      grid-template-columns: 54px minmax(0, 1fr);
      gap: 8px;
      color: #26313d;
      font-size: 13px;
      line-height: 1.35;
    }
    .match-points {
      font-weight: 900;
      color: #0d8f56;
    }
    .match-points.negative {
      color: #c83b3b;
    }
    .card-section {
      position: relative;
      z-index: 1;
      border: 1px solid rgba(203, 211, 221, .75);
      background: rgba(255, 255, 255, .76);
      border-radius: 16px;
      padding: 14px 16px;
      break-inside: avoid;
    }
    .card-section h3 {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: #627181;
      margin-bottom: 8px;
    }
    .card-section p {
      margin: 0;
      color: #26313d;
      line-height: 1.48;
    }
    .card-section.risks {
      border-color: rgba(200, 59, 59, .22);
      background: rgba(255, 246, 246, .78);
    }
    .raw-field-grid {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .raw-field {
      border: 1px solid rgba(203, 211, 221, .75);
      border-radius: 12px;
      background: rgba(255, 255, 255, .72);
      padding: 9px 11px;
      min-height: 54px;
    }
    .raw-field b {
      display: block;
      color: #627181;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .08em;
      margin-bottom: 4px;
    }
    .raw-field span {
      display: block;
      color: #26313d;
      font-size: 13px;
      font-weight: 800;
      line-height: 1.32;
      overflow-wrap: anywhere;
    }
    .card-actions {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-top: auto;
    }
    .swipe-button {
      min-height: 76px;
      border: 0;
      border-radius: 20px;
      font-weight: 900;
      cursor: pointer;
      display: grid;
      place-items: center;
      gap: 4px;
      color: #fff;
      box-shadow: 0 14px 26px rgba(16, 24, 32, .12);
    }
    .swipe-button .icon {
      font-size: 34px;
      line-height: 1;
    }
    .swipe-button.skip { background: linear-gradient(145deg, #d64a4a, #a92828); }
    .swipe-button.save { background: linear-gradient(145deg, #13a86b, #08784c); }
    .send-time-toggle {
      display: grid;
      grid-template-columns: auto 64px auto;
      align-items: center;
      gap: 10px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 900;
    }
    .send-time-toggle span.active {
      color: var(--ink);
    }
    .empty-card {
      width: min(680px, 100%);
      min-height: 360px;
      border: 2px dashed #cbd3dd;
      border-radius: 24px;
      display: grid;
      place-items: center;
      text-align: center;
      color: var(--muted);
      padding: 28px;
      background: rgba(255, 255, 255, .62);
    }
    .favorite-list {
      display: grid;
      gap: 14px;
    }
    .favorite-item {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 10px 22px rgba(16, 24, 32, .05);
      cursor: pointer;
      position: relative;
      transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease;
    }
    .favorite-item:hover,
    .favorite-item:focus {
      border-color: #98a5b4;
      box-shadow: 0 16px 34px rgba(16, 24, 32, .1);
      outline: none;
      transform: translateY(-1px);
    }
    .favorite-item h3 {
      margin-bottom: 6px;
      font-size: 18px;
      padding-right: 36px;
    }
    .favorite-meta {
      color: var(--muted);
      font-weight: 700;
      margin-bottom: 10px;
    }
    .favorite-item p {
      margin: 0;
      color: #303946;
      line-height: 1.45;
    }
    .favorite-remove {
      position: absolute;
      top: 12px;
      right: 12px;
      width: 34px;
      height: 34px;
      border: 0;
      border-radius: 50%;
      background: #eef2f6;
      color: #8793a1;
      font-size: 24px;
      line-height: 1;
      cursor: pointer;
    }
    .favorite-remove:hover,
    .favorite-remove:focus {
      background: #dde3ea;
      color: #536170;
      outline: none;
    }
    .files {
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      white-space: pre-line;
    }
    .select-line {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 42px;
      gap: 8px;
      align-items: center;
    }
    .select-line.criteria-line {
      grid-template-columns: minmax(0, 1fr) 42px 42px;
    }
    .select-line.vacancy-line {
      grid-template-columns: minmax(0, 1fr) 42px 42px;
    }
    .fetch-result-select {
      min-width: min(360px, 100%);
      max-width: 460px;
      flex: 1 1 320px;
      margin-left: auto;
    }
    .fetch-result-select select.placeholder {
      color: var(--muted);
      font-weight: 700;
    }
    .select-line.disabled {
      opacity: 0.55;
      filter: grayscale(1);
    }
    .select-line.disabled .icon-button,
    .select-line.disabled select {
      pointer-events: none;
    }
    .icon-button {
      width: 42px;
      height: 42px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      background: #fff;
      color: var(--ink);
      display: grid;
      place-items: center;
      cursor: pointer;
      padding: 0;
    }
    .icon-button:hover { border-color: #98a5b4; background: #f7f9fb; }
    .icon-button:disabled {
      cursor: not-allowed;
      opacity: .45;
      background: #eef2f6;
    }
    .icon-button svg {
      width: 18px;
      height: 18px;
      stroke-width: 2.2;
    }
    .rank-transfer-button {
      min-height: 42px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      background: #eef2f6;
      color: #7a8795;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 9px;
      cursor: not-allowed;
      font-weight: 900;
      padding: 0 18px;
      min-width: 190px;
      flex: 1 1 220px;
    }
    .rank-transfer-button svg {
      width: 18px;
      height: 18px;
      stroke-width: 2.2;
    }
    .rank-transfer-button:not(:disabled) {
      cursor: pointer;
      background: var(--ink);
      border-color: var(--ink);
      color: #fff;
      box-shadow: 0 10px 22px rgba(16, 24, 32, .12);
    }
    .rank-transfer-button:not(:disabled):hover {
      background: #26313d;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(16, 24, 32, .38);
      display: grid;
      place-items: center;
      padding: 20px;
      z-index: 300;
    }
    .modal {
      width: min(640px, 100%);
      background: #fff;
      border: 1px solid #cbd3dd;
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 24px 80px rgba(16, 24, 32, .24);
    }
    .modal.wide {
      width: min(860px, 100%);
      max-height: min(820px, calc(100vh - 40px));
      overflow: auto;
    }
    .modal-backdrop.hidden { display: none; }
    .modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 16px;
    }
    .modal-head h2 { margin-bottom: 0; }
    .modal label {
      color: var(--muted);
      font-size: 16px;
      margin-bottom: 8px;
    }
    .modal .field { margin-bottom: 16px; }
    .modal input { min-height: 54px; font-size: 22px; }
    .modal textarea { min-height: 120px; font-size: 22px; line-height: 1.25; }
    .criteria-prompt textarea { min-height: 220px; }
    .criteria-editor.hidden { display: none; }
    .criteria-editor {
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 14px;
    }
    .criteria-editor h3 {
      font-size: 15px;
      margin-bottom: 12px;
    }
    .criteria-form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .criteria-form-grid .wide-field { grid-column: 1 / -1; }
    .criteria-form-grid.filter-grid .filter-field-empty {
      color: var(--muted);
    }
    .criteria-form-grid.filter-grid .filter-field-empty label {
      color: #6f7b88;
      font-weight: 700;
    }
    .criteria-form-grid.filter-grid .filter-field-empty input,
    .criteria-form-grid.filter-grid .filter-field-empty .chip-editor,
    .criteria-form-grid.filter-grid .filter-field-empty .salary-control,
    .criteria-form-grid.filter-grid .filter-field-empty .choice-chip-group {
      border-style: dashed;
      border-color: #b9c4cf;
      background: #fbfcfd;
    }
    .criteria-form-grid.filter-grid .filter-field-empty .filter-state {
      color: #6f7b88;
      background: #eef2f6;
    }
    .criteria-form-grid.filter-grid .filter-field-filled label {
      color: var(--ink);
      font-weight: 900;
    }
    .criteria-form-grid.filter-grid .filter-field-filled input,
    .criteria-form-grid.filter-grid .filter-field-filled .chip-editor,
    .criteria-form-grid.filter-grid .filter-field-filled .salary-control,
    .criteria-form-grid.filter-grid .filter-field-filled .choice-chip-group {
      border-color: #303946;
      box-shadow: 0 0 0 2px rgba(48, 57, 70, .08);
    }
    .criteria-form-grid.filter-grid .filter-field-filled .criteria-chip {
      border-color: #303946;
      background: #fff4a6;
      color: var(--ink);
    }
    .filter-label-line {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }
    .filter-label-line label {
      margin-bottom: 0;
    }
    .criteria-priority-panel {
      display: grid;
      justify-items: end;
      gap: 6px;
      align-self: center;
      padding-top: 0;
    }
    .criteria-priority-hint {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      line-height: 1.25;
      text-align: right;
      max-width: 280px;
    }
    .criteria-toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      line-height: 1.2;
    }
    .criteria-toggle input {
      width: 16px;
      height: 16px;
      accent-color: var(--ink);
      margin: 0;
    }
    .criteria-form-grid .field.with-priority {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px 12px;
      align-items: start;
    }
    .criteria-form-grid .field.with-priority .filter-label-line,
    .criteria-form-grid .field.with-priority input,
    .criteria-form-grid .field.with-priority .chip-editor,
    .criteria-form-grid .field.with-priority .salary-control,
    .criteria-form-grid .field.with-priority .choice-chip-group,
    .criteria-form-grid .field.with-priority .criteria-toggle {
      grid-column: 1;
    }
    .criteria-form-grid .field.with-priority .criteria-priority-panel {
      grid-column: 2;
      grid-row: 1 / span 2;
      align-self: center;
    }
    .source-warning {
      margin-top: 8px;
      color: var(--red);
      font-size: 11px;
      font-weight: 800;
      line-height: 1.35;
    }
    .source-warning ul {
      margin: 4px 0 0 16px;
      padding: 0;
    }
    .source-warning.hidden {
      display: none;
    }
    .llm-mode-warning {
      margin-top: 8px;
      color: var(--red);
      font-size: 11px;
      font-weight: 800;
      line-height: 1.35;
    }
    .llm-mode-warning.hidden {
      display: none;
    }
    .filter-state {
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      font-weight: 900;
      line-height: 1;
      white-space: nowrap;
    }
    .filter-field-filled .filter-state {
      color: #0f2d1d;
      background: #d7f4df;
    }
    .chip-editor {
      min-height: 54px;
      border: 1px solid #cbd3dd;
      border-radius: 7px;
      background: #fff;
      padding: 8px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      position: relative;
    }
    .chip-editor:focus-within {
      outline: 2px solid rgba(255, 221, 45, .65);
      border-color: #a88b00;
    }
    .criteria-chip {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 30px;
      border: 1px solid #cbd3dd;
      border-radius: 999px;
      background: #eef2f6;
      color: #303946;
      font-size: 13px;
      font-weight: 800;
      padding: 0 8px 0 11px;
    }
    .criteria-chip button {
      width: 18px;
      height: 18px;
      border: 0;
      border-radius: 50%;
      background: #d8e0e8;
      color: #536170;
      cursor: pointer;
      line-height: 1;
      padding: 0;
    }
    .chip-editor input {
      flex: 1 1 150px;
      min-width: 120px;
      width: auto;
      min-height: 32px;
      border: 0;
      padding: 0 4px;
      font-size: 14px;
    }
    .chip-editor input:focus {
      outline: none;
      border-color: transparent;
    }
    .chip-suggestions {
      position: absolute;
      left: 8px;
      right: 8px;
      top: calc(100% + 6px);
      z-index: 20;
      border: 1px solid #cbd3dd;
      border-radius: 10px;
      background: #fff;
      box-shadow: 0 16px 35px rgba(25, 32, 42, .18);
      padding: 6px;
      display: grid;
      gap: 4px;
    }
    .chip-suggestions.hidden { display: none; }
    .chip-suggestion {
      border: 0;
      border-radius: 8px;
      background: transparent;
      color: #303946;
      cursor: pointer;
      font-size: 14px;
      font-weight: 850;
      min-height: 34px;
      padding: 0 10px;
      text-align: left;
    }
    .chip-suggestion:hover {
      background: #fff4a6;
    }
    .salary-control {
      border: 1px solid #cbd3dd;
      border-radius: 10px;
      background: #fff;
      padding: 12px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 120px;
      gap: 12px;
      align-items: center;
    }
    .salary-control input[type="range"] {
      width: 100%;
      min-height: 32px;
      padding: 0;
      accent-color: #303946;
    }
    .salary-control input[type="number"] {
      width: 100%;
      min-width: 0;
      min-height: 42px;
      font-size: 18px;
      padding: 8px 10px;
    }
    .salary-hints {
      grid-column: 1;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      width: 100%;
    }
    .salary-hints span:nth-child(2) {
      text-align: center;
    }
    .salary-hints span:last-child {
      text-align: right;
    }
    .choice-chip-group {
      min-height: 54px;
      border: 1px solid #cbd3dd;
      border-radius: 10px;
      background: #fff;
      padding: 8px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .choice-chip {
      border: 1px solid #cbd3dd;
      border-radius: 999px;
      background: #f3f6f9;
      color: #303946;
      min-height: 34px;
      padding: 0 13px;
      font-size: 13px;
      font-weight: 900;
      cursor: pointer;
    }
    .choice-chip:hover {
      border-color: #303946;
      background: #fff9c9;
    }
    .choice-chip.active {
      border-color: #303946;
      background: #ffdd2d;
      color: #111820;
      box-shadow: 0 3px 0 rgba(48, 57, 70, .18);
    }
    .modal .primary { min-height: 54px; font-size: 18px; }
    .modal-status {
      min-height: 20px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
      line-height: 1.35;
    }
    .modal-status.error { color: var(--red); }
    .modal-actions {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 54px;
      gap: 12px;
      align-items: stretch;
    }
    button.danger {
      width: 100%;
      min-height: 54px;
      border: 1px solid #b52b2b;
      border-radius: 7px;
      background: #c83b3b;
      color: #fff;
      font-weight: 900;
      cursor: pointer;
      display: grid;
      place-items: center;
      padding: 0;
    }
    button.danger:hover { background: #a92828; }
    button.danger svg {
      width: 22px;
      height: 22px;
      stroke-width: 2.2;
    }
    .detail-grid {
      display: grid;
      gap: 14px;
    }
    .source-detail-grid {
      display: grid;
      gap: 12px;
    }
    .source-detail-item {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 12px;
      background: #f8fafc;
    }
    .source-detail-item h3 {
      font-size: 15px;
      margin-bottom: 8px;
    }
    .source-detail-item p {
      margin: 0;
      color: #303946;
      line-height: 1.45;
      white-space: pre-line;
    }
    .detail-block {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #f8fafc;
      padding: 14px;
    }
    .detail-block h3 {
      font-size: 13px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .06em;
      margin-bottom: 8px;
    }
    .detail-block p {
      margin: 0;
      white-space: pre-wrap;
      line-height: 1.48;
    }
    .vacancy-link {
      display: inline-flex;
      width: fit-content;
      align-items: center;
      border-radius: 999px;
      background: var(--ink);
      color: #fff;
      font-weight: 900;
      padding: 12px 16px;
      text-decoration: none;
    }
    .vacancy-link.hidden { display: none; }
    .spinner {
      width: 16px;
      height: 16px;
      border: 2px solid rgba(16, 24, 32, .25);
      border-top-color: var(--ink);
      border-radius: 50%;
      animation: spin 1s linear infinite;
      display: none;
    }
    .busy .spinner { display: inline-block; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes cardIn {
      from { opacity: 0; transform: translateY(22px) scale(.97); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes swipeLeft {
      to { opacity: 0; transform: translateX(-120%) rotate(-12deg); }
    }
    @keyframes swipeRight {
      to { opacity: 0; transform: translateX(120%) rotate(12deg); }
    }
    body.dark-theme .theme-toggle {
      background: #253246;
      box-shadow: inset 0 0 0 1px #3a4a60;
    }
    body.dark-theme .theme-toggle-mark {
      background: #f2c94c;
      color: #101820;
    }
    body.dark-theme .theme-toggle input:checked + .theme-toggle-mark {
      background: #0d131c;
      color: #f2c94c;
      box-shadow: inset 0 0 0 1px rgba(242, 201, 76, .7);
    }
    body.dark-theme .mode-toggle {
      color: #95a3b5;
    }
    body.dark-theme .mode-toggle span.active,
    body.dark-theme .tab.active {
      color: #f2c94c;
    }
    body.dark-theme .tab.active {
      border-color: #f2c94c;
    }
    body.dark-theme .panel,
    body.dark-theme .modal,
    body.dark-theme .notification-panel,
    body.dark-theme .sources-menu,
    body.dark-theme .info-popover,
    body.dark-theme .quick-report-popover {
      background: #151f2c;
      border-color: #344255;
      box-shadow: 0 22px 56px rgba(0, 0, 0, .42);
    }
    body.dark-theme .quick-report-pill,
    body.dark-theme .quick-report-popover-head button {
      background: #172332;
      border-color: #344255;
      color: #f5f7fa;
    }
    body.dark-theme .quick-report-pill:hover {
      border-color: #f2c94c;
      background: #2d2a18;
    }
    body.dark-theme .quick-report-popover-head {
      border-color: #344255;
    }
    body.dark-theme .quick-report-popover pre {
      color: #dbe4ef;
    }
    body.dark-theme .notification-island {
      background: #f2c94c;
      color: #101820;
    }
    body.dark-theme input,
    body.dark-theme select,
    body.dark-theme textarea,
    body.dark-theme .sources-select,
    body.dark-theme .checkbox-group,
    body.dark-theme .rank-mode-control,
    body.dark-theme .keyword-chip,
    body.dark-theme .subscription-item,
    body.dark-theme .subscription-details,
    body.dark-theme .detail-block,
    body.dark-theme .source-detail-item,
    body.dark-theme .criteria-field,
    body.dark-theme .chip-editor,
    body.dark-theme .chip-suggestions,
    body.dark-theme .salary-control,
    body.dark-theme .choice-chip-group,
    body.dark-theme .cards-cta,
    body.dark-theme .cards-cta:disabled,
    body.dark-theme .empty-card,
    body.dark-theme .favorite-item,
    body.dark-theme .metric,
    body.dark-theme .card-fact,
    body.dark-theme .card-section,
    body.dark-theme .raw-field,
    body.dark-theme .match-details {
      background: #1d2a3a;
      color: #f5f7fa;
      border-color: #344255;
    }
    body.dark-theme input::placeholder,
    body.dark-theme textarea::placeholder {
      color: #7f8ea3;
    }
    body.dark-theme label,
    body.dark-theme .switchline,
    body.dark-theme .checkline,
    body.dark-theme .checkbox-option,
    body.dark-theme .source-category-title,
    body.dark-theme .subscription-title,
    body.dark-theme .notification-title,
    body.dark-theme .card-company,
    body.dark-theme .card-salary-line,
    body.dark-theme .card-section p,
    body.dark-theme .card-fact span,
    body.dark-theme .raw-field span,
    body.dark-theme .skills-line,
    body.dark-theme .match-detail-list li,
    body.dark-theme .favorite-meta,
    body.dark-theme .favorite-item p,
    body.dark-theme .modal-status,
    body.dark-theme .files {
      color: #f5f7fa;
    }
    body.dark-theme .notification-meta,
    body.dark-theme .notification-empty,
    body.dark-theme .metric span,
    body.dark-theme .card-fact b,
    body.dark-theme .card-section h3,
    body.dark-theme .raw-field b,
    body.dark-theme .skills-line .skills-label,
    body.dark-theme .match-details h3,
    body.dark-theme .source-detail-item h3,
    body.dark-theme .detail-block h3,
    body.dark-theme .subscription-meta {
      color: #a8b3c0;
    }
    body.dark-theme .notification-button,
    body.dark-theme .info-button,
    body.dark-theme .icon-button,
    body.dark-theme button.secondary,
    body.dark-theme .metadata-file-button,
    body.dark-theme .favorite-remove,
    body.dark-theme .match-details-close,
    body.dark-theme .keyword-remove,
    body.dark-theme .subscription-delete,
    body.dark-theme .number-stepper button,
    body.dark-theme .rank-transfer-button,
    body.dark-theme .chip-suggestion,
    body.dark-theme .criteria-chip {
      background: #223147;
      color: #f5f7fa;
      border-color: #3a4a60;
    }
    body.dark-theme .notification-item {
      background: #1d2a3a;
      color: #f5f7fa;
    }
    body.dark-theme .notification-action {
      background: #223147;
      color: #f5f7fa;
      border-color: #3a4a60;
    }
    body.dark-theme .notification-action.queue-chip {
      background: #f2c94c;
      color: #101820;
      border-color: #d8ad32;
    }
    body.dark-theme .notification-action.success {
      background: rgba(49, 196, 141, .16);
      color: #b8f7dc;
      border-color: rgba(49, 196, 141, .34);
    }
    body.dark-theme .notification-action.danger {
      color: #fca5a5;
      border-color: rgba(248, 113, 113, .34);
    }
    body.dark-theme .notification-item:hover,
    body.dark-theme .notification-item:focus,
    body.dark-theme .cards-cta:not(:disabled),
    body.dark-theme .rank-transfer-button:not(:disabled),
    body.dark-theme .icon-button:hover,
    body.dark-theme button.secondary:hover,
    body.dark-theme .number-stepper button:hover,
    body.dark-theme .chip-suggestion:hover,
    body.dark-theme .choice-chip:hover,
    body.dark-theme .metadata-file-button:hover,
    body.dark-theme .metadata-file-button:focus,
    body.dark-theme .favorite-item:hover,
    body.dark-theme .favorite-item:focus {
      background: #26364b;
      border-color: #f2c94c;
      outline-color: rgba(242, 201, 76, .55);
    }
    body.dark-theme .sources-select:disabled,
    body.dark-theme .sources-control.all-enabled .checkbox-group,
    body.dark-theme .select-line.disabled,
    body.dark-theme .criteria-empty,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-empty input,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-empty .chip-editor,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-empty .salary-control,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-empty .choice-chip-group,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-empty .filter-state {
      background: #172231;
      color: #7f8ea3;
      border-color: #2a3748;
    }
    body.dark-theme .switch-slider {
      background: #43536a;
    }
    body.dark-theme .switch-slider::before {
      background: #d8e1ec;
    }
    body.dark-theme .switch input:checked + .switch-slider,
    body.dark-theme button.primary,
    body.dark-theme .match-score,
    body.dark-theme .vacancy-link {
      background: #f2c94c;
      color: #101820;
    }
    body.dark-theme button.primary:hover {
      background: #ffd95f;
    }
    body.dark-theme .pill,
    body.dark-theme .progress,
    body.dark-theme .keyword-remove {
      background: #223147;
      color: #dbe5f0;
      border-color: #344255;
    }
    body.dark-theme pre {
      background: #0a1018;
      color: #dbe5f0;
      border: 1px solid #344255;
    }
    body.dark-theme .vacancy-card {
      border-color: #344255;
      background:
        radial-gradient(circle at 88% 8%, rgba(242, 201, 76, .18), transparent 32%),
        linear-gradient(160deg, #182231 0%, #151f2c 62%, #101820 100%);
      box-shadow: 0 28px 70px rgba(0, 0, 0, .48);
    }
    body.dark-theme .card-stage.card-overlay {
      background: #0d131c;
    }
    body.dark-theme .card-overlay-close {
      background: #223147;
      color: #f5f7fa;
      border-color: #3a4a60;
      box-shadow: 0 16px 34px rgba(0, 0, 0, .38);
    }
    body.dark-theme .vacancy-card::before {
      border-color: rgba(255, 255, 255, .08);
    }
    body.dark-theme .card-hint {
      background: #223147;
      color: #f5f7fa;
      border-color: #3a4a60;
    }
    body.dark-theme .card-hint-left { color: #f87171; }
    body.dark-theme .card-hint-right,
    body.dark-theme .skills-line .matched-skill,
    body.dark-theme .match-points { color: #31c48d; }
    body.dark-theme .card-hint-up { color: #f2c94c; }
    body.dark-theme .match-score {
      border-color: #f2c94c;
      box-shadow: 0 10px 20px rgba(0, 0, 0, .28), 0 0 0 5px rgba(242, 201, 76, .16);
    }
    body.dark-theme .match-score::after {
      background: #101820;
      color: #f2c94c;
    }
    body.dark-theme .card-section.risks {
      background: #2b2028;
      border-color: rgba(248, 113, 113, .35);
    }
    body.dark-theme .filter-field-filled .filter-state {
      background: rgba(49, 196, 141, .18);
      color: #b8f7dc;
    }
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-filled input,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-filled .chip-editor,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-filled .salary-control,
    body.dark-theme .criteria-form-grid.filter-grid .filter-field-filled .choice-chip-group,
    body.dark-theme .choice-chip.active {
      border-color: #f2c94c;
      background: #2d2a18;
      color: #f5f7fa;
      box-shadow: none;
    }
    body.dark-theme .match-points.negative,
    body.dark-theme .field-error,
    body.dark-theme .modal-status.error {
      color: #f87171;
    }
    body.dark-theme .modal-backdrop {
      background: rgba(0, 0, 0, .58);
    }
    @media (max-width: 900px) {
      header { padding: 0 16px; }
      header {
        grid-template-columns: auto minmax(0, 1fr) auto;
        column-gap: 10px;
      }
      .brand {
        gap: 0;
      }
      .brand-title-desktop {
        display: none;
      }
      .brand-title-mobile {
        display: inline;
      }
      .mode-toggle {
        gap: 6px;
        font-size: 11px;
        min-width: 0;
      }
      .mode-text-desktop {
        display: none;
      }
      .mode-text-mobile {
        display: inline;
      }
      .header-actions .pill {
        display: none;
      }
      .rank-mode-field {
        grid-template-rows: auto auto;
      }
      .rank-mode-field > label {
        min-height: 0;
        margin-bottom: 8px;
      }
      .rank-mode-control {
        grid-template-columns: minmax(52px, 1fr) auto minmax(52px, 1fr);
        gap: 8px;
        padding: 8px 10px;
      }
      .rank-mode-control span {
        font-size: 12px;
        line-height: 1.1;
      }
      .shell { padding: 16px; }
      .grid { grid-template-columns: 1fr; }
      .result-grid { grid-template-columns: 1fr; }
      .row { grid-template-columns: 1fr; }
      .criteria-form-grid { grid-template-columns: 1fr; }
      .card-stage { min-height: 620px; }
      .card-stage.card-overlay { padding: 10px 10px 12px; }
      .vacancy-card {
        width: min(680px, calc(100vw - 24px));
        height: calc(100vh - 22px);
        min-height: calc(100vh - 22px);
        max-height: calc(100vh - 22px);
        padding: 20px 18px 22px;
      }
      .card-title { font-size: clamp(22px, 3.5vh, 28px); }
      .card-head { display: block; }
      .match-score { display: inline-flex; margin-top: 14px; }
      .match-details { left: 0; right: auto; width: min(100%, calc(100vw - 40px)); }
      .card-facts { grid-template-columns: repeat(auto-fit, minmax(138px, 1fr)); }
      .raw-field-grid { grid-template-columns: 1fr; }
      .match-detail-list li { grid-template-columns: 46px minmax(0, 1fr); }
      .card-hint-up { top: 6px; }
      .card-hint-left { left: 6px; bottom: 32px; }
      .card-hint-right { right: 6px; bottom: 32px; }
    }
    body.card-overlay-open,
    html.card-overlay-open {
      overflow: hidden;
      height: 100%;
      touch-action: none;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <label class="theme-toggle" for="themeToggle" title="Сменить тему">
        <input id="themeToggle" type="checkbox" aria-label="Сменить тему">
        <span class="theme-toggle-mark">T</span>
      </label>
      <span class="brand-title-desktop">Vacancy Finder Service</span>
      <span class="brand-title-mobile">VFS</span>
    </div>
    <label class="mode-toggle" for="uiModeToggle">
      <span id="quickModeText"><span class="mode-text-desktop">Быстрый поиск</span><span class="mode-text-mobile">Быстро</span></span>
      <span class="switch">
        <input id="uiModeToggle" type="checkbox">
        <span class="switch-slider" aria-hidden="true"></span>
      </span>
      <span id="advancedModeText"><span class="mode-text-desktop">Расширенный</span><span class="mode-text-mobile">Расш.</span></span>
    </label>
    <div class="header-actions">
      <span class="pill hidden"><span class="dot" id="topDot"></span><span id="topState">Готов</span></span>
      <div class="notification-wrap" id="notificationWrap">
        <span class="notification-island" id="notificationIsland"></span>
        <button class="notification-button" id="notificationBell" type="button" aria-label="Открыть уведомления" title="Уведомления" aria-expanded="false">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"></path>
            <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"></path>
          </svg>
          <span class="notification-count" id="notificationCount"></span>
        </button>
        <section class="notification-panel hidden" id="notificationPanel" aria-label="Последние уведомления">
          <div class="notification-head">
            <h2>Уведомления</h2>
            <label class="notification-sound" title="Звук событий">
              <span>звук</span>
              <input id="notificationSoundToggle" type="checkbox" aria-label="Включить звук уведомлений">
            </label>
          </div>
          <div class="notification-list" id="notificationList"></div>
        </section>
      </div>
    </div>
  </header>

  <main class="shell">
    <div class="tabs">
      <button class="tab hidden" data-tab="quick">Быстрый поиск</button>
      <button class="tab active" data-tab="rank">Ранжирование</button>
      <button class="tab" data-tab="fetch">Сбор вакансий</button>
      <button class="tab" data-tab="email">Рассылки</button>
      <button class="tab" data-tab="favorites">Избранное</button>
    </div>

    <section id="quickTab" class="grid hidden">
      <form class="panel" id="quickForm">
        <h2>Быстрый поиск</h2>
        <div class="field">
          <label for="quickText">Пожелания соискателя</label>
          <textarea id="quickText" name="quick_text" placeholder="Например: ищу junior data analyst удаленно или гибрид, SQL и Python важны, зарплата от 80 000, без senior/lead, покажи top-15."></textarea>
          <div class="field-error hidden" id="quickTextError">Опишите, какую работу нужно найти.</div>
        </div>
        <button class="primary" type="submit">Найти вакансии</button>
      </form>

      <section class="panel">
        <div class="status">
          <h2>Результат</h2>
          <span class="queue-position hidden" id="quickQueuePosition"></span>
          <span class="pill"><span class="spinner"></span><span id="quickState">Ожидание</span></span>
          <div class="quick-report-pills" id="quickReportPills">
            <button class="quick-report-pill hidden" id="quickParamsReport" type="button" data-title="Отчет по выбранным параметрам">Параметры</button>
            <button class="quick-report-pill hidden" id="quickSourcesReport" type="button" data-title="Отчет по источникам">Источники</button>
          </div>
        </div>
        <div class="progress hidden" id="quickProgress"><div class="progress-fill" id="quickProgressFill"></div></div>
        <div class="progress-text hidden" id="quickProgressText">0% · Ожидание</div>
        <div class="toolbar">
          <button class="cards-cta" id="quickOpenCards" type="button" disabled>
            <strong>Карточки недоступны</strong>
            <span>Запустите быстрый поиск, затем здесь можно будет открыть карточки вакансий.</span>
          </button>
        </div>
        <span class="hidden" id="quickParamsReportText"></span>
        <span class="hidden" id="quickSourcesReportText"></span>
        <div class="quick-report-popover hidden" id="quickReportPopover">
          <div class="quick-report-popover-head">
            <h3 id="quickReportPopoverTitle">Отчет</h3>
            <button id="closeQuickReportPopover" type="button" aria-label="Закрыть">×</button>
          </div>
          <pre id="quickReportPopoverText"></pre>
        </div>
        <div class="card-stage hidden" id="quickCardStage"></div>
      </section>
    </section>

    <section id="emailTab" class="grid hidden">
      <form class="panel" id="emailForm">
        <h2>Рассылка</h2>
        <div class="field">
          <label for="emailSearchText">Пожелания соискателя</label>
          <textarea id="emailSearchText" name="email_search_text" placeholder="Например: ищу junior data analyst удаленно или гибрид, SQL и Python важны, зарплата от 80 000, без senior/lead."></textarea>
          <div class="field-error hidden" id="emailTextError">Опишите, какую работу нужно найти.</div>
        </div>
        <div class="row">
          <div class="field">
            <label for="emailTopK">Вакансий в сводке</label>
            <div class="number-stepper">
              <button id="emailTopKMinus" type="button" aria-label="Уменьшить число вакансий">−</button>
              <input id="emailTopK" name="email_top_k" type="number" min="1" max="20" value="5">
              <button id="emailTopKPlus" type="button" aria-label="Увеличить число вакансий">+</button>
            </div>
          </div>
          <div class="field">
            <label for="emailIntervalValue">Интервал</label>
            <div class="row">
              <input id="emailIntervalValue" name="email_interval_value" type="number" min="1" max="365" value="24">
              <select id="emailIntervalUnit" name="email_interval_unit">
                <option value="hours">часов</option>
                <option value="days">дней</option>
              </select>
            </div>
            <label class="send-time-toggle" for="emailSendLater">
              <span id="sendNowLabel" class="active">Отправить сразу</span>
              <span class="switch">
                <input id="emailSendLater" type="checkbox">
                <span class="switch-slider" aria-hidden="true"></span>
              </span>
              <span id="sendLaterLabel">Отправить потом</span>
            </label>
          </div>
        </div>
        <div class="field">
          <label for="emailInput">Почта</label>
          <div class="email-list" id="emailList"></div>
          <div class="keyword-line">
            <input id="emailInput" name="email" placeholder="name@example.com">
            <button class="icon-button" id="addEmail" type="button" aria-label="Добавить почту" title="Добавить">✓</button>
          </div>
        </div>
        <div class="field">
          <label for="emailThemeToggle">Тема письма</label>
          <div class="rank-mode-control">
            <span id="emailThemeLightLabel" class="active">светлая</span>
            <label class="switch" for="emailThemeToggle">
              <input id="emailThemeToggle" type="checkbox">
              <span class="switch-slider" aria-hidden="true"></span>
            </label>
            <span id="emailThemeDarkLabel">темная</span>
          </div>
        </div>
        <div class="field">
          <div class="telegram-field-head">
            <label for="telegramInput">Telegram</label>
            <div class="info-wrap">
              <button class="info-button" id="telegramInfoButton" type="button" aria-label="Как подключить Telegram" title="Как подключить Telegram">i</button>
              <div class="info-popover hidden" id="telegramInfoPopover">
                <p>Сначала откройте бота, нажмите Start и отправьте короткое сообщение о себе, например: AISearchJob Иван, backend стажировка.</p>
                <p>После этого добавьте сюда ваш @username или numeric chat_id.</p>
                <a id="telegramBotLink" href="#" target="_blank" rel="noopener noreferrer">Открыть Telegram-бота</a>
              </div>
            </div>
          </div>
          <div class="email-list" id="telegramList"></div>
          <div class="keyword-line">
            <input id="telegramInput" name="telegram" placeholder="@username или chat_id">
            <button class="icon-button" id="addTelegram" type="button" aria-label="Добавить Telegram" title="Добавить">✓</button>
          </div>
          <div class="field-error hidden" id="emailRecipientsError">Добавьте хотя бы один email или Telegram-получателя.</div>
        </div>
        <button class="primary" type="submit">Создать рассылку</button>
      </form>

      <section class="panel">
        <div class="status">
          <h2>Рассылки</h2>
          <span class="queue-position hidden" id="emailQueuePosition"></span>
          <span class="pill"><span class="spinner"></span><span id="emailState">Ожидание</span></span>
        </div>
        <div class="progress hidden" id="emailProgress"><div class="progress-fill" id="emailProgressFill"></div></div>
        <div class="progress-text hidden" id="emailProgressText">0% · Ожидание</div>
        <div class="subscription-list" id="subscriptionList"></div>
      </section>
    </section>

    <section id="rankTab" class="grid">
      <form class="panel" id="rankForm">
        <h2>Ранжирование CSV</h2>
        <div class="field">
          <label for="rankVacanciesFile">Источник вакансий</label>
          <select id="rankVacanciesFile" name="rank_vacancies" class="placeholder">
            <option value="">Источник вакансий</option>
          </select>
          <div class="files" id="selectedRankFileText"></div>
        </div>
        <div class="field">
          <label for="criteriaFile">Файл критериев</label>
          <div class="select-line criteria-line">
            <select id="criteriaFile" name="criteria"></select>
            <button class="icon-button" id="editCriteriaMeta" type="button" aria-label="Редактировать метаданные критериев" title="Редактировать метаданные">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <path d="M12 20h9"></path>
                <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path>
              </svg>
            </button>
            <button class="icon-button" id="openCriteriaPrompt" type="button" aria-label="Создать критерии из текста" title="Создать критерии из текста">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <path d="M12 5v14"></path>
                <path d="M5 12h14"></path>
              </svg>
            </button>
          </div>
          <div class="files" id="selectedCriteriaText"></div>
        </div>
        <div class="row">
          <div class="field rank-mode-field">
            <label for="rankScoreModeToggle">Подсчет очков</label>
            <input id="rankMode" name="mode" type="hidden" value="dry_run">
            <div class="rank-mode-control">
              <span id="rankScoreModeAutoLabel" class="active">auto</span>
              <label class="switch" for="rankScoreModeToggle">
                <input id="rankScoreModeToggle" type="checkbox" checked>
                <span class="switch-slider" aria-hidden="true"></span>
              </label>
              <span id="rankScoreModeLlmLabel">auto + LLM</span>
            </div>
          </div>
          <div class="field rank-mode-field">
            <label for="rankExplanationModeToggle">Генерация описания вакансии</label>
            <div class="rank-mode-control">
              <span id="rankExplanationModeAutoLabel" class="active">auto</span>
              <label class="switch" for="rankExplanationModeToggle">
                <input id="rankExplanationModeToggle" type="checkbox" checked>
                <span class="switch-slider" aria-hidden="true"></span>
              </label>
              <span id="rankExplanationModeLlmLabel">auto + LLM</span>
            </div>
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label for="topK">Целевое число карточек</label>
            <div class="number-stepper">
              <button id="topKMinus" type="button" aria-label="Уменьшить число карточек">−</button>
              <input id="topK" name="top_k" type="number" min="1" max="20" value="15">
              <button id="topKPlus" type="button" aria-label="Увеличить число карточек">+</button>
            </div>
          </div>
        </div>
        <button class="primary" type="submit">Запустить ранжирование</button>
      </form>

      <section class="panel">
        <div class="status">
          <h2>Результат</h2>
          <span class="queue-position hidden" id="rankQueuePosition"></span>
          <span class="pill"><span class="spinner"></span><span id="rankState">Ожидание</span></span>
        </div>
        <div class="progress hidden" id="rankProgress"><div class="progress-fill" id="rankProgressFill"></div></div>
        <div class="progress-text hidden" id="rankProgressText">0% · Ожидание</div>
        <div class="toolbar">
          <button class="cards-cta" id="openCards" type="button" disabled>
            <strong>Карточки недоступны</strong>
            <span>Запустите ранжирование, затем здесь можно будет открыть карточки вакансий.</span>
          </button>
        </div>
        <div class="card-stage hidden" id="cardStage"></div>
      </section>
    </section>

    <section id="fetchTab" class="grid hidden">
      <form class="panel" id="fetchForm">
        <h2>Сбор из интернета</h2>
        <div class="field">
          <label for="queryText">Поисковый запрос</label>
          <div class="keyword-list" id="keywordList"></div>
          <div class="keyword-line">
            <input id="queryText" name="text" placeholder="junior python developer">
            <button class="icon-button" id="addKeyword" type="button" aria-label="Добавить ключевое слово" title="Добавить">✓</button>
          </div>
          <div class="field-error hidden" id="keywordsError">Необходимо добавить хотя бы одно ключевое слово.</div>
        </div>
        <div class="field">
          <label class="switchline">
            <span>Жесткие фильтры</span>
            <span class="switch">
              <input id="hardFiltersToggle" type="checkbox">
              <span class="switch-slider" aria-hidden="true"></span>
            </span>
          </label>
        </div>
        <div class="field" id="fetchCriteriaField">
          <div class="select-line criteria-line" id="fetchCriteriaLine">
            <select id="fetchCriteriaFile" name="fetch_criteria"></select>
            <button class="icon-button" id="editFetchCriteriaMeta" type="button" aria-label="Редактировать метаданные критериев" title="Редактировать метаданные">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <path d="M12 20h9"></path>
                <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path>
              </svg>
            </button>
            <button class="icon-button" id="openFetchCriteriaPrompt" type="button" aria-label="Создать критерии из текста" title="Создать критерии из текста">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <path d="M12 5v14"></path>
                <path d="M5 12h14"></path>
              </svg>
            </button>
          </div>
          <div class="files" id="selectedFetchCriteriaText"></div>
        </div>
        <div class="row">
          <div class="field">
            <label for="maxVacancies">Целевое число вакансий</label>
            <input id="maxVacancies" name="max_vacancies" type="number" min="1" max="50" value="50">
          </div>
          <div class="field">
            <label id="sourcesLabel">Источники вакансий</label>
            <div class="sources-control" id="sourcesControl">
              <button class="sources-select" id="sourcesToggle" type="button" aria-haspopup="true" aria-expanded="false" aria-labelledby="sourcesLabel sourcesSummary">
                <span class="sources-select-text" id="sourcesSummary">Все источники</span>
                <span class="sources-select-arrow" aria-hidden="true">▾</span>
              </button>
              <div class="sources-menu hidden" id="sourcesMenu">
                <div class="checkbox-group" role="group" aria-labelledby="sourcesLabel">
                  <div class="source-category">
                    <div class="source-category-title">Основные универсальные</div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="hh" checked> <span>HH</span></label><div class="source-priority" data-source="hh" data-priority="high" aria-label="Приоритет HH"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="superjob" checked> <span>SuperJob</span></label><div class="source-priority" data-source="superjob" data-priority="medium" aria-label="Приоритет SuperJob"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="rabota_ru"> <span>Работа.ру</span></label><div class="source-priority" data-source="rabota_ru" data-priority="medium" aria-label="Приоритет Работа.ру"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="avito"> <span>Авито Работа</span></label><div class="source-priority" data-source="avito" data-priority="medium" aria-label="Приоритет Авито Работа"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="zarplata"> <span>Зарплата.ру</span></label><div class="source-priority" data-source="zarplata" data-priority="medium" aria-label="Приоритет Зарплата.ру"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                  </div>
                  <div class="source-category">
                    <div class="source-category-title">Агрегаторы с дублями</div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="gorodrabot"> <span>ГородРабот</span></label><div class="source-priority" data-source="gorodrabot" data-priority="low" aria-label="Приоритет ГородРабот"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="jooble"> <span>Jooble</span></label><div class="source-priority" data-source="jooble" data-priority="low" aria-label="Приоритет Jooble"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                  </div>
                  <div class="source-category">
                    <div class="source-category-title">IT, аналитика, студенты</div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="habr"> <span>Хабр Карьера</span></label><div class="source-priority" data-source="habr" data-priority="high" aria-label="Приоритет Хабр Карьера"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="geekjob"> <span>GeekJob</span></label><div class="source-priority" data-source="geekjob" data-priority="high" aria-label="Приоритет GeekJob"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                  </div>
                  <div class="source-category">
                    <div class="source-category-title">Государственный источник</div>
                    <div class="source-option"><label class="checkbox-option"><input type="checkbox" name="sources" value="trudvsem"> <span>Работа России</span></label><div class="source-priority" data-source="trudvsem" data-priority="medium" aria-label="Приоритет Работа России"><button class="priority-step low" type="button" data-priority="low" title="Низкий приоритет"></button><button class="priority-step medium" type="button" data-priority="medium" title="Средний приоритет"></button><button class="priority-step high" type="button" data-priority="high" title="Высокий приоритет"></button></div></div>
                  </div>
                </div>
              </div>
              <label class="switchline">
                <span>Включить все</span>
                <span class="switch">
                  <input id="allSourcesToggle" type="checkbox">
                  <span class="switch-slider" aria-hidden="true"></span>
                </span>
              </label>
              <div class="source-warning hidden" id="allSourcesWarning">
                Источники из списка ниже сейчас в альфа-тестировании и могут работать нестабильно:
                <ul id="unstableSourcesList"></ul>
              </div>
            </div>
            <div class="field-error hidden" id="sourcesError">Необходимо выбрать хотя бы один источник вакансий.</div>
          </div>
        </div>
        <label class="switchline">
          <span id="llmHtmlModeLabel">автоматический парсинг</span>
          <span class="switch">
            <input id="useLlmHtml" type="checkbox">
            <span class="switch-slider" aria-hidden="true"></span>
          </span>
        </label>
        <div class="files" id="llmHtmlModeHint">автоматический парсинг</div>
        <div class="llm-mode-warning hidden" id="llmHtmlModeWarning"></div>
        <button class="primary" type="submit">Собрать вакансии</button>
      </form>

      <section class="panel">
        <div class="status">
          <h2>Результат</h2>
          <span class="queue-position hidden" id="fetchQueuePosition"></span>
          <div class="select-line fetch-result-select vacancy-line">
            <select id="vacanciesFile" name="vacancies" class="placeholder">
              <option value="">Файл поиска</option>
            </select>
            <button class="icon-button" id="editVacancyMeta" type="button" aria-label="Редактировать метаданные вакансий" title="Редактировать метаданные">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <path d="M12 20h9"></path>
                <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path>
              </svg>
            </button>
            <button class="icon-button" id="openFetchDetails" type="button" disabled aria-label="Открыть отчет по источникам" title="Отчет по источникам">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <path d="M7 3h7l5 5v13H7z"></path>
                <path d="M14 3v6h5"></path>
                <path d="M10 13h6"></path>
                <path d="M10 17h6"></path>
              </svg>
            </button>
          </div>
          <button class="rank-transfer-button" id="rankCreated" type="button" disabled>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
              <path d="M21 3 10 14"></path>
              <path d="m21 3-7 18-4-7-7-4Z"></path>
            </svg>
            <span>отранжировать</span>
          </button>
          <span class="pill hidden"><span class="spinner"></span><span id="fetchState">Сбор</span></span>
        </div>
        <div class="files hidden" id="selectedFileText"></div>
        <div class="progress hidden" id="fetchProgress"><div class="progress-fill" id="fetchProgressFill"></div></div>
        <div class="progress-text hidden" id="fetchProgressText">0% · Ожидание</div>
        <div class="result-grid">
          <div class="metric"><b id="fetchRowsMetric">-</b><span>рассмотрено вакансий</span></div>
          <div class="metric"><b id="fetchTopSourceMetric">-</b><span id="fetchTopSourceLabel">топ-источник</span></div>
          <div class="metric"><b id="fetchMaxScoreMetric">-</b><span>максимальный Score</span></div>
        </div>
        <div class="toolbar">
          <button class="cards-cta" id="fetchOpenCards" type="button" disabled>
            <strong>Карточки недоступны</strong>
            <span>Выберите итоговый файл поиска или завершите сбор, затем здесь можно будет открыть карточки вакансий.</span>
          </button>
        </div>
        <div class="card-stage hidden" id="fetchCardStage"></div>
        <pre id="fetchOutput" class="hidden"></pre>
      </section>
    </section>

    <section id="favoritesTab" class="hidden">
      <section class="panel">
        <div class="status">
          <h2>Избранные вакансии</h2>
          <span class="pill"><span id="favoritesCount">0</span> сохранено</span>
        </div>
        <div class="favorite-list" id="favoritesList"></div>
        <div class="card-stage hidden" id="favoriteCardStage"></div>
      </section>
    </section>
  </main>

  <div class="modal-backdrop hidden" id="metadataModal">
    <section class="modal" id="metadataDialog" role="dialog" aria-modal="true" aria-labelledby="metadataModalTitle">
      <div class="modal-head">
        <h2 id="metadataModalTitle">Метаданные файла</h2>
        <button class="icon-button" id="closeMetadataModal" type="button" aria-label="Закрыть окно" title="Закрыть">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
          </svg>
        </button>
      </div>
      <div class="field">
        <label for="metadataName">Название</label>
        <input id="metadataName" maxlength="80">
      </div>
      <div class="field">
        <label for="metadataDescription">Описание</label>
        <textarea id="metadataDescription" maxlength="300"></textarea>
      </div>
      <div class="criteria-editor hidden" id="criteriaEditor">
        <h3>Значения критериев</h3>
        <div class="criteria-form-grid" id="criteriaEditorFields"></div>
      </div>
      <div class="modal-status" id="metadataStatus"></div>
      <div class="modal-actions">
        <button class="primary" id="saveMetadata" type="button">Сохранить</button>
        <button class="danger" id="deleteMetadataFile" type="button" aria-label="Удалить файл" title="Удалить файл">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M3 6h18"></path>
            <path d="M8 6V4h8v2"></path>
            <path d="M6 6l1 18h10l1-18"></path>
            <path d="M10 11v6"></path>
            <path d="M14 11v6"></path>
          </svg>
        </button>
      </div>
    </section>
  </div>

  <div class="modal-backdrop hidden" id="criteriaPromptModal">
    <section class="modal criteria-prompt" role="dialog" aria-modal="true" aria-labelledby="criteriaPromptTitle">
      <div class="modal-head">
        <h2 id="criteriaPromptTitle">Новые критерии</h2>
        <button class="icon-button" id="closeCriteriaPrompt" type="button" aria-label="Закрыть окно" title="Закрыть">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
          </svg>
        </button>
      </div>
      <div class="field">
        <label for="criteriaPromptText">Требования</label>
        <textarea id="criteriaPromptText" placeholder="Например: ищу junior data analyst удаленно или гибрид в Москве, зарплата от 80000, хочу SQL, Python и дашборды, senior и lead не подходят."></textarea>
      </div>
      <button class="primary" id="generateCriteria" type="button">Создать файл критериев</button>
      <div class="modal-status" id="criteriaPromptStatus"></div>
    </section>
  </div>

  <div class="modal-backdrop hidden" id="fetchCriteriaPromptModal">
    <section class="modal criteria-prompt" role="dialog" aria-modal="true" aria-labelledby="fetchCriteriaPromptTitle">
      <div class="modal-head">
        <h2 id="fetchCriteriaPromptTitle">Новые критерии для сбора</h2>
        <button class="icon-button" id="closeFetchCriteriaPrompt" type="button" aria-label="Закрыть окно" title="Закрыть">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
          </svg>
        </button>
      </div>
      <div class="field">
        <label for="fetchCriteriaPromptText">Требования</label>
        <textarea id="fetchCriteriaPromptText" placeholder="Например: ищу junior data analyst удаленно или гибрид в Москве, зарплата от 80000, хочу SQL, Python и дашборды, senior и lead не подходят."></textarea>
      </div>
      <button class="primary" id="generateFetchCriteria" type="button">Создать файл фильтров</button>
      <div class="modal-status" id="fetchCriteriaPromptStatus"></div>
    </section>
  </div>

  <div class="modal-backdrop hidden" id="fetchDetailsModal">
    <section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="fetchDetailsTitle">
      <div class="modal-head">
        <h2 id="fetchDetailsTitle">Подробный результат источников</h2>
        <button class="icon-button" id="closeFetchDetailsModal" type="button" aria-label="Закрыть окно" title="Закрыть">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
          </svg>
        </button>
      </div>
      <div class="source-detail-grid" id="fetchDetailsContent"></div>
    </section>
  </div>

  <div class="modal-backdrop hidden" id="favoriteModal">
    <section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="favoriteModalTitle">
      <div class="modal-head">
        <div>
          <h2 id="favoriteModalTitle">Вакансия</h2>
          <div class="favorite-meta" id="favoriteModalMeta"></div>
        </div>
        <button class="icon-button" id="closeFavoriteModal" type="button" aria-label="Закрыть окно" title="Закрыть">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
          </svg>
        </button>
      </div>
      <div class="detail-grid">
        <section class="detail-block">
          <h3>Описание (LLM)</h3>
          <p id="favoriteModalDescription"></p>
        </section>
        <section class="detail-block">
          <h3>Параметры</h3>
          <p id="favoriteModalLlm"></p>
        </section>
        <section class="detail-block">
          <h3>Риски / минусы</h3>
          <p id="favoriteModalRisks"></p>
        </section>
        <a class="vacancy-link hidden" id="favoriteModalLink" href="#" target="_blank" rel="noopener noreferrer">Открыть вакансию</a>
      </div>
    </section>
  </div>

  <div class="modal-backdrop hidden" id="notificationDetailsModal">
    <section class="modal wide" role="dialog" aria-modal="true" aria-labelledby="notificationDetailsTitle">
      <div class="modal-head">
        <div>
          <h2 id="notificationDetailsTitle">Подробный лог уведомления</h2>
          <div class="favorite-meta" id="notificationDetailsMeta"></div>
        </div>
        <button class="icon-button" id="closeNotificationDetailsModal" type="button" aria-label="Закрыть окно" title="Закрыть">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
          </svg>
        </button>
      </div>
      <div class="detail-grid">
        <section class="detail-block">
          <h3>Лог</h3>
          <pre id="notificationDetailsText" style="white-space: pre-wrap; word-break: break-word; max-height: 60vh; overflow: auto;"></pre>
        </section>
      </div>
    </section>
  </div>

  <script>
    const state = {
      files: [],
    criteriaFiles: [],
    filterFiles: [],
      lastReport: "",
      lastMethodology: "",
      lastTrace: "",
      lastRun: "",
      lastCreated: "",
      lastFetchResult: null,
      lastFetchRunResult: null,
      fetchFileTouched: false,
      fetchSummaryRequestId: 0,
      fetchCardsRequestId: 0,
      metadataKind: "",
    criteriaPromptBusy: false,
    fetchCriteriaPromptBusy: false,
    criteriaEditor: {},
      criteriaImportance: {},
      cards: [],
      rankCards: [],
      quickCards: [],
      fetchCards: [],
      cardIndex: 0,
      cardStageId: "cardStage",
      cardMode: "rank",
      rankView: "",
      quickView: "",
      fetchView: "",
      swiping: false,
      uiMode: "advanced",
      manualSources: [],
      keywords: ["junior analyst"],
      emailRecipients: [],
      telegramRecipients: [],
      telegramBot: {},
      subscriptions: [],
      expandedSubscriptions: {},
      progress: {},
      favorites: loadStoredFavorites(),
      notifications: loadStoredNotifications(),
      unreadNotifications: 0,
      notificationTimer: null,
      notificationSound: loadNotificationSound(),
      audioContext: null,
      queueJobs: [],
      activeQueueJob: null,
      queueJobSeq: 0,
      scopeQueueJobIds: { rank: "", fetch: "", quick: "" },
      rankViews: {
        cards: "",
        report: "",
        methodology: "",
        trace: "",
        run: ""
      },
      quickViews: {
        cards: "",
        report: "",
        methodology: "",
        trace: "",
        run: ""
      },
      fetchViews: {
        cards: ""
      }
    };

    const $ = (id) => document.getElementById(id);
    const criteriaFields = [
      { key: "target_roles", label: "Целевые роли", type: "chips", placeholder: "Добавить роль", toggles: [
        { key: "target_roles_use_description", label: "Учитывать в описании" }
      ] },
      { key: "min_salary", label: "Минимальная зарплата", type: "salary", placeholder: "80000", toggles: [
        { key: "salary_missing_penalty", label: "Штрафовать за отсутствие" }
      ] },
      { key: "english_level", label: "Английский", type: "multi-choice", options: ["A1", "A2", "A2+", "B1", "B2", "C1", "C2"] },
      { key: "preferred_levels", label: "Уровни", type: "multi-choice", options: ["Internship", "Entry", "Junior", "Middle", "Senior", "Lead"] },
      { key: "preferred_formats", label: "Формат работы", type: "multi-choice", options: [
        { value: "onsite", label: "Офис" },
        { value: "hybrid", label: "Гибрид" },
        { value: "remote", label: "Удаленка" },
        { value: "field", label: "Разъездной" }
      ] },
      { key: "preferred_cities", label: "Города", type: "chips", placeholder: "Москва, Удаленно" },
      { key: "skills", label: "Навыки", type: "chips", placeholder: "SQL, Python" },
      { key: "stop_words", label: "Стоп-слова", type: "chips", placeholder: "Senior, Lead" }
    ];
    const filterOnlyFields = [
      { key: "search_fields", label: "Искать должность в", type: "multi-choice", options: [
        { value: "name", label: "Названии вакансии" },
        { value: "description", label: "Описании вакансии" }
      ] },
      { key: "salary_defined", label: "Указан доход", type: "choice", options: [{ value: "yes", label: "Да" }] },
      { key: "working_hours", label: "Рабочие часы в день", type: "multi-choice", options: [
        { value: "2", label: "2 ч" },
        { value: "3", label: "3 ч" },
        { value: "4", label: "4 ч" },
        { value: "5", label: "5 ч" },
        { value: "6", label: "6 ч" },
        { value: "7", label: "7 ч" },
        { value: "8", label: "8 ч" },
        { value: "9", label: "9 ч" },
        { value: "10", label: "10 ч" },
        { value: "11", label: "11 ч" },
        { value: "12", label: "12 ч" },
        { value: "24", label: "24 ч" },
        { value: "flexible", label: "Гибко" },
        { value: "other", label: "Другое" }
      ] },
      { key: "employment_contract", label: "Оформление", type: "multi-choice", options: [
        { value: "labor_contract", label: "Трудовой договор" },
        { value: "gph_or_part_time", label: "ГПХ/совместительство" }
      ] },
      { key: "accredited_it", label: "Аккредитованная ИТ-компания", type: "choice", options: [{ value: "yes", label: "Да" }] }
    ];
    const filterFields = criteriaFields
      .filter((field) => !["target_roles", "skills"].includes(field.key))
      .map(({ toggles, ...field }) => field)
      .concat(filterOnlyFields);
    const unstableSourceMap = {
      rabota_ru: "Работа.ру",
      avito: "Авито Работа",
      zarplata: "Зарплата.ру",
      gorodrabot: "ГородРабот",
      jooble: "Jooble",
      habr: "Хабр Карьера",
      geekjob: "GeekJob",
      trudvsem: "Работа России"
    };
    const importanceDefaults = {
      min_salary: "low"
    };
    const importanceConfig = {
      low: {
        label: "Низкая важность",
        hint: "Бонус x0.7; при выходе за границу штраф -3. Зарплата: до -4 пропорционально недостаче."
      },
      medium: {
        label: "Средняя важность",
        hint: "Бонус x1.0; при выходе за границу штраф -7. Зарплата: до -9 пропорционально недостаче."
      },
      high: {
        label: "Высокая важность",
        hint: "Бонус x1.35; при выходе за границу штраф -12. Зарплата: до -16 пропорционально недостаче."
      }
    };
    const citySuggestions = [
      "Москва",
      "Санкт-Петербург",
      "Казань",
      "Екатеринбург",
      "Новосибирск",
      "Нижний Новгород",
      "Самара",
      "Ростов-на-Дону",
      "Краснодар",
      "Воронеж",
      "Пермь",
      "Уфа",
      "Омск",
      "Красноярск",
      "Челябинск",
      "Удаленно"
    ];
    const searchPlaceholders = [
      "junior python developer",
      "стажировка аналитик данных",
      "backend developer удаленно",
      "product analyst junior",
      "data engineer internship",
      "системный аналитик junior",
      "frontend react стажер",
      "qa engineer junior",
      "ml engineer intern",
      "technical support engineer",
      "golang developer junior",
      "business analyst стажировка",
      "python django junior",
      "sql analyst удаленно",
      "devops intern linux",
      "java developer junior",
      "data scientist intern",
      "1c junior developer",
      "bi analyst junior",
      "стажер разработчик python"
    ];
    const candidateWishPlaceholders = [
      "Ищу junior data analyst удаленно или гибрид, SQL и Python важны, зарплата от 80 000, без senior/lead, покажи top-15.",
      "Нужна стажировка backend Python в Москве или удаленно, FastAPI/Django плюс, без продаж и техподдержки, top-10.",
      "Ищу frontend React junior, можно без коммерческого опыта, важны TypeScript и верстка, зарплата от 70 000.",
      "Хочу entry-level product analyst, SQL обязателен, Python и A/B тесты как преимущество, офис Москва или гибрид.",
      "Найди стажировку ML engineer, Python и PyTorch важны, рассматриваю Москву и удаленку, не senior и не lead.",
      "Ищу junior QA manual/automation, обязательно удаленно, Python или Java желательны, зарплата от 60 000.",
      "Нужна позиция системного аналитика junior, UML/BPMN и SQL важны, гибрид в Санкт-Петербурге, top-12.",
      "Ищу data engineer internship, Python и SQL обязательны, Airflow/Spark как плюс, без опыта от 3 лет.",
      "Хочу junior backend Java, Spring желательно, Москва или Казань, зарплата от 90 000, без fullstack.",
      "Найди junior DevOps/Linux, Docker обязателен, Kubernetes как плюс, удаленно или гибрид, без senior.",
      "Ищу стажировку бизнес-аналитика, Excel и SQL важны, можно офис Москва, зарплата от 50 000.",
      "Нужна junior BI analyst роль, Power BI/Tableau и SQL, удаленно, не требовать английский выше B1.",
      "Ищу junior Go developer, backend задачи, удаленно, зарплата от 100 000, без вакансий с C++.",
      "Хочу стажировку 1C разработчика, Москва или удаленно, обучение внутри компании важно, top-10.",
      "Найди technical support engineer с ростом в DevOps, Linux и SQL важны, смены 2/2 не подходят.",
      "Ищу junior UX/UI researcher, аналитика пользователей и интервью, Москва гибрид, без чистого дизайна.",
      "Нужна internship data scientist, Python/sklearn важны, NLP как плюс, зарплата от 70 000.",
      "Ищу junior mobile developer Flutter, удаленно, pet-проекты есть, без требований 3+ лет опыта.",
      "Хочу junior C#/.NET backend, SQL Server и ASP.NET важны, офис или гибрид в Москве.",
      "Найди стажировку в аналитике рисков, SQL и Excel обязательны, банки подходят, зарплата от 60 000.",
      "Ищу junior project coordinator в IT, английский B1, Jira/Confluence плюс, удаленно или гибрид.",
      "Нужна junior NLP engineer роль, Python и transformers важны, можно research internship, top-15.",
      "Ищу junior security analyst/SOC, Linux и сети важны, ночные смены не подходят, Москва.",
      "Хочу backend internship Node.js, TypeScript и PostgreSQL, удаленно, без требований production опыта.",
      "Найди junior game analyst, SQL и продуктовые метрики, удаленно, зарплата от 80 000.",
      "Ищу стажировку тестировщика игр, внимательность и баг-репорты, удаленно или Москва, без продаж.",
      "Нужна junior database developer, SQL обязателен, PostgreSQL плюс, Москва или удаленно.",
      "Ищу junior AI prompt engineer/LLM evaluator, Python плюс, удаленно, английский B2 желательно.",
      "Хочу junior support analyst, SQL и логирование важны, график 5/2, без холодных звонков.",
      "Найди стажировку robotics/embedded, C/C++ и Linux важны, Москва, зарплата от 60 000."
    ];
    const emailWishPlaceholders = [
      "Для рассылки: junior data analyst удаленно или гибрид, SQL/Python, зарплата от 80 000, без senior/lead.",
      "Для регулярного поиска: стажировка backend Python, Москва или удаленно, Django/FastAPI, без техподдержки.",
      "Ищу для рассылки frontend React junior, TypeScript и верстка важны, можно без коммерческого опыта.",
      "Присылай product analyst junior, SQL обязателен, Python и A/B тесты как плюс, Москва гибрид.",
      "Для рассылки нужны ML internship вакансии, Python/PyTorch, Москва или удаленно, не senior.",
      "Ищи junior QA automation, удаленно, Python или Java, зарплата от 60 000, без сменного графика.",
      "Рассылка по junior системному аналитику: SQL, BPMN/UML, Санкт-Петербург или гибрид.",
      "Присылай data engineer internship, Python/SQL, Airflow как плюс, без опыта от 3 лет.",
      "Junior backend Java, Spring, Москва или Казань, зарплата от 90 000, без fullstack.",
      "Junior DevOps/Linux, Docker обязателен, Kubernetes плюс, удаленно или гибрид.",
      "Стажировка бизнес-аналитика, Excel и SQL, Москва, зарплата от 50 000.",
      "Junior BI analyst, Power BI или Tableau, SQL, удаленно, английский до B1 подходит.",
      "Junior Go backend developer, удаленно, зарплата от 100 000, без C++ вакансий.",
      "Стажировка 1C разработчика, Москва или удаленно, обучение внутри компании важно.",
      "Technical support engineer с переходом в DevOps, Linux/SQL, без смен 2/2.",
      "Junior UX/UI researcher, интервью и аналитика пользователей, Москва гибрид.",
      "Data scientist internship, Python/sklearn, NLP плюс, зарплата от 70 000.",
      "Junior Flutter developer, удаленно, pet-проекты есть, без требования 3+ лет.",
      "Junior C#/.NET backend, ASP.NET и SQL Server, Москва офис или гибрид.",
      "Стажировка аналитика рисков, SQL/Excel, банки подходят, зарплата от 60 000.",
      "Junior IT project coordinator, Jira/Confluence, английский B1, удаленно или гибрид.",
      "Junior NLP engineer, Python и transformers, research internship подходит.",
      "Junior SOC/security analyst, Linux и сети, Москва, без ночных смен.",
      "Backend internship Node.js, TypeScript/PostgreSQL, удаленно, без production опыта.",
      "Junior game analyst, SQL и продуктовые метрики, удаленно, зарплата от 80 000.",
      "Стажировка QA game tester, баг-репорты, удаленно или Москва, без продаж.",
      "Junior database developer, PostgreSQL и SQL, Москва или удаленно.",
      "Junior LLM evaluator или prompt engineer, Python плюс, удаленно, английский B2.",
      "Junior support analyst, SQL и логи, график 5/2, без холодных звонков.",
      "Robotics/embedded internship, C/C++ и Linux, Москва, зарплата от 60 000."
    ];
    let placeholderIndex = 0;
    let quickWishPlaceholderIndex = 0;
    let emailWishPlaceholderIndex = 0;

    document.querySelectorAll(".tab").forEach((button) => {
      button.addEventListener("click", () => switchTab(button.dataset.tab));
    });

    function switchTab(tab) {
      const allowed = state.uiMode === "quick" ? ["quick"] : ["rank", "fetch", "email", "favorites"];
      const target = allowed.includes(tab) ? tab : allowed[0];
      document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.tab === target));
      $("quickTab").classList.toggle("hidden", target !== "quick");
      $("rankTab").classList.toggle("hidden", target !== "rank");
      $("emailTab").classList.toggle("hidden", target !== "email");
      $("fetchTab").classList.toggle("hidden", target !== "fetch");
      $("favoritesTab").classList.toggle("hidden", target !== "favorites");
      if (target === "favorites") renderFavorites();
      if (target === "email") loadSubscriptions();
    }

    function activeTab() {
      return document.querySelector(".tab.active")?.dataset.tab || "rank";
    }

    function applyUiMode() {
      const quick = !$("uiModeToggle").checked;
      state.uiMode = quick ? "quick" : "advanced";
      $("quickModeText").classList.toggle("active", quick);
      $("advancedModeText").classList.toggle("active", !quick);
      document.querySelector('.tab[data-tab="quick"]').classList.toggle("hidden", !quick);
      document.querySelector('.tab[data-tab="rank"]').classList.toggle("hidden", quick);
      document.querySelector('.tab[data-tab="email"]').classList.toggle("hidden", quick);
      document.querySelector('.tab[data-tab="fetch"]').classList.toggle("hidden", quick);
      document.querySelector('.tab[data-tab="favorites"]').classList.toggle("hidden", quick);
      switchTab(quick ? "quick" : "rank");
    }

    function applyTheme() {
      const dark = $("themeToggle").checked;
      document.body.classList.toggle("dark-theme", dark);
      try {
        window.localStorage.setItem("vacancyFinderTheme", dark ? "dark" : "light");
      } catch (error) {
        return;
      }
    }

    function restoreTheme() {
      try {
        $("themeToggle").checked = window.localStorage.getItem("vacancyFinderTheme") === "dark";
      } catch (error) {
        $("themeToggle").checked = false;
      }
      applyTheme();
    }

    function rotateSearchPlaceholder() {
      placeholderIndex = rotatePlaceholder("queryText", searchPlaceholders, placeholderIndex);
    }

    function rotatePlaceholder(inputId, placeholders, currentIndex) {
      const input = $(inputId);
      if (!input || !placeholders.length || document.activeElement === input) return currentIndex;
      input.classList.add("placeholder-fade");
      const nextIndex = (currentIndex + 1) % placeholders.length;
      window.setTimeout(() => {
        input.placeholder = placeholders[nextIndex];
        input.classList.remove("placeholder-fade");
      }, 280);
      return nextIndex;
    }

    function rotateCandidateWishPlaceholders() {
      quickWishPlaceholderIndex = rotatePlaceholder("quickText", candidateWishPlaceholders, quickWishPlaceholderIndex);
      emailWishPlaceholderIndex = rotatePlaceholder("emailSearchText", emailWishPlaceholders, emailWishPlaceholderIndex);
    }

    function updateSendTimeToggle() {
      const later = $("emailSendLater").checked;
      $("sendNowLabel").classList.toggle("active", !later);
      $("sendLaterLabel").classList.toggle("active", later);
    }

    function updateEmailThemeToggle() {
      const dark = $("emailThemeToggle").checked;
      $("emailThemeLightLabel").classList.toggle("active", !dark);
      $("emailThemeDarkLabel").classList.toggle("active", dark);
    }

    async function loadFiles(selectedPath = "", selectedCriteriaPath = "", selectedFilterPath = "", selectedRankPath = "") {
      const response = await fetch("/api/files");
      const data = await response.json();
      state.files = data.files || [];
      state.criteriaFiles = data.criteria_files || (data.criteria ? [data.criteria] : []);
      state.filterFiles = data.filter_files || (data.filter ? [data.filter] : []);
      const select = $("vacanciesFile");
      const rankSelect = $("rankVacanciesFile");
      const criteriaSelect = $("criteriaFile");
      const fetchCriteriaSelect = $("fetchCriteriaFile");
      const previousFetchValue = select.value;
      const previousRankValue = rankSelect.value;
      select.innerHTML = "";
      rankSelect.innerHTML = "";
      criteriaSelect.innerHTML = "";
      fetchCriteriaSelect.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Файл поиска";
      select.appendChild(placeholder);
      const rankPlaceholder = document.createElement("option");
      rankPlaceholder.value = "";
      rankPlaceholder.textContent = "Источник вакансий";
      rankSelect.appendChild(rankPlaceholder);
      state.files.forEach((file) => {
        const option = document.createElement("option");
        option.value = file.path;
        option.textContent = file.name;
        select.appendChild(option);
        const rankOption = document.createElement("option");
        rankOption.value = file.path;
        rankOption.textContent = file.name;
        rankSelect.appendChild(rankOption);
      });
      state.criteriaFiles.forEach((file) => {
        const option = document.createElement("option");
        option.value = file.path;
        option.textContent = file.name;
        criteriaSelect.appendChild(option);
      });
      state.filterFiles.forEach((file) => {
        const option = document.createElement("option");
        option.value = file.path;
        option.textContent = file.name;
        fetchCriteriaSelect.appendChild(option);
      });
      if (selectedPath) select.value = selectedPath;
      if (!selectedPath && state.fetchFileTouched && previousFetchValue) select.value = previousFetchValue;
      if (!selectedPath && !state.fetchFileTouched) select.value = "";
      if (selectedRankPath) rankSelect.value = selectedRankPath;
      if (!selectedRankPath && previousRankValue) rankSelect.value = previousRankValue;
      if (selectedCriteriaPath) criteriaSelect.value = selectedCriteriaPath;
      if (selectedFilterPath) fetchCriteriaSelect.value = selectedFilterPath;
      if (!fetchCriteriaSelect.value && state.filterFiles[0]) fetchCriteriaSelect.value = state.filterFiles[0].path;
      updateFileMeta();
      updateFetchCriteriaUiState();
    }

    function updateFileMeta() {
      const selected = state.files.find((file) => file.path === $("vacanciesFile").value);
      const selectedRank = state.files.find((file) => file.path === $("rankVacanciesFile").value);
      const selectedCriteria = state.criteriaFiles.find((file) => file.path === $("criteriaFile").value);
      const selectedFetchCriteria = state.filterFiles.find((file) => file.path === $("fetchCriteriaFile").value);
      $("vacanciesFile").classList.toggle("placeholder", !selected);
      $("rankVacanciesFile").classList.toggle("placeholder", !selectedRank);
      $("selectedFileText").textContent = selected ? buildFileText(selected) : "";
      $("selectedRankFileText").textContent = selectedRank ? buildFileText(selectedRank) : "";
      $("selectedCriteriaText").textContent = selectedCriteria ? buildFileText(selectedCriteria) : "";
      $("selectedFetchCriteriaText").textContent = selectedFetchCriteria ? buildFileText(selectedFetchCriteria) : "";
      updateFetchFileSummary(selected ? selected.path : "");
      updateRankTransferButton();
    }

    function buildFileText(file) {
      return [file.created, file.description].join("\n");
    }

    function resetFetchFilePanel() {
      $("fetchRowsMetric").textContent = "-";
      $("fetchTopSourceMetric").textContent = "-";
      $("fetchTopSourceLabel").textContent = "топ-источник";
      $("fetchMaxScoreMetric").textContent = "-";
      $("rankCreated").disabled = true;
      $("openFetchDetails").disabled = true;
      state.fetchCards = [];
      updateCardsCta("fetch");
      if (!state.lastFetchResult) $("fetchOutput").textContent = "";
    }

    async function updateFetchFileSummary(path) {
      const requestId = ++state.fetchSummaryRequestId;
      if (!path) {
        state.lastFetchResult = null;
        state.fetchCards = [];
        resetFetchFilePanel();
        return;
      }
      $("rankCreated").disabled = true;
      $("openFetchDetails").disabled = true;
      try {
        const response = await fetch(`/api/vacancy-file-summary?path=${encodeURIComponent(path)}`);
        const result = await response.json();
        if (requestId !== state.fetchSummaryRequestId) return;
        if (!result.ok) {
          state.lastFetchResult = result;
          resetFetchFilePanel();
          $("fetchOutput").textContent = JSON.stringify(result, null, 2);
          return;
        }
        applyFetchSummary(result);
        await loadFetchCards(path);
      } catch (error) {
        if (requestId !== state.fetchSummaryRequestId) return;
        state.lastFetchResult = { ok: false, error: String(error) };
        resetFetchFilePanel();
        $("fetchOutput").textContent = JSON.stringify(state.lastFetchResult, null, 2);
      }
    }

    function applyFetchSummary(result) {
      const trace = result.trace_summary || {};
      const metrics = trace.fetch_metrics || {};
      const runResult = state.lastFetchRunResult || {};
      const displayResult = result.path && result.path === state.lastCreated && runResult.created_path === result.path
        ? { ...runResult, path: result.path, rows: result.rows, trace_path: result.trace_path, trace_summary: result.trace_summary, source_breakdown: result.source_breakdown }
        : result;
      state.lastFetchResult = displayResult;
      $("fetchRowsMetric").textContent = metrics.total_considered ?? trace.raw_rows ?? result.rows ?? "-";
      updateFetchSourceMetric(result);
      $("fetchMaxScoreMetric").textContent = maxScoreText(displayResult.card_vacancies || []);
      $("rankCreated").disabled = !result.path;
      $("openFetchDetails").disabled = (!trace || !Object.keys(trace).length) && !Object.keys(result.source_breakdown || {}).length;
      $("fetchOutput").textContent = JSON.stringify(displayResult, null, 2);
      updateRankTransferButton();
    }

    async function loadFetchCards(path) {
      const requestId = ++state.fetchCardsRequestId;
      state.fetchCards = [];
      updateCardsCta("fetch");
      if (!path) return;
      try {
        const response = await fetch(`/api/vacancy-file-cards?path=${encodeURIComponent(path)}`);
        const result = await response.json();
        if (requestId !== state.fetchCardsRequestId) return;
        state.fetchCards = result.ok ? normalizeRawCsvCards(result.cards || []) : [];
        if ($("fetchMaxScoreMetric").textContent === "-") {
          $("fetchMaxScoreMetric").textContent = maxScoreText(state.fetchCards);
        }
        updateCardsCta("fetch");
      } catch (error) {
        if (requestId !== state.fetchCardsRequestId) return;
        state.fetchCards = [];
        updateCardsCta("fetch");
      }
    }

    function updateFetchSourceMetric(result) {
      const breakdown = aggregateSourceMap(result.source_breakdown || {});
      const entries = Object.entries(breakdown).sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0));
      if (!entries.length) {
        $("fetchTopSourceMetric").textContent = "-";
        $("fetchTopSourceLabel").textContent = "топ-источник";
        return;
      }
      const [source, count] = entries[0];
      $("fetchTopSourceMetric").textContent = String(count);
      $("fetchTopSourceLabel").textContent = sourceDisplayName(source);
    }

    function aggregateSourceMap(mapping) {
      const aggregated = {};
      Object.entries(mapping || {}).forEach(([source, count]) => {
        const key = baseSourceName(source);
        aggregated[key] = (aggregated[key] || 0) + Number(count || 0);
      });
      return aggregated;
    }

    function baseSourceName(source) {
      const normalized = String(source || "unknown").trim().toLowerCase();
      if (!normalized || normalized === "unknown") return "unknown";
      if (normalized === "sj" || normalized.startsWith("superjob")) return "superjob";
      if (normalized === "hh" || normalized.startsWith("hh-") || normalized.startsWith("hh_")) return "hh";
      return normalized.replace(/-(?:llm-html|mixed-html|html-detail|api-detail|html|json|detail)$/i, "") || "unknown";
    }

    function sourceDisplayName(source) {
      return {
        hh: "HH",
        superjob: "SuperJob",
        rabota_ru: "Работа.ру",
        avito: "Авито",
        zarplata: "Зарплата.ру",
        gorodrabot: "ГородРабот",
        jooble: "Jooble",
        habr: "Хабр",
        geekjob: "GeekJob",
        trudvsem: "Работа России"
      }[source] || source;
    }

    function maxScoreText(cards) {
      const scores = (cards || [])
        .map((card) => card?.score ?? card?.match_score ?? "")
        .filter((score) => String(score).trim() !== "")
        .map((score) => Number(score))
        .filter((score) => Number.isFinite(score));
      if (!scores.length) return "-";
      return formatScore(Math.max(...scores));
    }

    function updateRankTransferButton() {
      const selected = state.files.find((file) => file.path === $("vacanciesFile").value);
      $("rankCreated").disabled = !selected;
    }

    function setTopState(text, state = null) {
      $("topState").textContent = text;
      let suffix = "";
      if (state === true || state === "ok" || state === "success") suffix = " ok";
      if (state === false || state === "err" || state === "error" || state === "empty") suffix = " err";
      if (state === "warn" || state === "partial") suffix = " warn";
      $("topDot").className = "dot" + suffix;
    }

    function setBusy(scope, busy, text, state = null) {
      const panel = scope === "rank"
        ? $("rankTab").children[1]
        : (scope === "quick" ? $("quickTab").children[1] : (scope === "email" ? $("emailTab").children[1] : $("fetchTab").children[1]));
      panel.classList.toggle("busy", busy);
      $(scope + "State").textContent = text;
      setTopState(text, busy ? null : (state ?? true));
    }

    function addNotification(scope, title, island, status = "", details = "", extras = {}) {
      const event = {
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        scope,
        title,
        island,
        status,
        progress: 0,
        details: String(details || "").slice(0, 60000),
        createdAt: Date.now(),
        queueStatus: "",
        openCards: false,
        canDelete: false,
        ...extras
      };
      state.notifications = [event, ...state.notifications].slice(0, 10);
      state.unreadNotifications = Math.min(10, state.unreadNotifications + 1);
      saveNotifications();
      renderNotifications();
      animateNotificationIsland(event);
      return event;
    }

    function updateNotification(notificationId, updates = {}) {
      const index = state.notifications.findIndex((item) => item.id === notificationId);
      if (index < 0) return null;
      state.notifications[index] = { ...state.notifications[index], ...updates };
      saveNotifications();
      renderNotifications();
      return state.notifications[index];
    }

    function removeNotification(notificationId) {
      const before = state.notifications.length;
      state.notifications = state.notifications.filter((item) => item.id !== notificationId);
      if (state.notifications.length !== before) {
        saveNotifications();
        renderNotifications();
      }
    }

    function animateNotificationIsland(event) {
      const wrap = $("notificationWrap");
      const island = $("notificationIsland");
      island.textContent = event.island;
      wrap.classList.remove("alerting");
      window.requestAnimationFrame(() => {
        wrap.classList.add("alerting");
      });
      window.clearTimeout(state.notificationTimer);
      state.notificationTimer = window.setTimeout(() => {
        wrap.classList.remove("alerting");
      }, 4200);
    }

    function appendNotificationAction(actions, text, className, onClick = null) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `notification-action ${className || ""}`.trim();
      button.textContent = text;
      if (onClick) {
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          onClick(event);
        });
      } else {
        button.tabIndex = -1;
        button.setAttribute("aria-disabled", "true");
        button.addEventListener("click", (event) => event.stopPropagation());
      }
      actions.appendChild(button);
      return button;
    }

    function notificationQueueChipText(item) {
      const position = Number(item.queue_position || 0);
      if (position > 1) return String(position);
      const match = String(item.status || item.island || "").match(/(\d+)[^\d]*по очереди/i);
      return match ? match[1] : "1";
    }

    function renderNotifications() {
      const list = $("notificationList");
      list.innerHTML = "";
      const count = $("notificationCount");
      count.textContent = String(Math.min(9, state.unreadNotifications));
      count.classList.toggle("visible", state.unreadNotifications > 0);
      if (!state.notifications.length) {
        const empty = document.createElement("div");
        empty.className = "notification-empty";
        empty.textContent = "Событий пока нет";
        list.appendChild(empty);
        return;
      }
      state.notifications.slice(0, 10).forEach((item) => {
        const row = document.createElement("div");
        row.className = "notification-item";
        row.dataset.scope = item.scope;
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.innerHTML = `
          <div class="notification-row">
            <div>
              <span class="notification-title"></span>
              <span class="notification-meta"></span>
            </div>
            <div class="notification-actions"></div>
          </div>
          <div class="notification-progress"><div class="notification-progress-fill"></div></div>
        `;
        row.querySelector(".notification-title").textContent = item.title;
        row.querySelector(".notification-meta").textContent = `${notificationScopeLabel(item.scope)} · ${formatNotificationTime(item.createdAt)}${item.status ? ` · ${item.status}` : ""}`;
        row.querySelector(".notification-progress-fill").style.width = `${Math.max(0, Math.min(100, Number(item.progress || 0)))}%`;
        const actions = row.querySelector(".notification-actions");
        if (item.queueStatus === "queued") {
          appendNotificationAction(actions, notificationQueueChipText(item), "warning queue-chip");
          if (item.canDelete || item.queueJobId) {
            appendNotificationAction(actions, "Удалить", "danger", () => cancelQueuedJob(item.queueJobId || item.id));
          }
        } else if (item.queueStatus === "running") {
          appendNotificationAction(actions, `${Math.round(Number(item.progress || 0))}%`, "neutral");
        } else if (item.queueStatus === "done") {
          appendNotificationAction(actions, "выполнен", "success");
        } else if (item.queueStatus === "error") {
          appendNotificationAction(actions, "лог", "danger", () => openNotificationDetails(item));
        } else if (item.details && String(item.status || "").toLowerCase().includes("ошиб")) {
          appendNotificationAction(actions, "лог", "danger", () => openNotificationDetails(item));
        }
        row.addEventListener("click", () => handleNotificationClick(item));
        row.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            handleNotificationClick(item);
          }
        });
        list.appendChild(row);
      });
    }

    async function handleNotificationClick(item) {
      closeNotifications();
      try {
        if (item.queueStatus === "error") {
          if (item.details) openNotificationDetails(item);
          else switchToNotificationScope(item.scope);
          return;
        }
        if (item.queueStatus === "queued" || item.queueStatus === "running") {
          switchToNotificationScope(item.scope);
          await restoreNotificationRequestState(item);
          if (item.progress !== undefined) setProgress(item.scope, Number(item.progress || 0), item.status || (item.queueStatus === "queued" ? "В очереди" : "Выполнение"));
          return;
        }
        if (item.openCards || item.queueStatus === "done") {
          await restoreCompletedNotificationState(item);
          return;
        }
        switchToNotificationScope(item.scope);
        await restoreNotificationRequestState(item);
      } catch (error) {
        switchToNotificationScope(item.scope);
        setTopState("Не удалось полностью восстановить событие", false);
      }
    }

    function switchToNotificationScope(scope) {
      if (scope === "quick" && $("uiModeToggle").checked) {
        $("uiModeToggle").checked = false;
        applyUiMode();
        return;
      }
      if (scope !== "quick" && !$("uiModeToggle").checked) {
        $("uiModeToggle").checked = true;
        applyUiMode();
      }
      switchTab(scope);
    }

    async function restoreCompletedNotificationState(item) {
      switchToNotificationScope(item.scope);
      if (item.scope === "quick") showQuickView("output");
      if (item.scope === "rank") showRankView("output");
      if (item.scope === "fetch") showFetchView("output");
      setTopState(item.status || item.island || "Готово", item.queueStatus === "error" ? false : true);
      await restoreNotificationRequestState(item);
      await hydrateNotificationCards(item);
      switchToNotificationScope(item.scope);
      if (item.scope === "quick") showQuickView("output");
      if (item.scope === "rank") showRankView("output");
      if (item.scope === "fetch") showFetchView("output");
      setTopState(item.status || item.island || "Готово", item.queueStatus === "error" ? false : true);
    }

    async function hydrateNotificationCards(item) {
      if (!["quick", "rank", "fetch"].includes(item.scope)) return false;
      let cards = Array.isArray(item.cards) ? item.cards : [];
      const storedCards = cards.length > 0;
      if (item.trace_path) {
        const result = await getJson(`/api/trace-cards?path=${encodeURIComponent(item.trace_path)}`);
        const traceCards = result.ok ? (result.cards || []) : [];
        if (traceCards.length) cards = traceCards;
      }
      if (!storedCards && !cards.length && item.created_path) {
        const result = await getJson(`/api/vacancy-file-cards?path=${encodeURIComponent(item.created_path)}`);
        cards = result.ok ? (result.cards || []) : [];
      }
      if (!cards.length) {
        updateCardsCta(item.scope);
        return false;
      }
      if (item.scope === "fetch") {
        state.fetchCards = storedCards || item.trace_path ? normalizeCards(cards) : normalizeRawCsvCards(cards);
      } else if (item.scope === "quick") {
        state.quickCards = normalizeCards(cards);
      } else {
        state.rankCards = normalizeCards(cards);
      }
      updateCardsCta(item.scope);
      return true;
    }

    async function openNotificationCards(item) {
      if (!["quick", "rank", "fetch"].includes(item.scope)) return false;
      await restoreNotificationRequestState(item);
      const hydrated = await hydrateNotificationCards(item);
      if (!hydrated) return false;
      switchToNotificationScope(item.scope);
      openCardsOverlay(item.scope);
      return true;
    }

    async function restoreNotificationRequestState(item) {
      const payload = item.request_payload || {};
      const snapshot = item.result_snapshot && typeof item.result_snapshot === "object" ? item.result_snapshot : {};
      if (item.scope === "quick") {
        $("quickText").value = payload.text || "";
        state.lastCreated = item.created_path || snapshot.created_path || state.lastCreated || "";
        state.quickViews.report = snapshot.report_preview || "";
        state.quickViews.methodology = snapshot.methodology_preview || "";
        state.quickViews.trace = snapshot.trace_preview || (snapshot.trace_summary ? JSON.stringify(snapshot.trace_summary, null, 2) : "");
        state.quickViews.run = snapshot.run_details || snapshot.run_preview || item.details || "";
        renderQuickReport("quickParamsReport", "quickParamsReportText", snapshot.parameters_report || "");
        renderQuickReport("quickSourcesReport", "quickSourcesReportText", snapshot.source_report || "");
        await loadFiles(state.lastCreated, item.criteria_path || snapshot.criteria_path || "", item.filter_path || snapshot.filter_path || "");
        updateCardsCta("quick");
        return;
      }
      if (item.scope === "rank") {
        await loadFiles(payload.vacancies || item.created_path || "", payload.criteria || item.criteria_path || "", "");
        if (payload.vacancies) $("rankVacanciesFile").value = payload.vacancies;
        if (payload.criteria) $("criteriaFile").value = payload.criteria;
        $("rankScoreModeToggle").checked = payload.llm_score !== false;
        $("rankExplanationModeToggle").checked = payload.llm_explanation !== false;
        if (payload.top_k) $("topK").value = payload.top_k;
        updateRankMode();
        updateFileMeta();
        state.lastReport = snapshot.report_path || "";
        state.lastMethodology = snapshot.methodology_path || "";
        state.lastTrace = item.trace_path || snapshot.trace_path || "";
        state.lastRun = snapshot.log_path || "";
        state.rankViews.report = snapshot.report_preview || "";
        state.rankViews.methodology = snapshot.methodology_preview || "";
        state.rankViews.trace = snapshot.trace_preview || (snapshot.trace_summary ? JSON.stringify(snapshot.trace_summary, null, 2) : "");
        state.rankViews.run = snapshot.run_details || item.details || "";
        updateCardsCta("rank");
        return;
      }
      if (item.scope === "fetch") {
        await loadFiles(item.created_path || "", "", payload.criteria || item.filter_path || "");
        state.lastCreated = item.created_path || snapshot.created_path || "";
        state.keywords = Array.isArray(payload.keywords) ? payload.keywords.slice() : [];
        renderKeywords();
        if (Array.isArray(payload.sources)) setSources(payload.sources);
        if (payload.source_priorities) {
          Object.entries(payload.source_priorities).forEach(([source, priority]) => setSourcePriority(source, priority));
        }
        if (payload.criteria) $("fetchCriteriaFile").value = payload.criteria;
        if (payload.max_vacancies) $("maxVacancies").value = payload.max_vacancies;
        $("useLlmHtml").checked = Boolean(payload.use_llm_html);
        $("hardFiltersToggle").checked = payload.hard_filters !== false;
        updateLlmHtmlMode();
        updateFetchCriteriaUiState();
        updateFileMeta();
        state.lastFetchRunResult = Object.keys(snapshot).length ? snapshot : state.lastFetchRunResult;
        state.lastFetchResult = Object.keys(snapshot).length ? snapshot : state.lastFetchResult;
        $("openFetchDetails").disabled = !(snapshot.trace_summary || snapshot.source_breakdown || item.trace_path);
        $("fetchOutput").textContent = Object.keys(snapshot).length ? JSON.stringify(snapshot, null, 2) : (item.details || $("fetchOutput").textContent);
        if (item.created_path) await loadFetchCards(item.created_path);
      }
    }

    function openNotificationDetails(item) {
      $("notificationDetailsTitle").textContent = item.title || "Подробный лог уведомления";
      $("notificationDetailsMeta").textContent = `${notificationScopeLabel(item.scope)} · ${formatNotificationTime(item.createdAt)}${item.status ? ` · ${item.status}` : ""}`;
      $("notificationDetailsText").textContent = String(item.details || "Подробный лог отсутствует.");
      $("notificationDetailsModal").classList.remove("hidden");
    }

    function closeNotificationDetails() {
      $("notificationDetailsModal").classList.add("hidden");
      $("notificationDetailsText").textContent = "";
      $("notificationDetailsMeta").textContent = "";
    }

    function notificationScopeLabel(scope) {
      if (scope === "quick") return "Быстрый поиск";
      if (scope === "email") return "Рассылки";
      return scope === "fetch" ? "Сбор вакансий" : "Ранжирование";
    }

    function queueScopeLabel(scope) {
      return notificationScopeLabel(scope);
    }

    function queuePositionText(position) {
      const number = Number(position || 0);
      if (!number || number <= 1) return "";
      return `${number}-я по очереди`;
    }

    function queuePositionForJob(job) {
      if (!job) return 0;
      if (state.activeQueueJob && state.activeQueueJob.id === job.id) return 1;
      const queuedIndex = state.queueJobs.findIndex((item) => item.id === job.id && !item.cancelled);
      if (queuedIndex < 0) return 0;
      return queuedIndex + 2;
    }

    function renderQueueIndicators() {
      ["quick", "fetch", "rank", "email"].forEach((scope) => {
        const node = $(scope + "QueuePosition");
        if (!node) return;
        const jobId = state.scopeQueueJobIds[scope];
        const job = state.activeQueueJob && state.activeQueueJob.id === jobId
          ? state.activeQueueJob
          : state.queueJobs.find((item) => item.id === jobId && !item.cancelled);
        const text = queuePositionText(queuePositionForJob(job));
        node.textContent = text;
        node.classList.toggle("hidden", !text);
      });
    }

    function setServerQueueIndicator(scope, position) {
      const node = $(scope + "QueuePosition");
      if (!node) return;
      const text = queuePositionText(position);
      node.textContent = text;
      node.classList.toggle("hidden", !text);
    }

    function queueJobDetails(job, position) {
      return [
        `Поставлено в очередь: ${queueScopeLabel(job.scope)}`,
        `Позиция: ${position || 1}`,
        `Endpoint: ${job.endpoint}`,
        `Payload: ${JSON.stringify(job.payload, null, 2)}`
      ].join("\n\n");
    }

    async function cancelQueuedJob(jobId) {
      if (jobId && !state.queueJobs.some((job) => job.id === jobId)) {
        const result = await postJson("/api/cancel-job", { job_id: jobId });
        if (result.ok) {
          const item = state.notifications.find((notification) => notification.queueJobId === jobId || notification.id === jobId);
          if (item) {
            updateNotification(item.id, {
              title: `${notificationScopeLabel(item.scope)} удален из очереди`,
              island: "удалено",
              status: "удалено",
              queueStatus: "cancelled",
              canDelete: false,
              openCards: false,
            });
          }
        }
        return result.ok;
      }
      const active = state.activeQueueJob && state.activeQueueJob.id === jobId;
      if (active) return false;
      const index = state.queueJobs.findIndex((job) => job.id === jobId);
      if (index < 0) return false;
      const [job] = state.queueJobs.splice(index, 1);
      if (state.scopeQueueJobIds[job.scope] === job.id) state.scopeQueueJobIds[job.scope] = "";
      if (job.notificationId) removeNotification(job.notificationId);
      job.cancelled = true;
      job.resolve?.({ ok: false, canceled: true, error: "Задача удалена из очереди." });
      renderQueueIndicators();
      processJobQueue();
      return true;
    }

    function normalizeQueueNotificationsAfterCompletion(job, result) {
      const cards = compactNotificationCards(
        result?.card_vacancies || result?.cards || result?.rank_result?.card_vacancies || []
      );
      const createdPath = result?.created_path || result?.fetch_result?.created_path || result?.rank_result?.created_path || "";
      const tracePath = result?.trace_path || result?.rank_result?.trace_path || result?.fetch_result?.trace_path || "";
      const hasCards = Boolean(result?.ok && (cards.length || createdPath || tracePath));
      updateNotification(job.notificationId, {
        title: result?.ok ? `${queueScopeLabel(job.scope)} завершен` : `${queueScopeLabel(job.scope)} завершился ошибкой`,
        island: result?.ok ? "готово" : "ошибка",
        status: result?.ok ? (result?.status_label || "готово") : (result?.error || "ошибка"),
        details: buildNotificationDetails(result, job.scope),
        queueStatus: result?.ok ? "done" : "error",
        queue_position: 0,
        openCards: hasCards,
        cards,
        created_path: createdPath,
        trace_path: tracePath,
        endpoint: result?._endpoint || "",
        request_payload: result?._request_payload || {},
        result_snapshot: notificationResultSnapshot(result, job.scope),
        canDelete: false
      });
    }

    function buildQueueStartNotification(job, position) {
      return addNotification(
        job.scope,
        `${queueScopeLabel(job.scope)} поставлен в очередь`,
        queuePositionText(position) || "в очереди",
        `ожидает ${queuePositionText(position) || ""}`.trim(),
        queueJobDetails(job, position),
        { queueStatus: "queued", queueJobId: job.id, queue_position: Number(position || 1), canDelete: true, openCards: false }
      );
    }

    function formatNotificationTime(value) {
      const date = new Date(Number(value || Date.now()));
      return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    }

    function buildNotificationDetails(result, scope) {
      const parts = [];
      if (result?.error) parts.push(`Ошибка: ${result.error}`);
      if (result?.status_label) parts.push(`Статус: ${result.status_label}`);
      if (result?.debug_log?.length) {
        parts.push("Debug log:");
        parts.push(Array.isArray(result.debug_log) ? result.debug_log.join("\n") : String(result.debug_log));
      }
      if (result?.error_details) {
        parts.push("Error details:");
        parts.push(String(result.error_details));
      }
      if (scope === "quick" && (result?.quick_plan || result?.fetch_result || result?.rank_result)) {
        parts.push("Quick search context:");
        parts.push(JSON.stringify({
          quick_plan: result.quick_plan,
          fetch_result: result.fetch_result,
          rank_result: result.rank_result,
          parameters_report: result.parameters_report,
          source_report: result.source_report
        }, null, 2));
      }
      if (scope === "fetch" && (result?.trace_summary || result?.stdout || result?.stderr)) {
        parts.push("Fetch context:");
        parts.push(JSON.stringify({
          trace_summary: result.trace_summary,
          stdout: result.stdout,
          stderr: result.stderr,
          created_path: result.created_path,
          trace_path: result.trace_path
        }, null, 2));
      }
      if (scope === "rank" && (result?.trace_summary || result?.stdout || result?.stderr)) {
        parts.push("Rank context:");
        parts.push(JSON.stringify({
          trace_summary: result.trace_summary,
          stdout: result.stdout,
          stderr: result.stderr,
          trace_path: result.trace_path,
          run_details: result.run_details
        }, null, 2));
      }
      if (scope === "email" && (result?.run_log || result?.delivery || result?.fetch_result || result?.rank_result)) {
        parts.push("Email delivery context:");
        parts.push(JSON.stringify({
          sent: result.sent || 0,
          message: result.message || "",
          delivery: result.delivery || {},
          fetch_result: compactResultSnapshot(result.fetch_result || {}),
          rank_result: compactResultSnapshot(result.rank_result || {}),
          run_log: result.run_log || []
        }, null, 2));
      }
      return parts.filter(Boolean).join("\n\n").slice(0, 60000);
    }

    function notificationResultSnapshot(result, scope) {
      if (!result || typeof result !== "object") return {};
      if (scope === "quick") {
        return {
          ok: Boolean(result.ok),
          status_label: result.status_label || "",
          created_path: result.created_path || result.fetch_result?.created_path || "",
          trace_path: result.trace_path || result.rank_result?.trace_path || "",
          criteria_path: result.criteria_path || "",
          filter_path: result.filter_path || "",
          quick_plan: result.quick_plan || {},
          parameters_report: result.parameters_report || "",
          source_report: result.source_report || "",
          report_preview: result.report_preview || "",
          methodology_preview: result.methodology_preview || "",
          trace_preview: result.trace_preview || "",
          trace_summary: result.trace_summary || {},
          run_details: result.run_details || "",
          fetch_result: compactResultSnapshot(result.fetch_result || {}),
          rank_result: compactResultSnapshot(result.rank_result || {})
        };
      }
      if (scope === "rank") {
        return {
          ok: Boolean(result.ok),
          status_label: result.status_label || "",
          report_path: result.report_path || "",
          methodology_path: result.methodology_path || "",
          trace_path: result.trace_path || "",
          log_path: result.log_path || "",
          report_preview: result.report_preview || "",
          methodology_preview: result.methodology_preview || "",
          trace_preview: result.trace_preview || "",
          trace_summary: result.trace_summary || {},
          run_details: result.run_details || ""
        };
      }
      if (scope === "fetch") {
        return {
          ok: Boolean(result.ok),
          status: result.status || "",
          status_label: result.status_label || "",
          relaxed_notice: result.relaxed_notice || result.trace_summary?.relaxed_notice || "",
          created_path: result.created_path || "",
          trace_path: result.trace_path || "",
          rows: result.rows || 0,
          trace_summary: result.trace_summary || {},
          source_breakdown: result.source_breakdown || {},
          stdout: String(result.stdout || "").slice(0, 3000),
          stderr: String(result.stderr || "").slice(0, 3000),
          error: result.error || ""
        };
      }
      return {};
    }

    function compactResultSnapshot(result) {
      if (!result || typeof result !== "object") return {};
      return {
        ok: Boolean(result.ok),
        status: result.status || "",
        status_label: result.status_label || "",
        created_path: result.created_path || "",
        trace_path: result.trace_path || "",
        rows: result.rows || 0,
        trace_summary: result.trace_summary || {},
        source_breakdown: result.source_breakdown || {},
        error: result.error || ""
      };
    }

    function completeJobNotification(scope, result, { okTitle, errorTitle, okIsland, errorIsland, status, details, cards = [] } = {}) {
      const payload = {
        title: result?.ok ? okTitle : errorTitle,
        island: result?.ok ? okIsland : errorIsland,
        status: String(status || (result?.ok ? "готово" : (result?.error || "ошибка"))).slice(0, 120),
        progress: 100,
        details: String(details || "").slice(0, 60000),
        queueStatus: result?.ok ? "done" : "error",
        queue_position: 0,
        canDelete: false,
        openCards: Boolean(result?.ok && (cards.length || result?.created_path || result?.trace_path)),
        cards: compactNotificationCards(cards),
        created_path: result?.created_path || result?.fetch_result?.created_path || "",
        trace_path: result?.trace_path || result?.rank_result?.trace_path || "",
        criteria_path: result?.criteria_path || "",
        filter_path: result?.filter_path || "",
        endpoint: result?._endpoint || "",
        request_payload: result?._request_payload || {},
        result_snapshot: notificationResultSnapshot(result, scope),
      };
      if (result?._notification_id) {
        const updated = updateNotification(result._notification_id, payload);
        if (updated) announceNotificationEvent(updated, Boolean(result?.ok));
      } else {
        const event = addNotification(scope, payload.title, payload.island, payload.status, payload.details, payload);
        playNotificationSound(Boolean(result?.ok));
      }
    }

    function announceNotificationEvent(event, ok) {
      state.unreadNotifications = Math.min(10, state.unreadNotifications + 1);
      renderNotifications();
      animateNotificationIsland(event);
      playNotificationSound(ok);
    }

    function compactNotificationCards(cards) {
      return (cards || []).slice(0, 30).map((card) => ({
        id: card.id || card.vacancy_id || card.url || card.link || card.title || "",
        title: card.title || card.normalized_title || card.role || "",
        company: card.company || card.employer || "",
        description: truncateText(card.description || card.llm_explanation_comment || card.llm_comment || card.summary || "", 900),
        risks: truncateText(card.risks || card.llm_risks || card.concerns || "", 420),
        score: card.score ?? card.match_score ?? "",
        source: card.source || card.parser || "",
        location: card.location || card.city || card.region || "",
        format: card.format || card.work_format || card.employment_type || "",
        salary: card.salary || card.salary_rub || card.compensation || card.pay || "",
        english: card.english || card.english_level || card.language || "",
        level: card.level || card.experience || card.experience_level || "",
        url: card.url || card.link || "",
        matchedSkills: card.matchedSkills || card.matched_skills || "",
        vacancySkills: card.vacancySkills || card.vacancy_skills || card.key_skills || card.stack || card.skills || "",
        extractedRequirements: card.extractedRequirements || card.extracted_requirements || "",
        scoreBreakdown: Array.isArray(card.scoreBreakdown) ? card.scoreBreakdown : (Array.isArray(card.score_breakdown) ? card.score_breakdown : []),
        stack: card.stack || card.vacancySkills || card.vacancy_skills || card.key_skills || card.skills || "",
        key_skills: card.key_skills || card.stack || card.vacancySkills || card.vacancy_skills || card.skills || "",
        vacancy_skills: card.vacancy_skills || card.vacancySkills || card.key_skills || card.stack || card.skills || "",
        score_breakdown: Array.isArray(card.score_breakdown) ? card.score_breakdown : (Array.isArray(card.scoreBreakdown) ? card.scoreBreakdown : []),
      }));
    }

    function toggleNotifications() {
      const panel = $("notificationPanel");
      const expanded = panel.classList.toggle("hidden") === false;
      $("notificationBell").setAttribute("aria-expanded", String(expanded));
      if (expanded) {
        state.unreadNotifications = 0;
        renderNotifications();
      }
    }

    function closeNotifications() {
      $("notificationPanel").classList.add("hidden");
      $("notificationBell").setAttribute("aria-expanded", "false");
    }

    function setProgress(scope, progress, stage) {
      const rawValue = Math.max(0, Math.min(100, Number(progress) || 0));
      const value = Math.max(Number(state.progress[scope] || 0), rawValue);
      state.progress[scope] = value;
      $(scope + "Progress").classList.remove("hidden");
      $(scope + "ProgressText").classList.remove("hidden");
      $(scope + "ProgressFill").style.width = `${value}%`;
      $(scope + "ProgressText").textContent = `${Math.round(value)}% · ${stage || "Выполнение"}`;
    }

    async function runProgressJobDirect(scope, endpoint, payload, onUpdate = null) {
      state.progress[scope] = 0;
      setProgress(scope, 0, "Запуск");
      const started = await postJson(endpoint, payload);
      if (!started.job_id) return started;
      const startPosition = Number(started.queue_position || 1);
      const notification = addNotification(
        scope,
        `${queueScopeLabel(scope)} поставлен в очередь`,
        queuePositionText(startPosition) || "запуск",
        queuePositionText(startPosition) || "1-я по очереди",
        JSON.stringify({ endpoint, payload }, null, 2),
        { queueStatus: "queued", queueJobId: started.job_id, queue_position: startPosition, canDelete: startPosition > 1, openCards: false, endpoint, request_payload: payload, progress: 0 }
      );
      while (true) {
        await sleep(350);
        const job = await getJson(`/api/job?id=${encodeURIComponent(started.job_id)}`);
        if (onUpdate) onUpdate(job);
        const progress = Number(job.progress ?? 0);
        const stage = job.stage || (job.status === "queued" ? "В очереди" : "Выполнение");
        if (job.status === "queued") {
          const positionText = queuePositionText(job.queue_position) || "1-я по очереди";
          setServerQueueIndicator(scope, job.queue_position);
          setProgress(scope, 0, positionText);
          updateNotification(notification.id, {
            title: `${queueScopeLabel(scope)} в очереди`,
            island: positionText,
            status: positionText,
            progress: 0,
            queueStatus: "queued",
            queue_position: Number(job.queue_position || 1),
            canDelete: true,
          });
          continue;
        }
        setServerQueueIndicator(scope, 1);
        setProgress(scope, progress, stage);
        updateNotification(notification.id, {
          title: `${queueScopeLabel(scope)} выполняется`,
          island: `${Math.round(progress)}%`,
          status: `${Math.round(progress)}% · ${stage}`,
          progress,
          queueStatus: "running",
          queue_position: 1,
          canDelete: false,
        });
        if (job.status === "done") {
          setServerQueueIndicator(scope, 0);
          return { ...(job.result || {}), _notification_id: notification.id, _job_id: started.job_id, _endpoint: endpoint, _request_payload: payload, _started: started };
        }
        if (job.status === "error") {
          setServerQueueIndicator(scope, 0);
          return {
            ok: false,
            error: job.error || "Job failed",
            error_details: job.error_details || "",
            debug_log: job.debug_log || "",
            _notification_id: notification.id,
            _job_id: started.job_id,
            _endpoint: endpoint,
            _request_payload: payload
          };
        }
        if (job.status === "cancelled") {
          setServerQueueIndicator(scope, 0);
          return { ok: false, canceled: true, error: job.error || "Задача удалена из очереди.", _notification_id: notification.id, _job_id: started.job_id };
        }
      }
    }

    function enqueueProgressJob(scope, endpoint, payload, onUpdate = null) {
      return new Promise((resolve) => {
        const job = {
          id: `${Date.now()}-${++state.queueJobSeq}`,
          scope,
          endpoint,
          payload,
          onUpdate,
          resolve,
          started: false,
          cancelled: false,
          notificationId: "",
        };
        state.queueJobs.push(job);
        state.scopeQueueJobIds[scope] = job.id;
        const position = state.activeQueueJob ? state.queueJobs.length + 1 : state.queueJobs.length;
        job.notificationId = buildQueueStartNotification(job, position).id;
        renderQueueIndicators();
        processJobQueue();
      });
    }

    function runProgressJob(scope, endpoint, payload, onUpdate = null) {
      return runProgressJobDirect(scope, endpoint, payload, onUpdate);
    }

    async function processJobQueue() {
      if (state.activeQueueJob) return;
      const job = state.queueJobs.find((item) => !item.cancelled && !item.started);
      if (!job) {
        renderQueueIndicators();
        return;
      }
      job.started = true;
      state.activeQueueJob = job;
      if (state.scopeQueueJobIds[job.scope] === job.id) renderQueueIndicators();
      updateNotification(job.notificationId, {
        title: `${queueScopeLabel(job.scope)} выполняется`,
        island: "в работе",
        status: "выполняется",
        queueStatus: "running",
        canDelete: false
      });
      let result;
      try {
        result = await runProgressJobDirect(job.scope, job.endpoint, job.payload, job.onUpdate);
      } catch (error) {
        result = { ok: false, error: String(error) };
      }
      job.resolve?.(result);
      normalizeQueueNotificationsAfterCompletion(job, result);
      state.queueJobs = state.queueJobs.filter((item) => item.id !== job.id);
      if (state.scopeQueueJobIds[job.scope] === job.id) state.scopeQueueJobIds[job.scope] = "";
      state.activeQueueJob = null;
      renderQueueIndicators();
      processJobQueue();
    }

    function sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function updateRankMode() {
      const llmScore = $("rankScoreModeToggle").checked;
      const llmExplanation = $("rankExplanationModeToggle").checked;
      $("rankMode").value = llmScore && llmExplanation ? "llm" : "dry_run";
      $("rankScoreModeAutoLabel").classList.toggle("active", !llmScore);
      $("rankScoreModeLlmLabel").classList.toggle("active", llmScore);
      $("rankExplanationModeAutoLabel").classList.toggle("active", !llmExplanation);
      $("rankExplanationModeLlmLabel").classList.toggle("active", llmExplanation);
    }

    function normalizeTopK() {
      const input = $("topK");
      const value = Math.max(Number(input.min || 1), Math.min(Number(input.max || 20), Number(input.value || 1)));
      input.value = String(value);
    }

    function stepTopK(delta) {
      const input = $("topK");
      input.value = String(Number(input.value || 1) + delta);
      normalizeTopK();
    }

    function resetQuickReports() {
      renderQuickReport("quickParamsReport", "quickParamsReportText", "");
      renderQuickReport("quickSourcesReport", "quickSourcesReportText", "");
      closeQuickReportPopover();
    }

    function updateQuickReportsFromJob(job) {
      renderQuickReport("quickParamsReport", "quickParamsReportText", job.parameters_report);
      renderQuickReport("quickSourcesReport", "quickSourcesReportText", job.source_report);
    }

    function renderQuickReport(sectionId, textId, text) {
      const value = String(text || "").trim();
      $(sectionId).classList.toggle("hidden", !value);
      $(sectionId).dataset.reportText = value;
      $(textId).textContent = value;
      if (!value && $("quickReportPopover")?.dataset.sourceId === sectionId) closeQuickReportPopover();
    }

    function openQuickReportPopover(sourceId) {
      const button = $(sourceId);
      const value = String(button?.dataset.reportText || "").trim();
      if (!value) return;
      $("quickReportPopover").dataset.sourceId = sourceId;
      $("quickReportPopoverTitle").textContent = button.dataset.title || "Отчет";
      $("quickReportPopoverText").textContent = value;
      $("quickReportPopover").classList.remove("hidden");
    }

    function closeQuickReportPopover() {
      const popover = $("quickReportPopover");
      if (!popover) return;
      popover.classList.add("hidden");
      popover.dataset.sourceId = "";
    }

    function normalizeEmailTopK() {
      const input = $("emailTopK");
      const value = Math.max(Number(input.min || 1), Math.min(Number(input.max || 20), Number(input.value || 1)));
      input.value = String(value);
    }

    function stepEmailTopK(delta) {
      $("emailTopK").value = String(Number($("emailTopK").value || 1) + delta);
      normalizeEmailTopK();
    }

    function addEmailFromInput({ silent = false } = {}) {
      const input = $("emailInput");
      const email = input.value.trim().toLowerCase();
      if (!email) return false;
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
        if (!silent) $("emailRecipientsError").classList.remove("hidden");
        return false;
      }
      if (!state.emailRecipients.includes(email)) state.emailRecipients.push(email);
      input.value = "";
      $("emailRecipientsError").classList.add("hidden");
      renderEmailRecipients();
      return true;
    }

    function renderEmailRecipients() {
      const list = $("emailList");
      list.innerHTML = "";
      state.emailRecipients.forEach((email, index) => {
        const chip = document.createElement("span");
        chip.className = "keyword-chip";
        chip.innerHTML = '<span></span><button class="keyword-remove" type="button" aria-label="Удалить почту">×</button>';
        chip.querySelector("span").textContent = email;
        chip.querySelector("button").addEventListener("click", () => {
          state.emailRecipients.splice(index, 1);
          renderEmailRecipients();
        });
        list.appendChild(chip);
      });
    }

    function addTelegramFromInput({ silent = false } = {}) {
      const input = $("telegramInput");
      let recipient = input.value.trim();
      if (!recipient) return false;
      if (recipient.startsWith("https://t.me/")) recipient = `@${recipient.replace(/\/$/, "").split("/").pop()}`;
      if (!/^-?\d{5,}$/.test(recipient) && !/^@?[A-Za-z0-9_]{5,32}$/.test(recipient)) {
        if (!silent) $("emailRecipientsError").classList.remove("hidden");
        return false;
      }
      if (!recipient.startsWith("@") && !/^-?\d+$/.test(recipient)) recipient = `@${recipient}`;
      if (!state.telegramRecipients.some((item) => item.toLowerCase() === recipient.toLowerCase())) {
        state.telegramRecipients.push(recipient);
      }
      input.value = "";
      $("emailRecipientsError").classList.add("hidden");
      renderTelegramRecipients();
      return true;
    }

    function renderTelegramRecipients() {
      const list = $("telegramList");
      list.innerHTML = "";
      state.telegramRecipients.forEach((recipient, index) => {
        const chip = document.createElement("span");
        chip.className = "keyword-chip";
        chip.innerHTML = '<span></span><button class="keyword-remove" type="button" aria-label="Удалить Telegram">×</button>';
        chip.querySelector("span").textContent = recipient;
        chip.querySelector("button").addEventListener("click", () => {
          state.telegramRecipients.splice(index, 1);
          renderTelegramRecipients();
        });
        list.appendChild(chip);
      });
    }

    async function loadTelegramBotInfo() {
      try {
        state.telegramBot = await getJson("/api/telegram-bot");
      } catch (error) {
        state.telegramBot = { ok: false, error: "Не удалось получить данные бота." };
      }
      const link = $("telegramBotLink");
      if (state.telegramBot.bot_url) {
        link.href = state.telegramBot.bot_url;
        link.textContent = `Открыть @${state.telegramBot.username || "бота"}`;
        link.classList.remove("hidden");
      } else {
        link.removeAttribute("href");
        link.textContent = state.telegramBot.error || "Telegram-бот пока недоступен";
        link.classList.remove("hidden");
      }
    }

    function toggleTelegramInfo(event) {
      event.stopPropagation();
      $("telegramInfoPopover").classList.toggle("hidden");
    }

    async function loadSubscriptions() {
      try {
        const data = await getJson("/api/email-subscriptions");
        state.subscriptions = data.subscriptions || [];
      } catch (error) {
        state.subscriptions = [];
      }
      renderSubscriptions();
    }

    function renderSubscriptions() {
      const list = $("subscriptionList");
      list.innerHTML = "";
      if (!state.subscriptions.length) {
        const empty = document.createElement("div");
        empty.className = "empty-card";
        empty.style.minHeight = "220px";
        empty.innerHTML = "<div><h2>Рассылок пока нет</h2><p>Создайте сводку, и первая отправка начнется в ближайший цикл планировщика.</p></div>";
        list.appendChild(empty);
        return;
      }
      state.subscriptions.forEach((item) => {
        const card = document.createElement("article");
        card.className = "subscription-item";
        const expanded = Boolean(state.expandedSubscriptions[item.id]);
        card.innerHTML = `
          <div class="subscription-head">
            <button class="subscription-title" type="button" aria-expanded="${expanded ? "true" : "false"}"></button>
            <button class="subscription-delete" type="button" aria-label="Удалить рассылку" title="Удалить рассылку">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <path d="M3 6h18"></path>
                <path d="M8 6V4h8v2"></path>
                <path d="M19 6l-1 14H6L5 6"></path>
                <path d="M10 11v5"></path>
                <path d="M14 11v5"></path>
              </svg>
            </button>
          </div>
          <div class="subscription-meta"></div>
          <div class="subscription-meta"></div>
          <div class="subscription-meta"></div>
          <div class="subscription-details hidden"></div>
        `;
        const titleButton = card.querySelector(".subscription-title");
        titleButton.textContent = expanded ? (item.text || "Поиск без названия") : truncateText(item.text || "Поиск без названия", 140);
        titleButton.addEventListener("click", () => {
          state.expandedSubscriptions[item.id] = !state.expandedSubscriptions[item.id];
          renderSubscriptions();
        });
        card.querySelector(".subscription-delete").addEventListener("click", () => deleteSubscription(item.id));
        const lines = card.querySelectorAll(".subscription-meta");
        const recipients = [...(item.emails || []), ...(item.telegram_recipients || [])].join(", ") || "получатели не заданы";
        lines[0].textContent = `${recipients} · ${item.k || 0} вакансий · раз в ${item.interval_value || "-"} ${item.interval_unit === "days" ? "дней" : "часов"}`;
        lines[1].textContent = `Статус: ${item.last_status || "ожидание"} · тема письма: ${item.email_theme === "dark" ? "темная" : "светлая"}`;
        lines[2].textContent = `Следующий запуск: ${formatDateTime(item.next_run_at)} · уже отправлено: ${item.sent_count || 0}`;
        const details = card.querySelector(".subscription-details");
        const logText = item.last_error_details || item.last_run_log ? `Лог последнего запуска:\n${item.last_error_details || item.last_run_log}` : "";
        details.textContent = logText;
        details.classList.toggle("hidden", !expanded || !logText);
        list.appendChild(card);
      });
    }

    function truncateText(value, limit) {
      const text = String(value || "").trim();
      if (text.length <= limit) return text;
      return `${text.slice(0, limit).trimEnd()}...`;
    }

    async function deleteSubscription(id) {
      if (!id) return;
      const result = await postJson("/api/delete-email-subscription", { id });
      if (result.ok) {
        delete state.expandedSubscriptions[id];
        state.subscriptions = result.subscriptions || state.subscriptions.filter((item) => item.id !== id);
        renderSubscriptions();
        addNotification("email", "Рассылка удалена", "рассылка удалена", "");
      } else {
        addNotification("email", "Не удалось удалить рассылку", "ошибка удаления", result.error || "ошибка");
      }
    }

    function formatDateTime(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    }

    $("vacanciesFile").addEventListener("change", () => {
      state.fetchFileTouched = true;
      updateFileMeta();
    });
    $("rankVacanciesFile").addEventListener("change", updateFileMeta);
    $("criteriaFile").addEventListener("change", updateFileMeta);
    $("fetchCriteriaFile").addEventListener("change", updateFileMeta);
    $("editVacancyMeta").addEventListener("click", () => openMetadataModal("vacancy"));
    $("editCriteriaMeta").addEventListener("click", () => openMetadataModal("criteria"));
    $("openCriteriaPrompt").addEventListener("click", openCriteriaPrompt);
    $("editFetchCriteriaMeta").addEventListener("click", () => openMetadataModal("filter", $("fetchCriteriaFile").value));
    $("openFetchCriteriaPrompt").addEventListener("click", openFetchCriteriaPrompt);
    $("closeCriteriaPrompt").addEventListener("click", closeCriteriaPrompt);
    $("closeFetchCriteriaPrompt").addEventListener("click", closeFetchCriteriaPrompt);
    $("generateCriteria").addEventListener("click", generateCriteriaFromPrompt);
    $("generateFetchCriteria").addEventListener("click", generateFetchCriteriaFromPrompt);
    $("criteriaPromptText").addEventListener("input", syncCriteriaPromptButton);
    $("criteriaPromptText").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        generateCriteriaFromPrompt();
      }
    });
    $("fetchCriteriaPromptText").addEventListener("input", syncFetchCriteriaPromptButton);
    $("fetchCriteriaPromptText").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        generateFetchCriteriaFromPrompt();
      }
    });
    $("uiModeToggle").addEventListener("change", applyUiMode);
    $("themeToggle").addEventListener("change", applyTheme);
    $("emailThemeToggle").addEventListener("change", updateEmailThemeToggle);
    $("rankScoreModeToggle").addEventListener("change", updateRankMode);
    $("rankExplanationModeToggle").addEventListener("change", updateRankMode);
    $("topKMinus").addEventListener("click", () => stepTopK(-1));
    $("topKPlus").addEventListener("click", () => stepTopK(1));
    $("topK").addEventListener("input", normalizeTopK);
    $("emailTopKMinus").addEventListener("click", () => stepEmailTopK(-1));
    $("emailTopKPlus").addEventListener("click", () => stepEmailTopK(1));
    $("emailTopK").addEventListener("input", normalizeEmailTopK);
    $("emailSendLater").addEventListener("change", updateSendTimeToggle);
    $("hardFiltersToggle").addEventListener("change", updateFetchCriteriaUiState);
    $("useLlmHtml").addEventListener("change", updateLlmHtmlMode);
    $("saveMetadata").addEventListener("click", saveMetadata);
    $("deleteMetadataFile").addEventListener("click", deleteMetadataFile);
    $("closeMetadataModal").addEventListener("click", closeMetadataModal);
    $("closeFavoriteModal").addEventListener("click", closeFavoriteModal);
    $("openFetchDetails").addEventListener("click", openFetchDetailsModal);
    $("closeFetchDetailsModal").addEventListener("click", closeFetchDetailsModal);
    $("notificationBell").addEventListener("click", (event) => {
      event.stopPropagation();
      toggleNotifications();
    });
    $("notificationIsland").addEventListener("click", (event) => {
      event.stopPropagation();
      toggleNotifications();
    });
    $("notificationSoundToggle").addEventListener("change", () => {
      state.notificationSound = $("notificationSoundToggle").checked;
      saveNotificationSound();
      if (state.notificationSound) playNotificationSound(true);
    });
    $("notificationPanel").addEventListener("click", (event) => {
      event.stopPropagation();
    });
    $("sourcesToggle").addEventListener("click", toggleSourcesMenu);
    $("allSourcesToggle").addEventListener("change", handleAllSourcesToggle);
    $("addKeyword").addEventListener("click", addKeywordFromInput);
    $("addEmail").addEventListener("click", () => addEmailFromInput());
    $("emailInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addEmailFromInput();
      }
    });
    $("addTelegram").addEventListener("click", () => addTelegramFromInput());
    $("telegramInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addTelegramFromInput();
      }
    });
    $("telegramInfoButton").addEventListener("click", toggleTelegramInfo);
    $("telegramInfoPopover").addEventListener("click", (event) => event.stopPropagation());
    $("queryText").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        addKeywordFromInput();
      }
    });
    $("metadataModal").addEventListener("click", (event) => {
      if (event.target === $("metadataModal")) closeMetadataModal();
    });
    $("criteriaPromptModal").addEventListener("click", (event) => {
      if (event.target === $("criteriaPromptModal")) closeCriteriaPrompt();
    });
    $("fetchCriteriaPromptModal").addEventListener("click", (event) => {
      if (event.target === $("fetchCriteriaPromptModal")) closeFetchCriteriaPrompt();
    });
    $("favoriteModal").addEventListener("click", (event) => {
      if (event.target === $("favoriteModal")) closeFavoriteModal();
    });
    $("notificationDetailsModal").addEventListener("click", (event) => {
      if (event.target === $("notificationDetailsModal")) closeNotificationDetails();
    });
    $("closeNotificationDetailsModal").addEventListener("click", closeNotificationDetails);
    $("fetchDetailsModal").addEventListener("click", (event) => {
      if (event.target === $("fetchDetailsModal")) closeFetchDetailsModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeCardsOverlay();
        closeMetadataModal();
        closeCriteriaPrompt();
        closeFavoriteModal();
        closeNotificationDetails();
        closeFetchDetailsModal();
        closeQuickReportPopover();
        closeSourcesMenu();
        closeNotifications();
        $("telegramInfoPopover").classList.add("hidden");
      }
      if (event.key === "ArrowLeft") handleCardHotkey(event, "left");
      if (event.key === "ArrowRight") handleCardHotkey(event, "right");
      if (event.key === "ArrowUp") handleCardHotkey(event, "up");
    });
    document.addEventListener("click", (event) => {
      if (!$("notificationWrap").contains(event.target)) closeNotifications();
      if (!$("telegramInfoPopover").contains(event.target) && event.target !== $("telegramInfoButton")) $("telegramInfoPopover").classList.add("hidden");
    });

    $("quickForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = $("quickText").value.trim();
      if (!text) {
        $("quickTextError").classList.remove("hidden");
        setTopState("Опишите поиск", false);
        return;
      }
      $("quickTextError").classList.add("hidden");
      setBusy("quick", true, "Быстрый поиск");
      resetQuickReports();
      const result = await runProgressJob("quick", "/api/quick-search", { text }, updateQuickReportsFromJob);
      if (result?.canceled) {
        setProgress("quick", 0, "Отменено");
        setBusy("quick", false, "Отменено", false);
        setTopState("Отменено", false);
        return;
      }
      setProgress("quick", 100, result.ok ? "Готово" : "Ошибка");
      setBusy("quick", false, result.ok ? "Готово" : "Ошибка", result.ok);
      state.lastCreated = result.created_path || result.fetch_result?.created_path || "";
      state.quickCards = normalizeCards(result.card_vacancies || []);
      state.cards = state.quickCards;
      state.cardIndex = 0;
      state.quickViews.report = result.report_preview || "";
      state.quickViews.methodology = result.methodology_preview || "";
      state.quickViews.trace = result.trace_preview || JSON.stringify({ plan: result.quick_plan, trace: result.trace_summary }, null, 2);
      state.quickViews.run = result.run_details || JSON.stringify(result.fetch_result || {}, null, 2);
      renderQuickReport("quickParamsReport", "quickParamsReportText", result.parameters_report);
      renderQuickReport("quickSourcesReport", "quickSourcesReportText", result.source_report);
      updateCardsCta("quick");
      if (result.ok && state.cards.length && activeTab() === "quick") openCardsOverlay("quick");
      else showQuickView("output");
      await loadFiles(state.lastCreated, result.criteria_path || "", result.filter_path || "");
      completeJobNotification("quick", result, {
        okTitle: "Быстрый поиск завершен",
        errorTitle: "Быстрый поиск завершился ошибкой",
        okIsland: "быстрый поиск готов",
        errorIsland: "ошибка быстрого поиска",
        status: result.ok ? `${state.cards.length || result.trace_summary?.trace_context?.ranked_count || 0} вакансий` : (result.error || "ошибка"),
        details: buildNotificationDetails(result, "quick"),
        cards: state.quickCards,
      });
    });

    $("emailForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      addEmailFromInput({ silent: true });
      addTelegramFromInput({ silent: true });
      const text = $("emailSearchText").value.trim();
      normalizeEmailTopK();
      if (!text) {
        $("emailTextError").classList.remove("hidden");
        setTopState("Опишите рассылку", false);
        return;
      }
      if (!state.emailRecipients.length && !state.telegramRecipients.length) {
        $("emailRecipientsError").classList.remove("hidden");
        setTopState("Добавьте получателя", false);
        return;
      }
      $("emailTextError").classList.add("hidden");
      $("emailRecipientsError").classList.add("hidden");
      setBusy("email", true, "Создание");
      setProgress("email", 15, "Сохранение настроек");
      const payload = {
        text,
        emails: state.emailRecipients,
        telegram_recipients: state.telegramRecipients,
        email_theme: $("emailThemeToggle").checked ? "dark" : "light",
        k: $("emailTopK").value,
        interval_value: $("emailIntervalValue").value,
        interval_unit: $("emailIntervalUnit").value,
        send_now: !$("emailSendLater").checked
      };
      const result = await runProgressJob("email", "/api/email-subscriptions", payload);
      const createdSubscription = result.subscription || result._started?.subscription || null;
      const created = Boolean(createdSubscription || result.subscription_id);
      const doneStage = created ? (result.ok ? "Готово" : "Создана, отправка с ошибкой") : "Ошибка";
      setProgress("email", 100, doneStage);
      setBusy("email", false, doneStage, created ? (result.ok ? true : "warn") : false);
      if (created) {
        await loadSubscriptions();
        if (result._notification_id) {
          const sent = Number(result.sent || 0);
          const statusText = result.ok
            ? (sent > 0 ? `отправлено вакансий: ${sent}` : (result.message || "новых вакансий нет"))
            : (result.error || "ошибка отправки");
          completeJobNotification("email", result, {
            okTitle: "Первая отправка рассылки завершена",
            errorTitle: "Первая отправка рассылки завершилась ошибкой",
            okIsland: "рассылка отправлена",
            errorIsland: "ошибка отправки",
            status: statusText,
            details: buildNotificationDetails(result, "email"),
          });
        } else {
          addNotification("email", "Рассылка создана", "рассылка создана", "ожидает плановой отправки");
        }
      } else {
        addNotification("email", "Рассылка не создана", "ошибка рассылки", result.error || "ошибка");
      }
    });

    $("rankForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      setBusy("rank", true, "Запуск");
      const payload = {
        vacancies: $("rankVacanciesFile").value,
        criteria: $("criteriaFile").value,
        mode: $("rankMode").value,
        llm_score: $("rankScoreModeToggle").checked,
        llm_explanation: $("rankExplanationModeToggle").checked,
        top_k: $("topK").value
      };
      const result = await runProgressJob("rank", "/api/rank", payload);
      if (result?.canceled) {
        setProgress("rank", 0, "Отменено");
        setBusy("rank", false, "Отменено", false);
        setTopState("Отменено", false);
        return;
      }
      setProgress("rank", 100, result.ok ? "Готово" : "Ошибка");
      setBusy("rank", false, result.ok ? "Готово" : "Ошибка", result.ok);
      state.lastReport = result.report_path || "";
      state.lastMethodology = result.methodology_path || "";
      state.lastTrace = result.trace_path || "";
      state.lastRun = result.log_path || "";
      state.rankCards = normalizeCards(result.card_vacancies || []);
      state.cards = state.rankCards;
      state.cardIndex = 0;
      state.rankViews.report = result.report_preview || "";
      state.rankViews.methodology = result.methodology_preview || "";
      state.rankViews.trace = result.trace_preview || JSON.stringify(result.trace_summary || {}, null, 2);
      state.rankViews.run = result.run_details || "";
      updateCardsCta("rank");
      if (result.ok && state.cards.length && activeTab() === "rank") openCardsOverlay("rank");
      else showRankView("output");
      setTopState(result.ok ? "Готов" : "Ошибка", result.ok);
      completeJobNotification("rank", result, {
        okTitle: "Ранжирование завершено",
        errorTitle: "Ранжирование завершилось ошибкой",
        okIsland: "ранжирование готово",
        errorIsland: "ошибка ранжирования",
        status: result.ok ? `${state.cards.length || result.trace_summary?.trace_context?.ranked_count || 0} вакансий` : (result.error || "ошибка"),
        details: buildNotificationDetails(result, "rank"),
        cards: state.rankCards,
      });
    });

    $("fetchForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      addKeywordFromInput({ silent: true });
      updateLlmHtmlMode();
      if (!state.keywords.length) {
        $("keywordsError").classList.remove("hidden");
        setTopState("Добавьте запрос", false);
        return;
      }
      const sources = selectedSources();
      if (!sources.length) {
        $("sourcesError").classList.remove("hidden");
        setTopState("Выберите источник", false);
        return;
      }
      $("sourcesError").classList.add("hidden");
      setBusy("fetch", true, "Сбор");
      state.lastFetchResult = null;
      $("openFetchDetails").disabled = true;
      $("fetchOutput").textContent = "Сбор вакансий выполняется...";
      const payload = {
        sources,
        source_priorities: selectedSourcePriorities(sources),
        keywords: state.keywords,
        max_vacancies: $("maxVacancies").value,
        use_llm_html: $("useLlmHtml").checked,
        criteria: $("fetchCriteriaFile").value,
        hard_filters: $("hardFiltersToggle").checked
      };
      const result = await runProgressJob("fetch", "/api/fetch", payload);
      if (result?.canceled) {
        setProgress("fetch", 0, "Отменено");
        setBusy("fetch", false, "Отменено", false);
        setTopState("Отменено", false);
        return;
      }
      const stateKind = result.status === "partial" ? "partial" : result.ok;
      const statusLabel = result.status_label || (result.ok ? "Готово" : "Ошибка");
      const relaxedNotice = result.relaxed_notice || result.trace_summary?.relaxed_notice || "";
      setProgress("fetch", 100, statusLabel);
      setBusy("fetch", false, statusLabel, stateKind);
      const fetchMetrics = result.trace_summary?.fetch_metrics || {};
      $("fetchRowsMetric").textContent = fetchMetrics.total_considered ?? result.trace_summary?.raw_rows ?? result.rows ?? "-";
      updateFetchSourceMetric(result);
      $("fetchMaxScoreMetric").textContent = maxScoreText(result.card_vacancies || []);
      state.lastCreated = result.created_path || "";
      state.lastFetchRunResult = result;
      if (state.lastCreated) state.fetchFileTouched = true;
      state.lastFetchResult = result;
      $("openFetchDetails").disabled = !result.trace_summary;
      $("fetchOutput").textContent = JSON.stringify(result, null, 2);
      await loadFiles(state.lastCreated);
      if (state.lastCreated) await loadFetchCards(state.lastCreated);
      setTopState(relaxedNotice || statusLabel, stateKind);
      completeJobNotification("fetch", result, {
        okTitle: result.ok && relaxedNotice ? "Сбор добран мягче" : "Сбор вакансий завершен",
        errorTitle: "Сбор вакансий завершился ошибкой",
        okIsland: result.ok && relaxedNotice ? "недобор по жестким фильтрам" : "поиск завершен",
        errorIsland: "ошибка сбора",
        status: relaxedNotice || statusLabel,
        details: buildNotificationDetails(result, "fetch"),
        cards: state.fetchCards,
      });
    });

    function transferSelectedFetchFileToRank() {
      const vacancies = $("vacanciesFile").value;
      if (!vacancies) {
        setTopState("Выберите CSV", false);
        return;
      }
      $("rankVacanciesFile").value = vacancies;
      updateFileMeta();
      switchTab("rank");
      $("rankVacanciesFile").focus();
      setTopState("CSV выбран для ранжирования", true);
    }

    function normalizeKeyword(value) {
      return String(value || "").trim().replace(/\s+/g, " ").slice(0, 120);
    }

    function updateLlmHtmlMode() {
      const isLlm = $("useLlmHtml").checked;
      const maxInput = $("maxVacancies");
      const maxValue = 50;
      const label = isLlm ? "автоматический + LLM парсинг" : "автоматический парсинг";
      $("llmHtmlModeLabel").textContent = label;
      $("llmHtmlModeHint").textContent = isLlm ? "Режим с дополнительной LLM-обработкой." : "Обычный автоматический парсинг.";
      $("llmHtmlModeWarning").classList.toggle("hidden", !isLlm);
      $("llmHtmlModeWarning").textContent = isLlm
        ? "Данная функция тоже находится в альфа-тестировании и работает нестабильно."
        : "";
      maxInput.max = String(maxValue);
      if (Number(maxInput.value || 0) > maxValue) maxInput.value = String(maxValue);
    }

    function openFetchDetailsModal() {
      renderFetchDetails();
      $("fetchDetailsModal").classList.remove("hidden");
    }

    function closeFetchDetailsModal() {
      $("fetchDetailsModal").classList.add("hidden");
    }

    function renderFetchDetails() {
      const result = state.lastFetchResult || {};
      const trace = result.trace_summary || {};
      const rawRows = Number(trace.raw_rows || result.rows || 0);
      const uniqueRows = Number(trace.unique_rows || result.rows || 0);
      const duplicates = Math.max(0, rawRows - uniqueRows);
      const metrics = trace.fetch_metrics || {};
      const breakdown = aggregateSourceMap(result.source_breakdown || {});
      const sourceStats = aggregateSourceStats(trace.source_stats || {});
      const htmlTotal = Number(metrics.html_without_llm || 0) + Number(metrics.html_with_llm || 0);
      const content = $("fetchDetailsContent");
      content.innerHTML = "";
      addDetailBlock("Итог", [
        `Всего рассмотрено вакансий: ${rawRows}`,
        `Итоговый список после дедупликации: ${uniqueRows}`,
        `Повторы: ${duplicates} (${percent(duplicates, rawRows)})`,
      ]);
      if (trace.relaxed_notice || trace.staged_fetch?.length) {
        const stageLines = (trace.staged_fetch || []).map((stage) => {
          if (stage.skipped) return `${stage.title}: пропущено, уже набрано ${stage.cumulative_rows || 0}`;
          return `${stage.title}: фильтров ${stage.filter_count || 0}, строк ${stage.rows || 0}, новых ${stage.added_rows || 0}, всего ${stage.cumulative_rows || 0}`;
        });
        addDetailBlock("Ослабление фильтров", [
          trace.relaxed_notice || "Использован staged-добор с менее строгой фильтрацией.",
          ...stageLines,
        ]);
      }
      addDetailBlock("Способ получения", [
        `auto parsing: ${metrics.html_without_llm || 0} (${percent(metrics.html_without_llm || 0, htmlTotal)})`,
        `LLM parsing: ${metrics.html_with_llm || 0} (${percent(metrics.html_with_llm || 0, htmlTotal)})`,
      ]);
      Object.keys({...sourceStats, ...breakdown}).sort().forEach((source) => {
        const stats = sourceStats[source] || {};
        const finalCount = Number(breakdown[source] || 0);
        addDetailBlock(source, [
          `Рассмотрено: ${stats.vacancies || 0}`,
          `В итоговом списке: ${finalCount} (${percent(finalCount, uniqueRows)})`,
          `Запросов: ${stats.requests || 0}, успешных: ${stats.successful_requests || 0}`,
          `Квота: ${stats.limit || 0}, приоритет: ${stats.priority || "-"}`,
        ]);
      });

      function aggregateSourceMap(mapping) {
        const aggregated = {};
        Object.entries(mapping || {}).forEach(([source, count]) => {
          const key = baseSourceName(source);
          aggregated[key] = (aggregated[key] || 0) + Number(count || 0);
        });
        return aggregated;
      }

      function aggregateSourceStats(mapping) {
        const aggregated = {};
        Object.entries(mapping || {}).forEach(([source, stats]) => {
          const key = baseSourceName(source);
          const current = aggregated[key] || { vacancies: 0, requests: 0, successful_requests: 0, limit: 0, priority: "-" };
          current.vacancies += Number(stats?.vacancies || 0);
          current.requests += Number(stats?.requests || 0);
          current.successful_requests += Number(stats?.successful_requests || 0);
          current.limit += Number(stats?.limit || 0);
          current.priority = stats?.priority || current.priority || "-";
          aggregated[key] = current;
        });
        return aggregated;
      }

      function baseSourceName(source) {
        return String(source || "unknown").replace(/-(?:llm-html|html|json|detail|api-detail|mixed-html)$/i, "") || "unknown";
      }

      function addDetailBlock(title, lines) {
        const block = document.createElement("section");
        block.className = "source-detail-item";
        const heading = document.createElement("h3");
        heading.textContent = title;
        const body = document.createElement("p");
        body.textContent = lines.join("\n");
        block.appendChild(heading);
        block.appendChild(body);
        content.appendChild(block);
      }
    }

    function percent(value, total) {
      const number = Number(value || 0);
      const denominator = Number(total || 0);
      if (!denominator) return "0%";
      return `${Math.round((number / denominator) * 100)}%`;
    }

    function addKeywordFromInput(options = {}) {
      const keyword = normalizeKeyword($("queryText").value);
      if (!keyword) return;
      const exists = state.keywords.some((item) => item.toLowerCase() === keyword.toLowerCase());
      if (!exists) state.keywords.push(keyword);
      $("queryText").value = "";
      $("keywordsError").classList.add("hidden");
      renderKeywords();
      if (!options.silent) $("queryText").focus();
    }

    function removeKeyword(index) {
      state.keywords.splice(index, 1);
      renderKeywords();
    }

    function renderKeywords() {
      $("keywordList").innerHTML = "";
      state.keywords.forEach((keyword, index) => {
        const chip = document.createElement("span");
        chip.className = "keyword-chip";
        chip.textContent = keyword;
        const remove = document.createElement("button");
        remove.className = "keyword-remove";
        remove.type = "button";
        remove.setAttribute("aria-label", `Удалить ${keyword}`);
        remove.textContent = "×";
        remove.addEventListener("click", () => removeKeyword(index));
        chip.appendChild(remove);
        $("keywordList").appendChild(chip);
      });
    }

    function selectedSources() {
      return Array.from(document.querySelectorAll('input[name="sources"]:checked'))
        .map((input) => input.value);
    }

    function sourceInputs() {
      return Array.from(document.querySelectorAll('input[name="sources"]'));
    }

    function priorityControls() {
      return Array.from(document.querySelectorAll(".source-priority"));
    }

    function priorityButtons() {
      return Array.from(document.querySelectorAll(".priority-step"));
    }

    function sourceLabels() {
      return sourceInputs().map((input) => ({
        value: input.value,
        label: input.closest("label")?.textContent.trim() || input.value
      }));
    }

    function setSources(values) {
      const selected = new Set(values);
      sourceInputs().forEach((input) => {
        input.checked = selected.has(input.value);
      });
      updateSourcesSummary();
      updateAllSourcesWarning();
    }

    function selectedSourcePriorities(sources) {
      const selected = new Set(sources);
      const priorities = {};
      priorityControls().forEach((control) => {
        const source = control.dataset.source;
        if (selected.has(source)) priorities[source] = control.dataset.priority || "medium";
      });
      return priorities;
    }

    function setSourcePriority(source, priority) {
      const control = document.querySelector(`.source-priority[data-source="${source}"]`);
      if (!control || $("allSourcesToggle").checked) return;
      control.dataset.priority = priority;
    }

    function updateSourcesSummary() {
      const labels = sourceLabels();
      const selected = selectedSources();
      if ($("allSourcesToggle").checked || selected.length === labels.length) {
        $("sourcesSummary").textContent = `Все источники (${labels.length})`;
        return;
      }
      if (!selected.length) {
        $("sourcesSummary").textContent = "Не выбраны";
        return;
      }
      const names = labels.filter((item) => selected.includes(item.value)).map((item) => item.label);
      $("sourcesSummary").textContent = names.length <= 2 ? names.join(", ") : `${names.slice(0, 2).join(", ")} +${names.length - 2}`;
    }

    function updateAllSourcesWarning() {
      const warning = $("allSourcesWarning");
      const list = $("unstableSourcesList");
      list.innerHTML = "";
      const unstableSelected = selectedSources()
        .filter((source) => !["hh", "superjob"].includes(source))
        .map((source) => unstableSourceMap[source] || sourceDisplayName(source))
        .filter(Boolean);
      unstableSelected.forEach((label) => {
        const item = document.createElement("li");
        item.textContent = label;
        list.appendChild(item);
      });
      warning.classList.toggle("hidden", unstableSelected.length === 0);
    }

    function toggleSourcesMenu() {
      if ($("allSourcesToggle").checked) return;
      const menu = $("sourcesMenu");
      const expanded = menu.classList.toggle("hidden") === false;
      if (expanded) positionSourcesMenu();
      $("sourcesToggle").setAttribute("aria-expanded", String(expanded));
    }

    function positionSourcesMenu() {
      const buttonRect = $("sourcesToggle").getBoundingClientRect();
      const margin = 12;
      const top = Math.min(buttonRect.bottom + 6, window.innerHeight - margin);
      const availableHeight = Math.max(180, window.innerHeight - top - margin);
      $("sourcesMenu").style.top = `${top}px`;
      $("sourcesMenu").style.left = `${buttonRect.left}px`;
      $("sourcesMenu").style.width = `${buttonRect.width}px`;
      $("sourcesMenu").style.maxHeight = `${availableHeight}px`;
    }

    function closeSourcesMenu() {
      $("sourcesMenu").classList.add("hidden");
      $("sourcesToggle").setAttribute("aria-expanded", "false");
    }

    function handleAllSourcesToggle() {
      const enabled = $("allSourcesToggle").checked;
      $("sourcesControl").classList.toggle("all-enabled", enabled);
      $("sourcesToggle").disabled = enabled;
      sourceInputs().forEach((input) => {
        input.disabled = enabled;
      });
      priorityButtons().forEach((button) => {
        button.disabled = enabled;
      });
      if (enabled) {
        state.manualSources = selectedSources();
        setSources(sourceInputs().map((input) => input.value));
        $("sourcesError").classList.add("hidden");
        closeSourcesMenu();
      } else {
        setSources(state.manualSources);
      }
      updateSourcesSummary();
      updateAllSourcesWarning();
    }

    document.querySelectorAll('input[name="sources"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (!$("allSourcesToggle").checked) state.manualSources = selectedSources();
        if (selectedSources().length) $("sourcesError").classList.add("hidden");
        updateSourcesSummary();
        updateAllSourcesWarning();
      });
    });

    priorityButtons().forEach((button) => {
      button.addEventListener("click", () => {
        const control = button.closest(".source-priority");
        if (!control) return;
        setSourcePriority(control.dataset.source, button.dataset.priority);
      });
    });

    document.addEventListener("click", (event) => {
      if (!$("sourcesControl").contains(event.target)) closeSourcesMenu();
    });
    window.addEventListener("resize", () => {
      if (!$("sourcesMenu").classList.contains("hidden")) positionSourcesMenu();
    });
    window.addEventListener("scroll", () => {
      if (!$("sourcesMenu").classList.contains("hidden")) positionSourcesMenu();
    }, true);
    state.manualSources = selectedSources();
    restoreTheme();
    $("queryText").placeholder = searchPlaceholders[0];
    $("quickText").placeholder = candidateWishPlaceholders[0];
    $("emailSearchText").placeholder = emailWishPlaceholders[0];
    window.setInterval(rotateSearchPlaceholder, 5000);
    window.setInterval(rotateCandidateWishPlaceholders, 5000);
    renderKeywords();
    renderEmailRecipients();
    renderTelegramRecipients();
    $("notificationSoundToggle").checked = state.notificationSound;
    renderQueueIndicators();
    applyUiMode();
    updateRankMode();
    normalizeTopK();
    normalizeEmailTopK();
    updateSendTimeToggle();
    updateEmailThemeToggle();
    updateCardsCta("rank");
    updateCardsCta("quick");
    updateCardsCta("fetch");
    loadSubscriptions();
    loadTelegramBotInfo();
    updateLlmHtmlMode();
    updateAllSourcesWarning();
    if ($("allSourcesToggle").checked) {
      handleAllSourcesToggle();
    } else {
      updateSourcesSummary();
    }

    $("rankCreated").addEventListener("click", transferSelectedFetchFileToRank);

    $("quickOpenCards").addEventListener("click", () => openCardsOverlay("quick"));
    $("openCards").addEventListener("click", () => openCardsOverlay("rank"));
    $("fetchOpenCards").addEventListener("click", () => openCardsOverlay("fetch"));
    $("quickParamsReport").addEventListener("click", () => openQuickReportPopover("quickParamsReport"));
    $("quickSourcesReport").addEventListener("click", () => openQuickReportPopover("quickSourcesReport"));
    $("closeQuickReportPopover").addEventListener("click", closeQuickReportPopover);
    document.addEventListener("click", (event) => {
      document.querySelectorAll(".vacancy-card .match-details:not(.hidden)").forEach((details) => {
        const wrap = details.closest(".match-wrap");
        const card = details.closest(".vacancy-card");
        if (wrap && card && !wrap.contains(event.target)) closeMatchDetails(card);
      });
      if (!$("quickReportPopover").classList.contains("hidden") && !$("quickReportPopover").contains(event.target) && !event.target.closest(".quick-report-pill")) {
        closeQuickReportPopover();
      }
    });

    function showRankView(view) {
      state.rankView = view;
      state.cardStageId = "cardStage";
      $("cardStage").classList.toggle("hidden", view !== "cards");
      $("cardStage").classList.toggle("card-overlay", view === "cards");
      setCardOverlayScrollLock(view === "cards");
      if (view === "cards") {
        renderCurrentCard();
        return;
      }
    }

    function showQuickView(view) {
      state.quickView = view;
      state.cardStageId = "quickCardStage";
      $("quickCardStage").classList.toggle("hidden", view !== "cards");
      $("quickCardStage").classList.toggle("card-overlay", view === "cards");
      setCardOverlayScrollLock(view === "cards");
      if (view === "cards") {
        renderCurrentCard();
        return;
      }
    }

    function showFetchView(view) {
      state.fetchView = view;
      state.cardStageId = "fetchCardStage";
      $("fetchCardStage").classList.toggle("hidden", view !== "cards");
      $("fetchCardStage").classList.toggle("card-overlay", view === "cards");
      setCardOverlayScrollLock(view === "cards");
      if (view === "cards") {
        renderCurrentCard();
        return;
      }
    }

    function openCardsOverlay(scope) {
      if (scope === "quick") {
        if ($("uiModeToggle").checked) {
          $("uiModeToggle").checked = false;
          applyUiMode();
        }
        state.cards = state.quickCards;
        state.cardMode = "quick";
        if (!state.cards.length) return;
        state.cardIndex = 0;
        switchTab("quick");
        showQuickView("cards");
      } else if (scope === "fetch") {
        state.cards = state.fetchCards;
        state.cardMode = "fetch";
        if (!state.cards.length) return;
        state.cardIndex = 0;
        switchTab("fetch");
        showFetchView("cards");
      } else {
        state.cards = state.rankCards;
        state.cardMode = "rank";
        if (!state.cards.length) return;
        state.cardIndex = 0;
        switchTab("rank");
        showRankView("cards");
      }
    }

    function closeCardsOverlay() {
      if (state.cardStageId === "quickCardStage" && state.quickView === "cards") showQuickView("output");
      if (state.cardStageId === "cardStage" && state.rankView === "cards") showRankView("output");
      if (state.cardStageId === "fetchCardStage" && state.fetchView === "cards") showFetchView("output");
      if (state.cardStageId === "favoriteCardStage") {
        $("favoriteCardStage").classList.add("hidden");
        $("favoriteCardStage").classList.remove("card-overlay");
      }
      setCardOverlayScrollLock(false);
    }

    function setCardOverlayScrollLock(enabled) {
      document.body.classList.toggle("card-overlay-open", enabled);
      document.documentElement.classList.toggle("card-overlay-open", enabled);
    }

    function updateCardsCta(scope) {
      const button = scope === "quick" ? $("quickOpenCards") : (scope === "fetch" ? $("fetchOpenCards") : $("openCards"));
      const count = scope === "quick" ? state.quickCards.length : (scope === "fetch" ? state.fetchCards.length : state.rankCards.length);
      button.disabled = count <= 0;
      button.querySelector("strong").textContent = count > 0 ? "Открыть карточки" : "Карточки недоступны";
      button.querySelector("span").textContent = count > 0
        ? `Можно кликнуть, чтобы посмотреть карточки вакансий: ${count}.`
        : (scope === "quick"
          ? "Запустите быстрый поиск, затем здесь можно будет открыть карточки вакансий."
          : (scope === "fetch"
            ? "Выберите итоговый файл поиска или завершите сбор, затем здесь можно будет открыть карточки вакансий."
            : "Запустите ранжирование, затем здесь можно будет открыть карточки вакансий."));
    }

    function normalizeCards(cards) {
      return cards.map((card, index) => ({
        id: firstText(card, ["vacancy_id", "id", "url", "link", "title"], `card-${index}`),
        title: firstText(card, ["title", "normalized_title", "role"], "Без названия"),
        company: firstText(card, ["company", "employer"], "Компания не указана"),
        description: buildLlmDescription(card),
        risks: buildRisks(card),
        score: card.score ?? card.match_score ?? 0,
        source: firstText(card, ["source", "parser"], ""),
        location: firstText(card, ["location", "city", "region"], ""),
        format: firstText(card, ["work_format", "format", "employment_type"], ""),
        salary: firstText(card, ["salary", "salary_rub", "compensation", "pay"], ""),
        english: firstText(card, ["english", "english_level", "language"], ""),
        level: firstText(card, ["level", "experience", "experience_level"], ""),
        url: firstText(card, ["url", "link"], ""),
        matchedSkills: listText(firstNonEmpty(card.matchedSkills, card.matched_skills)),
        missingTargetSkills: listText(firstNonEmpty(card.missingTargetSkills, card.missing_target_skills)),
        vacancySkills: listText(firstNonEmpty(extractTechSkills(card), card.vacancySkills, card.vacancy_skills, card.key_skills, card.stack, card.skills)),
        reasons: listText(card.reasons),
        concerns: listText(card.concerns),
        extractedRequirements: listText(firstNonEmpty(card.extractedRequirements, card.extracted_requirements)),
        scoreBreakdown: Array.isArray(card.scoreBreakdown) ? card.scoreBreakdown : (Array.isArray(card.score_breakdown) ? card.score_breakdown : []),
      }));
    }

    function normalizeRawCsvCards(cards) {
      return cards.map((card, index) => {
        const title = firstText(card, ["title", "name", "vacancy_name", "position", "role", "normalized_title"], `Вакансия ${index + 1}`);
        const company = firstText(card, ["company", "employer", "organization", "company_name"], "Компания не указана");
        const salary = buildRawSalary(card);
        const description = buildRawDescription(card);
        const usedKeys = new Set([
          "title", "name", "vacancy_name", "position", "role", "normalized_title",
          "company", "employer", "organization", "company_name",
          "description", "summary", "text", "requirements", "responsibilities", "conditions",
          "url", "link"
        ]);
        return {
          id: firstText(card, ["vacancy_id", "id", "url", "link", "title", "_csv_row"], `csv-card-${index}`),
          title,
          company,
          description,
          risks: "",
          score: firstText(card, ["score", "match_score"], ""),
          source: firstText(card, ["source", "parser"], ""),
          location: firstText(card, ["location", "city", "region", "address"], ""),
          format: firstText(card, ["work_format", "format", "employment_type", "employment", "schedule"], ""),
          salary,
          english: firstText(card, ["english", "english_level", "language"], ""),
          level: firstText(card, ["level", "experience", "experience_level"], ""),
          url: firstText(card, ["url", "link"], ""),
          matchedSkills: "",
          missingTargetSkills: "",
          vacancySkills: listText(firstNonEmpty(extractTechSkills(card), card.vacancy_skills, card.key_skills, card.stack, card.skills)),
          reasons: "",
          concerns: "",
          extractedRequirements: listText(firstNonEmpty(card.requirements, card.responsibilities, card.conditions, card.extracted_requirements)),
          scoreBreakdown: [],
          rawCard: true,
          rawFields: buildRawCardFields(card, usedKeys),
        };
      });
    }

    function buildRawSalary(card) {
      const direct = firstText(card, ["salary", "salary_rub", "compensation", "pay"], "");
      if (direct) return direct;
      const from = firstText(card, ["salary_from", "salary_min", "min_salary"], "");
      const to = firstText(card, ["salary_to", "salary_max", "max_salary"], "");
      const currency = firstText(card, ["salary_currency", "currency"], "RUB");
      if (from && to) return `${from}-${to} ${currency}`;
      if (from) return `от ${from} ${currency}`;
      if (to) return `до ${to} ${currency}`;
      return "";
    }

    function buildRawDescription(card) {
      const parts = [
        firstText(card, ["description", "summary", "text"], ""),
        firstText(card, ["requirements", "responsibilities", "conditions"], ""),
      ].filter(Boolean);
      return trimText(parts.join(" "), 620) || "В CSV нет подробного описания; карточка собрана из доступных полей файла.";
    }

    function buildRawCardFields(card, usedKeys) {
      const fields = [];
      Object.entries(card || {}).forEach(([key, value]) => {
        const normalizedKey = String(key || "").trim();
        const normalizedLookup = normalizedKey.toLowerCase();
        if (!normalizedKey || usedKeys.has(normalizedLookup)) return;
        const text = trimText(Array.isArray(value) ? value.join(", ") : value, 140);
        if (!text) return;
        fields.push({ label: rawFieldLabel(normalizedKey), value: text });
      });
      return fields.slice(0, 18);
    }

    function rawFieldLabel(key) {
      const normalized = String(key || "").toLowerCase();
      const labels = {
        _csv_row: "Строка CSV",
        vacancy_id: "ID",
        id: "ID",
        source: "Источник",
        parser: "Парсер",
        city: "Город",
        region: "Регион",
        location: "Локация",
        address: "Адрес",
        salary_from: "ЗП от",
        salary_to: "ЗП до",
        salary_currency: "Валюта",
        currency: "Валюта",
        work_format: "Формат",
        employment_type: "Занятость",
        employment: "Занятость",
        schedule: "График",
        experience: "Опыт",
        published_at: "Опубликовано",
        created_at: "Создано",
        updated_at: "Обновлено",
        key_skills: "Навыки",
        skills: "Навыки",
        stack: "Стек",
      };
      return labels[normalized] || String(key || "").replace(/_/g, " ");
    }

    function firstText(source, keys, fallback = "") {
      for (const key of keys) {
        const value = source?.[key];
        if (Array.isArray(value) && value.length) return cleanCardText(value.join(", "));
        if (value !== undefined && value !== null && String(value).trim()) return cleanCardText(String(value).trim());
      }
      return fallback;
    }

    function listText(value) {
      if (Array.isArray(value)) return value.filter(Boolean).join(", ");
      return String(value || "").trim();
    }

    function firstNonEmpty(...values) {
      for (const value of values) {
        if (Array.isArray(value) && value.some(Boolean)) return value;
        if (!Array.isArray(value) && String(value || "").trim()) return value;
      }
      return "";
    }

    function splitListText(value) {
      if (Array.isArray(value)) return value.map(cleanFactText).filter(Boolean);
      return String(value || "")
        .split(/[;,|]/)
        .map(cleanFactText)
        .filter(Boolean);
    }

    function cleanCardText(value) {
      let text = String(value || "");
      text = text.replace(/<[^>]+>/g, " ");
      text = text.replace(/\b[a-z]*onse\?\s*vacancyId=\d+[^\s"'<]*/gi, " ");
      text = text.replace(/\b(?:fill-rule|clip-rule|fill-opacity|fill|class|style|data-[\w-]+|aria-[\w-]+|xlink:href|title|type)=["'][^"']*["']/gi, " ");
      text = text.replace(/\b(?:f-test-[\w-]+|undefined)\b/gi, " ");
      text = text.replace(/^\s*(?:[A-Za-z0-9_-]{2,}\s+){2,}[A-Za-z0-9_-]{2,}["']?>\s*/, " ");
      text = text.replace(/^\s*(?:span|pan|div|svg|path|button|class)["']?>\s*/i, " ");
      text = text.replace(/^\s*[A-Za-z]?\d+(?:[.\s,-]*[A-Za-z]?\d+){5,}[A-Za-z]*["']?\s*>?\s*/, " ");
      text = text.replace(/\b[A-Za-z]?\d+(?:\.\d+)?(?:[.\s,-]+[A-Za-z]?\d+(?:\.\d+)?){7,}[A-Za-z]*\b/g, " ");
      text = text.replace(/\b(?:Apply|Откликнуться|Чат|Добавить в избранное)\b/gi, " ");
      text = text.replace(/\+7\s*\d{3}\s*\d{3}[•\d]*/g, " ");
      text = text.replace(/\b(?:Сегодня|Вчера|\d{1,2}\s+[А-Яа-я]+)\s*(?:в\s*\d{1,2}:\d{2})?/gi, " ");
      text = text.replace(/\b(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b/gi, " ");
      text = text.replace(/\b\d+\s+зарплат[аы]?\b/gi, " ");
      text = text.replace(/\b\d+\s+отзыв(?:ов|а)?\b/gi, " ");
      text = text.replace(/\bПерейти в каталог компаний\b/gi, " ");
      return text.replace(/\s+/g, " ").replace(/^[\s"'>]+|[\s"'>]+$/g, "").trim();
    }

    function cleanFactText(value) {
      const text = cleanCardText(value);
      if (!text || text.length > 90) return "";
      if (/^[₽$€£]+$/.test(text)) return "";
      if (/[<>]|magritte|data-qa|f-test-|undefined|fill-|vacancyId/i.test(text)) return "";
      return text;
    }

    function sentenceLimit(text, limit = 4) {
      const cleaned = String(text || "").replace(/\s+/g, " ").trim();
      if (!cleaned) return "";
      const sentences = cleaned.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [cleaned];
      return sentences.slice(0, limit).join(" ").slice(0, 760).trim();
    }

    function removeRiskSentences(text) {
      const cleaned = String(text || "").replace(/\s+/g, " ").trim();
      if (!cleaned) return "";
      const riskPatterns = [
        /стоп-слов/i,
        /стоит уточнить/i,
        /нужно уточнить/i,
        /важно уточнить/i,
        /не указан/i,
        /не указана/i,
        /без указания/i,
        /неизвест/i,
        /риск/i,
        /минус/i,
        /может означать/i,
        /не раскрыт/i,
        /не раскрыта/i,
        /не соответствует/i,
        /снижает/i,
      ];
      const sentences = cleaned.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [cleaned];
      return sentences.filter((sentence) => !riskPatterns.some((pattern) => pattern.test(sentence))).join(" ").trim();
    }

    function trimText(value, limit = 160) {
      const text = cleanCardText(value);
      if (!text) return "";
      if (text.length <= limit) return text;
      const cut = text.slice(0, limit).replace(/\s+\S*$/, "").trim();
      return `${(cut || text.slice(0, limit).trim()).replace(/[\s,;:-]+$/, "")}…`;
    }

    function removeCardFacts(text, facts) {
      let result = cleanCardText(text);
      facts.filter(Boolean).forEach((fact) => {
        const escaped = String(fact).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        if (escaped.length >= 3) result = result.replace(new RegExp(escaped, "gi"), " ");
      });
      result = result.replace(/\b(?:salary|зарплата|город|формат|remote|hybrid|onsite|офис|гибрид|удален[а-я]*|english|английский|риски?|минусы?)\s*[:\-–][^.!?]*(?:[.!?]|$)/gi, " ");
      result = result.replace(/^\s*(?:вакансия|роль|позиция)\s+[^.!?]{0,120}?(?:подходит|предполагает|требует|ориентирована|в\s+)/i, "");
      result = result.replace(/\b(?:критичных рисков не выявлено|проверьте требования и условия перед откликом)\b/gi, " ");
      return result.replace(/\s+/g, " ").trim();
    }

    function displayValue(value, fallback = "не указано") {
      const text = cleanFactText(value);
      return text || fallback;
    }

    function displayWorkFormat(value) {
      const text = String(value || "").toLowerCase().replace("ё", "е");
      if (/remote|удален/.test(text)) return "Удаленка";
      if (/hybrid|гибрид/.test(text)) return "Гибрид";
      if (/onsite|office|офис|полный день/.test(text)) return "Офис";
      return displayValue(value);
    }

    function buildLlmDescription(card) {
      const llmText = firstText(card, ["llm_explanation_comment", "llm_comment", "llm_rank_comment", "why_fit"], "");
      const rawText = [
        firstText(card, ["raw_detail_text"], ""),
        firstText(card, ["description", "summary", "text"], ""),
        firstText(card, ["requirements"], ""),
        firstText(card, ["responsibilities"], ""),
        firstText(card, ["conditions"], ""),
      ].filter(Boolean).join("\n");
      const source = [
        llmText && !/недоступен/i.test(llmText) ? llmText : "",
        rawText,
      ].filter(Boolean).join("\n");
      const facts = [
        firstText(card, ["title", "normalized_title", "role"], ""),
        firstText(card, ["company", "employer"], ""),
        firstText(card, ["work_format", "format", "employment_type"], ""),
        firstText(card, ["location", "city", "region"], ""),
        firstText(card, ["salary", "salary_rub", "compensation", "pay"], ""),
        firstText(card, ["english", "english_level", "language"], ""),
        firstText(card, ["level", "experience", "experience_level"], "")
      ].map(cleanFactText).filter(Boolean);
      const riskText = listText(card.llm_risks || card.llm_score_risks || card.concerns || card.risks || []);
      const cleaned = removeRiskSentences(removeCardFacts(source, facts.concat(riskText ? [riskText] : [])));
      const sections = extractDescriptionSections(source);
      const fragments = [];
      const sectionFacts = facts.concat(["Описание", "Требования", "Обязанности", "Условия", "Что важно", "Что предстоит делать", "Что мы предлагаем"]);

      const intro = summarizeSectionText(sections.intro || cleaned, sectionFacts, 220);
      const important = summarizeSectionText(sections.important, sectionFacts, 220);
      const duties = summarizeSectionText(sections.duties, sectionFacts, 240);
      const offer = summarizeSectionText(sections.offer, sectionFacts, 220);
      const extra = summarizeSectionText(sections.extra || cleaned, sectionFacts, 180);

      if (intro) fragments.push(intro);
      if (important) fragments.push(`Что важно: ${important}`);
      if (duties) fragments.push(`Что предстоит делать: ${duties}`);
      if (offer) fragments.push(`Что мы предлагаем: ${offer}`);
      if (!fragments.length && extra) fragments.push(extra);

      const extractedRequirements = trimText(card.extractedRequirements || card.extracted_requirements || [], 180);
      const reasons = trimText(card.reasons || [], 180);
      if (!fragments.length && extractedRequirements) {
        fragments.push(`Из описания можно извлечь требования: ${extractedRequirements}.`);
      }
      if (!fragments.length && reasons) {
        fragments.push(`По скорингу вакансия выглядит релевантной по признакам: ${reasons}.`);
      }
      if (!fragments.length) {
        fragments.push("Описание роли в источнике слишком короткое или фрагментарное; в карточке показаны только доступные данные вакансии.");
      }

      const expanded = ensureMinimumSentences(fragments.join(" "), cleaned, sectionFacts, 5, 10);
      return expanded || "Описание роли не найдено в данных вакансии.";
    }

    function extractDescriptionSections(text) {
      const result = {
        intro: "",
        important: "",
        duties: "",
        offer: "",
        extra: "",
      };
      const paragraphs = String(text || "")
        .replace(/\r/g, "\n")
        .split(/\n+/)
        .map((part) => cleanCardText(part))
        .filter(Boolean);
      let current = "intro";
      const headingMatchers = [
        [/^что важно\b/i, "important"],
        [/^требования\b/i, "important"],
        [/^что предстоит делать\b/i, "duties"],
        [/^обязанности\b/i, "duties"],
        [/^что мы предлагаем\b/i, "offer"],
        [/^что предлагаем\b/i, "offer"],
        [/^условия\b/i, "offer"],
        [/^о компании\b/i, "extra"],
        [/^о проекте\b/i, "extra"],
        [/^проект\b/i, "extra"],
        [/^мы ищем\b/i, "intro"],
        [/^кого мы ищем\b/i, "intro"],
      ];
      paragraphs.forEach((paragraph) => {
        let matchedSection = "";
        let remainder = paragraph;
        headingMatchers.some(([pattern, section]) => {
          const match = paragraph.match(pattern);
          if (!match) return false;
          matchedSection = section;
          remainder = cleanCardText(paragraph.slice(match[0].length).replace(/^[:\-–—\s]+/, ""));
          return true;
        });
        if (matchedSection) {
          current = matchedSection;
          if (remainder) {
            result[current] = result[current] ? `${result[current]} ${remainder}` : remainder;
          }
          return;
        }
        result[current] = result[current] ? `${result[current]} ${paragraph}` : paragraph;
      });
      return result;
    }

    function summarizeSectionText(text, facts, limit = 220) {
      const cleaned = removeRiskSentences(removeCardFacts(cleanCardText(text), facts));
      if (!cleaned) return "";
      const sentence = sentenceLimit(cleaned, 2) || cleaned;
      return trimText(sentence, limit);
    }

    function ensureMinimumSentences(summary, source, facts, minimum = 5, maximum = 10) {
      const base = sentenceList(removeCardFacts(summary, facts));
      const used = new Set(base.map((sentence) => sentence.toLowerCase()));
      const candidates = sentenceList(removeRiskSentences(removeCardFacts(source, facts)))
        .filter((sentence) => sentence.length >= 35 && sentence.length <= 260)
        .filter((sentence) => !used.has(sentence.toLowerCase()));
      const result = base.slice();
      candidates.forEach((sentence) => {
        if (result.length < minimum) result.push(sentence);
      });
      [
        "Источник не раскрывает все детали роли, поэтому часть выводов опирается на доступные фрагменты описания.",
        "Перед откликом стоит проверить конкретные задачи, состав команды и ожидаемую зону ответственности.",
        "Отдельно полезно уточнить стек проекта, процессы ревью и формат постановки задач.",
        "Если вакансия заинтересовала, лучше открыть первоисточник и сверить полное описание перед откликом.",
      ].forEach((sentence) => {
        if (result.length < minimum && !result.some((item) => item.toLowerCase() === sentence.toLowerCase())) result.push(sentence);
      });
      return sentenceLimit(result.join(" "), maximum);
    }

    function sentenceList(text) {
      const cleaned = cleanCardText(text);
      if (!cleaned) return [];
      return (cleaned.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [cleaned])
        .map((sentence) => sentence.replace(/\s+/g, " ").trim())
        .filter(Boolean);
    }

    function buildRisks(card) {
      const risks = card.llm_risks || card.llm_score_risks || card.concerns || card.risks || [];
      const filtered = splitListText(listText(risks)).filter((risk) => !isNoiseRisk(risk, card));
      const text = listText(filtered);
      return text || "Критичных рисков не выявлено. Проверьте требования и условия перед откликом.";
    }

    function isNoiseRisk(value, card = {}) {
      const text = String(value || "").toLowerCase().replace("ё", "е");
      const salaryText = normalizeSalaryText(firstText(card, ["salary", "salary_rub", "compensation", "pay"], ""));
      if (/^(но|однако|при этом)\b/.test(text)) return true;
      if (/не критич|некритич|не является критич|не обязательно|не обязателен|не обязательна/.test(text)) return true;
      if (/английск|english/.test(text) && /не указан|не указана|отсутств/.test(text)) return true;
      if (/зарплат|доход|salary/.test(text) && /не указан|не указана|отсутств|уточн/.test(text) && salaryText) return true;
      if (hasPositiveScoreCriterion(card, ["level_match"]) && /(уров|seniority|middle|senior|мидл|сеньор|джун|junior).*(ниже|не подход|вне|ожидан|недостат)|ниже ожиданий.*(мидл|сеньор|middle|senior)/.test(text)) return true;
      if (hasPositiveScoreCriterion(card, ["work_format"]) && /(формат|офис|office|onsite).*(не подход|вне|не совпад|отсутств|не указан)/.test(text)) return true;
      if (hasPositiveScoreCriterion(card, ["city"]) && /(город|локац|москва|moscow).*(не подход|вне|не совпад|отсутств|не указан)/.test(text)) return true;
      if (hasPositiveScoreCriterion(card, ["skills_match", "llm_skills_match"]) && /(нет|отсутств).{0,50}(react|vue|html|css|javascript|typescript|js|ts)/.test(text) && /не критич|не обязатель/.test(text)) return true;
      return /опечат|транслит|translit|transcription|транслитерац|англицизм|capital|case|uppercase|lowercase|регистр|орфограф|написан|несовпада|mismatch/.test(text);
    }

    function hasPositiveScoreCriterion(card, names) {
      const items = Array.isArray(card.score_breakdown)
        ? card.score_breakdown
        : (Array.isArray(card.scoreBreakdown) ? card.scoreBreakdown : []);
      return items.some((item) => names.includes(String(item?.criterion || "")) && Number(item?.points || 0) > 0);
    }

    function extractTechSkills(card) {
      const source = [
        card.description,
        card.requirements,
        card.responsibilities,
        card.conditions,
        card.raw_detail_text,
        card.llm_comment,
        card.llm_explanation_comment,
      ].map((value) => String(value || "")).join(" ").toLowerCase();
      const patterns = [
        ["GeoJSON", /\bgeojson\b/],
        ["OpenLayers", /\bopenlayers\b|open\s+layers/],
        ["React", /\breact\b/],
        ["Vue", /\bvue(?:\.js)?\b/],
        ["Vue 2", /\bvue\s*2\b|\bvue2\b/],
        ["Vue 3", /\bvue\s*3\b|\bvue3\b/],
        ["Nuxt", /\bnuxt(?:\s*3|\.js)?\b/],
        ["JavaScript", /\bjavascript\b|\bjs\b/],
        ["TypeScript", /\btypescript\b|\bts\b/],
        ["React Hooks", /\bhooks?\b|react\s+hooks?/],
        ["React Router", /\brouter\b|react\s+router/],
        ["React Effector", /\beffector\b|react[-\s]?effector/],
        ["Pinia", /\bpinia\b/],
        ["Vuex", /\bvuex\b/],
        ["XState", /\bxstate\b/],
        ["RxJS", /\brxjs\b/],
        ["Next.js", /\bnext\.?js\b/],
        ["Redux", /\bredux\b/],
        ["MobX", /\bmobx\b/],
        ["HTML", /\bhtml\b/],
        ["CSS", /\bcss\b/],
        ["Tailwind CSS", /\btailwind(?:css)?\b/],
        ["Sass", /\bsass\b|\bscss\b/],
        ["Webpack", /\bwebpack\b/],
        ["Vite", /\bvite\b/],
        ["Jest", /\bjest\b/],
        ["Vitest", /\bvitest\b/],
        ["Cypress", /\bcypress\b/],
        ["Playwright", /\bplaywright\b/],
        ["Linux", /\blinux\b/],
      ];
      return patterns.filter(([, pattern]) => pattern.test(source)).map(([label]) => label);
    }

    function renderCurrentCard() {
      const stage = $(state.cardStageId || "cardStage");
      stage.innerHTML = "";
      const closeButton = document.createElement("button");
      closeButton.className = "card-overlay-close";
      closeButton.type = "button";
      closeButton.setAttribute("aria-label", "Закрыть карточки");
      closeButton.textContent = "×";
      closeButton.addEventListener("click", closeCardsOverlay);
      stage.appendChild(closeButton);
      stage.onclick = (event) => {
        if (event.target === stage) closeCardsOverlay();
      };
      const card = state.cards[state.cardIndex];
      if (!card) {
        const empty = document.createElement("div");
        empty.className = "empty-card";
        empty.innerHTML = `<div><h2>Карточки закончились</h2><p>Сохранено вакансий: ${state.favorites.length}. Можно открыть вкладку Избранное.</p></div>`;
        stage.appendChild(empty);
        return;
      }

      const element = document.createElement("article");
      element.className = "vacancy-card";
      element.innerHTML = `
        <span class="card-hint card-hint-up" title="Открыть вакансию">↑</span>
        <span class="card-hint card-hint-left" title="Пропустить">←</span>
        <span class="card-hint card-hint-right" title="Сохранить">→</span>
        <div class="card-head">
          <div>
            <h2 class="card-title"><a class="card-title-link"></a></h2>
            <div class="card-company"></div>
            <div class="card-salary-line hidden"></div>
          </div>
          <div class="match-wrap">
            <div class="score-source-row">
              <button class="match-score" type="button" aria-expanded="false"></button>
              <span class="source-badge hidden" title="Источник вакансии"></span>
            </div>
            <section class="match-details hidden">
              <div class="match-details-head"><h3>Детали Score</h3><button class="match-details-close" type="button" aria-label="Закрыть">×</button></div>
              <ul class="match-detail-list"></ul>
            </section>
          </div>
        </div>
        <div class="card-facts">
          <div class="card-fact"><b>Город</b><span class="card-location"></span></div>
          <div class="card-fact"><b>Формат</b><span class="card-format"></span></div>
          <div class="card-fact"><b>Английский</b><span class="card-english"></span></div>
          <div class="card-fact"><b>Уровень</b><span class="card-level"></span></div>
        </div>
        <div class="skills-line hidden" aria-label="Навыки вакансии"></div>
        <section class="card-section"><h3 class="card-description-title">Описание (LLM)</h3><p class="card-description"></p></section>
        <div class="raw-field-grid hidden"></div>
        <section class="card-section risks"><h3>Риски / минусы</h3><p class="card-risks"></p></section>
        <div class="card-actions">
          <button class="swipe-button skip" type="button"><span class="icon">✕</span><span>Пропустить</span></button>
          <button class="swipe-button save" type="button"><span class="icon">✓</span><span>В избранное</span></button>
        </div>
      `;
      const titleLink = element.querySelector(".card-title-link");
      titleLink.textContent = card.title;
      if (card.url) {
        titleLink.href = card.url;
        titleLink.target = "_blank";
        titleLink.rel = "noopener noreferrer";
      } else {
        titleLink.removeAttribute("href");
      }
      element.querySelector(".card-company").textContent = card.company;
      element.querySelector(".match-wrap").classList.toggle("hidden", Boolean(card.rawCard));
      element.querySelector(".match-score").textContent = `Score: ${formatScore(card.score)}`;
      setSourceBadge(element, card.source);
      setOptionalText(element.querySelector(".card-salary-line"), displaySalaryLine(card.salary));
      setFact(element.querySelector(".card-location").closest(".card-fact"), displayOptional(card.location));
      setFact(element.querySelector(".card-format").closest(".card-fact"), displayOptionalWorkFormat(card.format));
      setFact(element.querySelector(".card-english").closest(".card-fact"), displayOptional(card.english));
      setFact(element.querySelector(".card-level").closest(".card-fact"), displayOptionalLevel(card.level));
      renderSkillLine(element, card);
      element.querySelector(".card-description-title").textContent = card.rawCard ? "Описание" : "Описание (LLM)";
      element.querySelector(".card-description").textContent = card.description;
      renderRawFields(element, card);
      const risksSection = element.querySelector(".card-section.risks");
      risksSection.classList.toggle("hidden", Boolean(card.rawCard));
      element.querySelector(".card-risks").textContent = card.risks;
      if (state.cardMode === "favorites" || state.cardMode === "fetch") {
        element.querySelector(".skip .icon").textContent = "←";
        element.querySelector(".skip span:last-child").textContent = "Назад";
        element.querySelector(".save .icon").textContent = "→";
        element.querySelector(".save span:last-child").textContent = "Далее";
        element.querySelector(".card-hint-left").setAttribute("title", "Назад");
        element.querySelector(".card-hint-right").setAttribute("title", "Далее");
      }
      if (!card.rawCard) {
        renderMatchDetails(element, card);
        element.querySelector(".match-score").addEventListener("click", () => toggleMatchDetails(element));
        element.querySelector(".match-details-close").addEventListener("click", (event) => {
          event.stopPropagation();
          closeMatchDetails(element);
        });
      }
      element.querySelector(".skip").addEventListener("click", () => swipeCard("left"));
      element.querySelector(".save").addEventListener("click", () => swipeCard("right"));
      setupCardSwipe(element);
      stage.appendChild(element);
      element.scrollTop = 0;
    }

    function setupCardSwipe(element) {
      let startX = 0;
      let startY = 0;
      let lastX = 0;
      let dragging = false;
      let horizontal = false;
      let pointerId = null;

      element.addEventListener("pointerdown", (event) => {
        if (state.swiping || event.button !== 0 || event.target.closest("button, a, .match-wrap")) return;
        startX = event.clientX;
        startY = event.clientY;
        lastX = startX;
        dragging = true;
        horizontal = false;
        pointerId = event.pointerId;
      });

      element.addEventListener("pointermove", (event) => {
        if (!dragging || pointerId !== event.pointerId) return;
        const dx = event.clientX - startX;
        const dy = event.clientY - startY;
        if (!horizontal && Math.abs(dx) > 14 && Math.abs(dx) > Math.abs(dy) * 1.25) {
          horizontal = true;
          element.setPointerCapture?.(event.pointerId);
        }
        if (!horizontal) return;
        event.preventDefault();
        lastX = event.clientX;
        const limited = Math.max(-150, Math.min(150, dx));
        element.style.transform = `translateX(${limited}px) rotate(${limited / 18}deg)`;
      });

      const finish = (event) => {
        if (!dragging || pointerId !== event.pointerId) return;
        const dx = lastX - startX;
        dragging = false;
        pointerId = null;
        element.releasePointerCapture?.(event.pointerId);
        if (horizontal && Math.abs(dx) >= 80) {
          element.style.transform = "";
          swipeCard(dx > 0 ? "right" : "left");
          return;
        }
        element.style.transform = "";
      };

      element.addEventListener("pointerup", finish);
      element.addEventListener("pointercancel", finish);
    }

    function renderRawFields(element, card) {
      const grid = element.querySelector(".raw-field-grid");
      grid.innerHTML = "";
      const fields = Array.isArray(card.rawFields) ? card.rawFields : [];
      fields.forEach((field) => {
        const item = document.createElement("div");
        item.className = "raw-field";
        const label = document.createElement("b");
        label.textContent = field.label;
        const value = document.createElement("span");
        value.textContent = field.value;
        item.appendChild(label);
        item.appendChild(value);
        grid.appendChild(item);
      });
      grid.classList.toggle("hidden", !fields.length);
    }

    function setOptionalText(node, value) {
      const text = cleanFactText(value);
      node.textContent = text;
      node.classList.toggle("hidden", !text);
    }

    function setFact(node, value, options = {}) {
      const text = cleanFactText(value);
      node.querySelector("span").textContent = text;
      node.classList.toggle("hidden", !text && !options.keepVisible);
    }

    function displayOptional(value) {
      return cleanFactText(value);
    }

    function displaySalaryLine(value) {
      const text = normalizeSalaryText(cleanFactText(value));
      return text ? `ЗП - ${text}` : "ЗП - ?";
    }

    function normalizeSalaryText(value) {
      return String(value || "")
        .replace(/^[\s—–-]+/, "")
        .replace(/\s*[—–-]\s*[—–-]+\s*/g, " — ")
        .replace(/\s+/g, " ")
        .trim();
    }

    function displayOptionalWorkFormat(value) {
      const text = String(value || "").toLowerCase().replace("ё", "е");
      if (/remote|удален/.test(text)) return "Удаленка";
      if (/hybrid|гибрид/.test(text)) return "Гибрид";
      if (/onsite|office|офис|полный день/.test(text)) return "Офис";
      return "";
    }

    function displayOptionalLevel(value) {
      const text = String(value || "").toLowerCase().replace("ё", "е");
      if (/intern|internship|стаж[её]р|стажиров/.test(text)) return "Internship";
      if (/entry/.test(text)) return "Entry";
      if (/junior|джун/.test(text)) return "Junior";
      if (/middle|мидл/.test(text)) return "Middle";
      if (/senior|сеньор/.test(text)) return "Senior";
      if (/lead|team[\s-]?lead|руковод/.test(text)) return "Lead";
      return "";
    }

    function setSourceBadge(element, value) {
      const badge = element.querySelector(".source-badge");
      const source = baseSourceName(String(value || "").trim().toLowerCase());
      if (!source || source === "unknown") {
        badge.className = "source-badge hidden";
        badge.textContent = "";
        return;
      }
      const isHh = source === "hh";
      const isSuperjob = source === "superjob";
      badge.className = `source-badge ${isHh ? "hh" : (isSuperjob ? "superjob" : "other")}`;
      badge.textContent = isHh ? "HH" : (isSuperjob ? "SJ" : sourceBadgeText(source));
      badge.title = `Источник: ${sourceDisplayName(source)}`;
    }

    function sourceBadgeText(source) {
      return String(source || "?")
        .replace(/[_-]+/g, " ")
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((part) => part[0] || "")
        .join("")
        .toUpperCase()
        .slice(0, 3) || "?";
    }

    function renderSkillLine(element, card) {
      const line = element.querySelector(".skills-line");
      line.innerHTML = "";
      const matched = splitListText(card.matchedSkills).filter(isTechnicalStackItem);
      const vacancySkills = splitListText(card.vacancySkills).filter(isTechnicalStackItem);
      const seen = new Set();
      const ordered = [];
      matched.concat(vacancySkills).forEach((skill) => {
        const key = skill.toLowerCase();
        if (seen.has(key)) return;
        seen.add(key);
        ordered.push({ label: skill, matched: matched.some((item) => item.toLowerCase() === key) });
      });
      if (!ordered.length) {
        const label = document.createElement("span");
        label.className = "skills-label";
        label.textContent = "Технический стек";
        const content = document.createElement("span");
        content.className = "skills-content";
        content.textContent = "—";
        line.appendChild(label);
        line.appendChild(content);
        line.classList.remove("hidden");
        return;
      }
      const label = document.createElement("span");
      label.className = "skills-label";
      label.textContent = "Технический стек";
      const content = document.createElement("span");
      content.className = "skills-content";
      line.appendChild(label);
      line.appendChild(content);
      chunkSkills(ordered, 2).forEach((group, index) => {
        if (index > 0) content.appendChild(document.createTextNode(" "));
        const bullet = document.createElement("span");
        bullet.textContent = `• `;
        bullet.setAttribute("aria-hidden", "true");
        content.appendChild(bullet);
        group.forEach((skill, skillIndex) => {
          if (skillIndex > 0) content.appendChild(document.createTextNode(", "));
          const item = document.createElement("span");
          item.className = skill.matched ? "matched-skill" : "";
          item.textContent = skill.label;
          content.appendChild(item);
        });
      });
      line.classList.remove("hidden");
    }

    function isTechnicalStackItem(value) {
      const text = cleanFactText(value);
      if (!text || text.length > 34) return false;
      if (isRoleLikeSkill(text)) return false;
      const lower = text.toLowerCase().replace(/ё/g, "е");
      if (/английск|english|зарплат|работ|команд|формат|финтех|fintech|офис|москва|предлагаем|режим|абонемент|пользовательск|задач|продукт/.test(lower)) return false;
      return /^(react|vue|vue\s?[23]|vuex|pinia|nuxt(?:\s?3)?|javascript|typescript|js|ts|html|css|sass|scss|tailwind(?:\s?css)?|vite|webpack|jest|vitest|cypress|playwright|rxjs|redux|mobx|xstate|geojson|openlayers|next\.?js|linux)$/i.test(text);
    }

    function chunkSkills(items, size) {
      const chunks = [];
      for (let index = 0; index < items.length; index += size) {
        chunks.push(items.slice(index, index + size));
      }
      return chunks;
    }

    function isRoleLikeSkill(value) {
      const text = String(value || "").trim().toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ");
      const roleWords = new Set([
        "analyst",
        "аналитик",
        "product analyst",
        "продуктовый аналитик",
        "junior product analyst",
        "data analyst",
        "business analyst",
        "system analyst",
        "bi analyst",
        "marketing analyst",
        "ai analyst",
        "продакт аналитик",
        "бизнес аналитик",
        "системный аналитик",
      ]);
      return roleWords.has(text) || /^(junior|middle|senior)\s+.+\s+analyst$/.test(text);
    }

    function toggleMatchDetails(element) {
      const details = element.querySelector(".match-details");
      const button = element.querySelector(".match-score");
      const hidden = details.classList.toggle("hidden");
      button.setAttribute("aria-expanded", String(!hidden));
    }

    function closeMatchDetails(element) {
      const details = element.querySelector(".match-details");
      const button = element.querySelector(".match-score");
      details.classList.add("hidden");
      button.setAttribute("aria-expanded", "false");
    }

    function renderMatchDetails(element, card) {
      const list = element.querySelector(".match-detail-list");
      list.innerHTML = "";
      const items = card.scoreBreakdown.length ? card.scoreBreakdown : [{ criterion: "score", points: card.score, evidence: "Детализация score отсутствует в trace." }];
      items.forEach((item) => {
        const points = Number(item.points || 0);
        const li = document.createElement("li");
        const pointsNode = document.createElement("span");
        pointsNode.className = "match-points" + (points < 0 ? " negative" : "");
        pointsNode.textContent = `${points > 0 ? "+" : ""}${points}`;
        const textNode = document.createElement("span");
        const evidence = formatMatchEvidence(item);
        textNode.textContent = `${criterionLabel(item.criterion)}${evidence ? `: ${evidence}` : ""}`;
        li.appendChild(pointsNode);
        li.appendChild(textNode);
        list.appendChild(li);
      });
    }

    function criterionLabel(value) {
      return {
        role_match: "Совпадение роли",
        irrelevant_role: "Нерелевантная роль",
        target_role_mismatch: "Должность вне целевых ролей",
        llm_role_match: "LLM: совпадение должности",
        llm_role_mismatch: "LLM: должность вне целевых ролей",
        senior_lead_middle: "Неподходящий seniority",
        level_match: "Подходящий уровень",
        skills_match: "Совпадение навыков",
        llm_skills_match: "LLM: совпадение навыков",
        llm_skills_mismatch: "LLM: слабое совпадение навыков",
        work_format: "Формат работы",
        work_format_mismatch: "Формат работы вне предпочтений",
        city: "Город",
        city_mismatch: "Город / удалёнка вне предпочтений",
        salary: "Зарплата",
        salary_below_min: "Зарплата ниже минимума",
        salary_missing: "Зарплата не указана",
        english: "Английский",
        english_mismatch: "Английский выше уровня кандидата",
        skills_mismatch: "Нет совпадений по навыкам",
        level_mismatch: "Уровень вне предпочтений",
        fresh: "Свежая вакансия",
        missing_link: "Нет ссылки",
        stop_word: "Стоп-слово",
        score: "Итоговый score"
      }[value] || String(value || "Критерий");
    }

    function formatEvidence(value) {
      if (Array.isArray(value)) return trimText(value.filter(Boolean).join(", "), 48);
      if (typeof value === "object" && value) return trimText(JSON.stringify(value), 48);
      return trimText(value, 48);
    }

    function formatMatchEvidence(item) {
      if (!item || item.evidence === undefined) return "";
      const criterion = String(item.criterion || "");
      if (criterion === "work_format_mismatch") return "не указан или не подходит";
      if (criterion === "skills_mismatch") return formatEvidence(item.evidence);
      if (criterion.endsWith("_mismatch") && String(item.evidence || "").length > 40) return "не совпадает с критериями";
      return formatEvidence(item.evidence);
    }

    function formatScore(score) {
      const number = Number(score);
      if (!Number.isFinite(number)) return String(score || "-");
      if (number <= 10) return `${number}/10`;
      return `${Math.max(0, Math.round(number))}`;
    }

    function swipeCard(direction) {
      if (state.swiping) return;
      const card = state.cards[state.cardIndex];
      const element = $(state.cardStageId || "cardStage").querySelector(".vacancy-card");
      if (!card || !element) return;
      state.swiping = true;
      if (["rank", "quick"].includes(state.cardMode) && direction === "right" && !state.favorites.some((item) => item.id === card.id)) {
        state.favorites.push(card);
        saveFavorites();
        renderFavorites();
      }
      element.classList.add(direction === "right" ? "swipe-right" : "swipe-left");
      window.setTimeout(() => {
        if (["favorites", "fetch"].includes(state.cardMode) && direction === "left") {
          state.cardIndex = Math.max(0, state.cardIndex - 1);
        } else {
          state.cardIndex += 1;
        }
        state.swiping = false;
        renderCurrentCard();
      }, 170);
    }

    function openCurrentCardLink() {
      const card = state.cards[state.cardIndex];
      if (!card || !card.url) return;
      window.open(card.url, "_blank", "noopener,noreferrer");
    }

    function handleCardHotkey(event, direction) {
      const rankCardsVisible = state.rankView === "cards" && !$("rankTab").classList.contains("hidden");
      const quickCardsVisible = state.quickView === "cards" && !$("quickTab").classList.contains("hidden");
      const fetchCardsVisible = state.fetchView === "cards" && !$("fetchTab").classList.contains("hidden");
      const favoriteCardsVisible = state.cardStageId === "favoriteCardStage" && !$("favoriteCardStage").classList.contains("hidden");
      if (!rankCardsVisible && !quickCardsVisible && !fetchCardsVisible && !favoriteCardsVisible) return;
      if (isTypingTarget(event.target)) return;
      event.preventDefault();
      if (direction === "up") {
        openCurrentCardLink();
        return;
      }
      swipeCard(direction);
    }

    function isTypingTarget(target) {
      const tagName = String(target?.tagName || "").toLowerCase();
      return ["input", "select", "textarea"].includes(tagName) || Boolean(target?.isContentEditable);
    }

    function renderFavorites() {
      $("favoritesCount").textContent = state.favorites.length;
      const list = $("favoritesList");
      list.innerHTML = "";
      if (!state.favorites.length) {
        const empty = document.createElement("div");
        empty.className = "empty-card";
        empty.innerHTML = "<div><h2>Избранное пусто</h2><p>Сохраняйте вакансии галочкой в карточках после ранжирования.</p></div>";
        list.appendChild(empty);
        return;
      }
      state.favorites.forEach((card, index) => {
        const item = document.createElement("article");
        item.className = "favorite-item";
        item.tabIndex = 0;
        item.setAttribute("role", "button");
        item.setAttribute("aria-label", `Открыть подробности вакансии ${card.title}`);
        item.innerHTML = `
          <button class="favorite-remove" type="button" aria-label="Удалить из избранного" title="Удалить из избранного">×</button>
          <h3></h3>
          <div class="favorite-meta"></div>
          <p></p>
        `;
        item.querySelector("h3").textContent = card.title;
        item.querySelector(".favorite-meta").textContent = `${card.company} · Score: ${formatScore(card.score)}`;
        item.querySelector("p").textContent = card.description;
        item.addEventListener("click", () => openFavoriteCards(index));
        item.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openFavoriteCards(index);
          }
        });
        item.querySelector(".favorite-remove").addEventListener("click", (event) => {
          event.stopPropagation();
          removeFavorite(index);
        });
        list.appendChild(item);
      });
    }

    function removeFavorite(index) {
      state.favorites.splice(index, 1);
      saveFavorites();
      renderFavorites();
    }

    function openFavoriteCards(index = 0) {
      if (!state.favorites.length) return;
      state.cards = state.favorites.slice();
      state.cardMode = "favorites";
      state.cardIndex = Math.max(0, Math.min(index, state.cards.length - 1));
      state.cardStageId = "favoriteCardStage";
      $("favoriteCardStage").classList.remove("hidden");
      $("favoriteCardStage").classList.add("card-overlay");
      renderCurrentCard();
    }

    function openFavoriteModal(index) {
      const card = state.favorites[index];
      if (!card) return;
      $("favoriteModalTitle").textContent = card.title || "Вакансия";
      $("favoriteModalMeta").textContent = `${card.company || "Компания не указана"} · Score: ${formatScore(card.score)}`;
      $("favoriteModalDescription").textContent = card.description || "Описание не найдено.";
      $("favoriteModalLlm").textContent = [
        `Зарплата: ${displayValue(card.salary, "не указана")}`,
        `Город: ${displayValue(card.location, "не указан")}`,
        `Формат: ${displayWorkFormat(card.format)}`,
        `Английский: ${displayValue(card.english, "не указан")}`,
        `Уровень: ${displayValue(card.level, "не указан")}`,
      ].join("\n");
      $("favoriteModalRisks").textContent = card.risks || "Риски не указаны.";
      const link = $("favoriteModalLink");
      if (card.url) {
        link.href = card.url;
        link.classList.remove("hidden");
      } else {
        link.href = "#";
        link.classList.add("hidden");
      }
      $("favoriteModal").classList.remove("hidden");
      $("closeFavoriteModal").focus();
    }

    function closeFavoriteModal() {
      $("favoriteModal").classList.add("hidden");
    }

    function loadStoredFavorites() {
      try {
        const raw = window.localStorage.getItem("vacancyMatchFavorites");
        const parsed = JSON.parse(raw || "[]");
        return Array.isArray(parsed) ? normalizeCards(parsed) : [];
      } catch (error) {
        return [];
      }
    }

    function saveFavorites() {
      try {
        window.localStorage.setItem("vacancyMatchFavorites", JSON.stringify(state.favorites));
      } catch (error) {
        return;
      }
    }

    function loadStoredNotifications() {
      try {
        const raw = window.localStorage.getItem("vacancyMatchNotifications");
        const parsed = JSON.parse(raw || "[]");
        if (!Array.isArray(parsed)) return [];
        return parsed
          .filter((item) => item && typeof item === "object")
          .map((item) => ({
            id: String(item.id || crypto.randomUUID?.() || Date.now()),
            scope: ["rank", "fetch", "quick", "email"].includes(item.scope) ? item.scope : "rank",
            title: String(item.title || "Событие завершено").slice(0, 90),
            island: String(item.island || "готово").slice(0, 34),
            status: String(item.status || "").slice(0, 120),
            progress: Number(item.progress || 0),
            details: String(item.details || "").slice(0, 60000),
            createdAt: Number(item.createdAt || Date.now()),
            queueStatus: String(item.queueStatus || "").slice(0, 24),
            queue_position: Number(item.queue_position || 0),
            openCards: Boolean(item.openCards),
            canDelete: Boolean(item.canDelete),
            queueJobId: String(item.queueJobId || ""),
            cancelled: Boolean(item.cancelled),
            cards: Array.isArray(item.cards) ? item.cards.slice(0, 30) : [],
            created_path: String(item.created_path || ""),
            trace_path: String(item.trace_path || ""),
            criteria_path: String(item.criteria_path || ""),
            filter_path: String(item.filter_path || ""),
            endpoint: String(item.endpoint || ""),
            request_payload: item.request_payload && typeof item.request_payload === "object" ? item.request_payload : {},
            result_snapshot: item.result_snapshot && typeof item.result_snapshot === "object" ? item.result_snapshot : {}
          }))
          .slice(0, 10);
      } catch (error) {
        return [];
      }
    }

    function saveNotifications() {
      try {
        window.localStorage.setItem("vacancyMatchNotifications", JSON.stringify(state.notifications.slice(0, 10)));
      } catch (error) {
        return;
      }
    }

    function loadNotificationSound() {
      try {
        return window.localStorage.getItem("vacancyMatchNotificationSound") === "1";
      } catch (error) {
        return false;
      }
    }

    function saveNotificationSound() {
      try {
        window.localStorage.setItem("vacancyMatchNotificationSound", state.notificationSound ? "1" : "0");
      } catch (error) {
        return;
      }
    }

    function playNotificationSound(ok) {
      if (!state.notificationSound) return;
      try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) return;
        const context = state.audioContext || new AudioContext();
        state.audioContext = context;
        const now = context.currentTime;
        const gain = context.createGain();
        gain.gain.setValueAtTime(0.0001, now);
        gain.gain.exponentialRampToValueAtTime(0.035, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.42);
        gain.connect(context.destination);
        const notes = ok ? [523.25, 659.25] : [392.0, 329.63];
        notes.forEach((frequency, index) => {
          const osc = context.createOscillator();
          osc.type = "sine";
          osc.frequency.setValueAtTime(frequency, now + index * 0.11);
          osc.connect(gain);
          osc.start(now + index * 0.11);
          osc.stop(now + index * 0.11 + 0.18);
        });
      } catch (error) {
        return;
      }
    }

    async function postJson(url, payload) {
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        return await response.json();
      } catch (error) {
        return { ok: false, error: String(error) };
      }
    }

    async function getJson(url) {
      try {
        const response = await fetch(url);
        return await response.json();
      } catch (error) {
        return { ok: false, error: String(error), status: "error" };
      }
    }

    async function openMetadataModal(kind, path = "") {
      if (kind === "criteria" && path) {
        $("criteriaFile").value = path;
        updateFileMeta();
      }
      if (kind === "filter" && path) {
        $("fetchCriteriaFile").value = path;
        updateFileMeta();
      }
      const file = selectedMetadataFile(kind);
      if (!file) return;
      state.metadataKind = kind;
      const isCriteria = kind === "criteria" || kind === "filter";
      state.criteriaEditor = {};
      state.criteriaImportance = {};
      $("metadataDialog").classList.toggle("wide", isCriteria);
      $("metadataModalTitle").textContent = kind === "vacancy" ? "Метаданные файла вакансий" : (kind === "filter" ? "Метаданные файла фильтров" : "Метаданные файла критериев");
      $("metadataName").value = file.name || "";
      $("metadataDescription").value = file.description || "";
      $("metadataStatus").textContent = "";
      $("metadataStatus").classList.remove("error");
      $("criteriaEditor").classList.toggle("hidden", !isCriteria);
      $("criteriaEditorFields").innerHTML = "";
      $("metadataModal").classList.remove("hidden");
      $("metadataName").focus();
      if (isCriteria) {
        $("metadataStatus").textContent = "Загрузка CSV...";
        const response = await fetch(`/api/file?path=${encodeURIComponent(file.path)}`);
        if (response.ok) {
          const csvText = await response.text();
          state.criteriaEditor = parseCriteriaCsv(csvText, kind);
          renderCriteriaEditor();
          $("metadataStatus").textContent = "";
        } else {
          $("metadataStatus").textContent = "Не удалось загрузить CSV.";
          $("metadataStatus").classList.add("error");
        }
      }
    }

    function closeMetadataModal() {
      $("metadataModal").classList.add("hidden");
      state.metadataKind = "";
      state.criteriaEditor = {};
      state.criteriaImportance = {};
      $("metadataDialog").classList.remove("wide");
      $("metadataStatus").textContent = "";
      $("metadataStatus").classList.remove("error");
    }

    function activeCriteriaFields() {
      return state.metadataKind === "filter" ? filterFields : criteriaFields;
    }

    function parseCriteriaCsv(csvText, kind = "criteria") {
      const rows = parseCsvRows(csvText);
      const header = rows[0] || [];
      const row = rows[1] || [];
      const result = {};
      const fields = kind === "filter" ? filterFields : criteriaFields;
      fields.forEach((field) => {
        const index = header.indexOf(field.key);
        const raw = index >= 0 ? row[index] || "" : "";
        result[field.key] = field.type === "chips" || field.type === "multi-choice" ? splitCriteriaList(raw) : raw;
        (field.toggles || []).forEach((toggle) => {
          const toggleIndex = header.indexOf(toggle.key);
          result[toggle.key] = toggleIndex >= 0 ? row[toggleIndex] || "" : "";
        });
      });
      if (kind === "criteria") {
        const importanceIndex = header.indexOf("criterion_importance");
        state.criteriaImportance = parseCriterionImportance(importanceIndex >= 0 ? row[importanceIndex] || "" : "");
      }
      return result;
    }

    function parseCriterionImportance(raw) {
      const parsed = { ...importanceDefaults };
      String(raw || "").split(/[;|,\n]+/).forEach((item) => {
        const match = item.trim().match(/^([a-zA-Z_]+)\s*[:=]\s*(low|medium|high|низк\w*|средн\w*|высок\w*)$/i);
        if (!match) return;
        const key = match[1];
        let priority = match[2].toLowerCase();
        if (priority.startsWith("низк")) priority = "low";
        if (priority.startsWith("сред")) priority = "medium";
        if (priority.startsWith("выс")) priority = "high";
        if (importanceDefaults[key] && importanceConfig[priority]) parsed[key] = priority;
      });
      return parsed;
    }

    function parseCsvRows(csvText) {
      const rows = [];
      let row = [];
      let value = "";
      let quoted = false;
      const text = String(csvText || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      for (let index = 0; index < text.length; index += 1) {
        const char = text[index];
        const next = text[index + 1];
        if (char === '"' && quoted && next === '"') {
          value += '"';
          index += 1;
        } else if (char === '"') {
          quoted = !quoted;
        } else if (char === "," && !quoted) {
          row.push(value);
          value = "";
        } else if (char === "\n" && !quoted) {
          row.push(value);
          if (row.some((cell) => String(cell).trim())) rows.push(row);
          row = [];
          value = "";
        } else {
          value += char;
        }
      }
      row.push(value);
      if (row.some((cell) => String(cell).trim())) rows.push(row);
      if (rows[0]?.[0]) rows[0][0] = rows[0][0].replace(/^\uFEFF/, "");
      return rows;
    }

    function splitCriteriaList(value) {
      return String(value || "")
        .split(/[;|]/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function renderCriteriaEditor() {
      const container = $("criteriaEditorFields");
      container.innerHTML = "";
      container.classList.toggle("filter-grid", state.metadataKind === "filter");
      activeCriteriaFields().forEach((field) => {
        const wrapper = document.createElement("div");
        wrapper.className = "field" + (["chips", "multi-choice", "salary"].includes(field.type) ? " wide-field" : "");
        if (state.metadataKind === "criteria" && importanceDefaults[field.key]) wrapper.classList.add("with-priority");
        updateFilterFieldState(wrapper, field.key);
        const labelLine = document.createElement("div");
        labelLine.className = "filter-label-line";
        const label = document.createElement("label");
        label.textContent = field.label;
        label.setAttribute("for", `criteriaField_${field.key}`);
        labelLine.appendChild(label);
        if (state.metadataKind === "filter") {
          const stateBadge = document.createElement("span");
          stateBadge.className = "filter-state";
          labelLine.appendChild(stateBadge);
        }
        wrapper.appendChild(labelLine);
        updateFilterFieldState(wrapper, field.key);
        if (field.type === "chips") {
          wrapper.appendChild(createChipEditor(field));
        } else if (field.type === "salary") {
          wrapper.appendChild(createSalaryControl(field, wrapper));
        } else if (field.type === "choice" || field.type === "multi-choice") {
          wrapper.appendChild(createChoiceChips(field, wrapper));
        } else {
          const input = document.createElement("input");
          input.id = `criteriaField_${field.key}`;
          input.type = field.type;
          input.placeholder = field.placeholder || "";
          input.value = state.criteriaEditor[field.key] || "";
          input.addEventListener("input", () => {
            state.criteriaEditor[field.key] = input.value;
            updateFilterFieldState(wrapper, field.key);
          });
          wrapper.appendChild(input);
        }
        (field.toggles || []).forEach((toggle) => {
          wrapper.appendChild(createCriteriaToggle(toggle, wrapper));
        });
        if (state.metadataKind === "criteria" && importanceDefaults[field.key]) {
          wrapper.appendChild(createCriterionPriority(field.key));
        }
        container.appendChild(wrapper);
      });
    }

    function createCriterionPriority(key) {
      const panel = document.createElement("div");
      panel.className = "criteria-priority-panel";
      const priority = state.criteriaImportance[key] || importanceDefaults[key] || "medium";
      const control = document.createElement("div");
      control.className = "criterion-priority";
      control.dataset.priority = priority;
      control.setAttribute("aria-label", "Важность критерия");
      ["low", "medium", "high"].forEach((value) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `priority-step ${value}`;
        button.dataset.priority = value;
        button.title = importanceConfig[value].label;
        button.addEventListener("click", () => {
          state.criteriaImportance[key] = value;
          renderCriteriaEditor();
        });
        control.appendChild(button);
      });
      const hint = document.createElement("div");
      hint.className = "criteria-priority-hint";
      hint.textContent = importanceConfig[priority]?.hint || "";
      panel.appendChild(control);
      panel.appendChild(hint);
      return panel;
    }

    function createCriteriaToggle(toggle, wrapper) {
      const label = document.createElement("label");
      label.className = "criteria-toggle";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = isCriteriaToggleEnabled(state.criteriaEditor[toggle.key]);
      input.addEventListener("change", () => {
        state.criteriaEditor[toggle.key] = input.checked ? "yes" : "";
        updateFilterFieldState(wrapper, toggle.key);
      });
      const text = document.createElement("span");
      text.textContent = toggle.label;
      label.appendChild(input);
      label.appendChild(text);
      return label;
    }

    function isCriteriaToggleEnabled(value) {
      return /^(1|true|yes|y|on|да|вкл|включено)$/i.test(String(value || "").trim());
    }

    function updateFilterFieldState(wrapper, key) {
      if (state.metadataKind !== "filter") return;
      const value = state.criteriaEditor[key];
      const filled = Array.isArray(value) ? value.length > 0 : Boolean(String(value || "").trim());
      wrapper.classList.toggle("filter-field-filled", filled);
      wrapper.classList.toggle("filter-field-empty", !filled);
      const badge = wrapper.querySelector(".filter-state");
      if (badge) badge.textContent = filled ? "используется" : "не используется";
    }

    function createSalaryControl(field, wrapper) {
      const control = document.createElement("div");
      control.className = "salary-control";
      control.id = `criteriaField_${field.key}`;
      const current = normalizeSalaryValue(state.criteriaEditor[field.key]);
      const range = document.createElement("input");
      range.type = "range";
      range.min = "0";
      range.max = "500000";
      range.step = "5000";
      range.value = current || "0";
      const number = document.createElement("input");
      number.type = "number";
      number.min = "0";
      number.max = "1000000";
      number.step = "5000";
      number.placeholder = field.placeholder || "";
      number.value = current;
      const sync = (value) => {
        const normalized = normalizeSalaryValue(value);
        state.criteriaEditor[field.key] = normalized;
        number.value = normalized;
        range.value = String(Math.min(Number(normalized || 0), Number(range.max)));
        updateFilterFieldState(wrapper, field.key);
      };
      range.addEventListener("input", () => sync(range.value));
      number.addEventListener("input", () => sync(number.value));
      control.appendChild(range);
      control.appendChild(number);
      const hints = document.createElement("div");
      hints.className = "salary-hints";
      hints.innerHTML = "<span>0 ₽</span><span>250 000 ₽</span><span>500 000 ₽</span>";
      control.appendChild(hints);
      return control;
    }

    function normalizeSalaryValue(value) {
      const digits = String(value || "").replace(/[^\d]/g, "");
      if (!digits) return "";
      return String(Math.max(0, Math.min(1000000, Number(digits))));
    }

    function createChoiceChips(field, wrapper) {
      const group = document.createElement("div");
      group.className = "choice-chip-group";
      group.id = `criteriaField_${field.key}`;
      const current = field.type === "multi-choice"
        ? (Array.isArray(state.criteriaEditor[field.key]) ? state.criteriaEditor[field.key] : splitCriteriaList(state.criteriaEditor[field.key]))
        : String(state.criteriaEditor[field.key] || "").trim();
      (field.options || []).forEach((option) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "choice-chip";
        const value = choiceOptionValue(option);
        button.textContent = choiceOptionLabel(option);
        const active = field.type === "multi-choice"
          ? current.some((item) => item.toLowerCase() === value.toLowerCase())
          : current.toLowerCase() === value.toLowerCase();
        button.classList.toggle("active", active);
        button.addEventListener("click", () => {
          if (field.type === "multi-choice") {
            const values = Array.isArray(state.criteriaEditor[field.key]) ? [...state.criteriaEditor[field.key]] : [];
            const index = values.findIndex((item) => item.toLowerCase() === value.toLowerCase());
            if (index >= 0) values.splice(index, 1);
            else if (value) values.push(value);
            state.criteriaEditor[field.key] = values;
          } else {
            state.criteriaEditor[field.key] = current.toLowerCase() === value.toLowerCase() ? "" : value;
          }
          renderCriteriaEditor();
        });
        group.appendChild(button);
      });
      return group;
    }

    function choiceOptionValue(option) {
      return typeof option === "object" && option ? String(option.value || "") : String(option || "");
    }

    function choiceOptionLabel(option) {
      if (typeof option === "object" && option) return String(option.label || option.value || "Любой");
      return String(option || "Любой");
    }

    function createChipEditor(field) {
      const editor = document.createElement("div");
      editor.className = "chip-editor";
      editor.id = `criteriaField_${field.key}`;
      const values = Array.isArray(state.criteriaEditor[field.key]) ? state.criteriaEditor[field.key] : [];
      values.forEach((value, index) => {
        const chip = document.createElement("span");
        chip.className = "criteria-chip";
        chip.textContent = value;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", `Удалить ${value}`);
        remove.textContent = "×";
        remove.addEventListener("click", () => {
          state.criteriaEditor[field.key].splice(index, 1);
          renderCriteriaEditor();
        });
        chip.appendChild(remove);
        editor.appendChild(chip);
      });
      const input = document.createElement("input");
      input.placeholder = field.placeholder || "";
      const suggestions = document.createElement("div");
      suggestions.className = "chip-suggestions hidden";
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === ",") {
          event.preventDefault();
          addCriteriaChip(field.key, input.value);
          input.value = "";
          renderCitySuggestions(field, input, suggestions);
        }
        if (event.key === "Backspace" && !input.value && values.length) {
          state.criteriaEditor[field.key].pop();
          renderCriteriaEditor();
        }
      });
      input.addEventListener("input", () => renderCitySuggestions(field, input, suggestions));
      input.addEventListener("focus", () => renderCitySuggestions(field, input, suggestions));
      input.addEventListener("blur", () => {
        window.setTimeout(() => {
          addCriteriaChip(field.key, input.value);
          input.value = "";
          suggestions.classList.add("hidden");
        }, 120);
      });
      editor.appendChild(input);
      editor.appendChild(suggestions);
      return editor;
    }

    function renderCitySuggestions(field, input, suggestions) {
      if (field.key !== "preferred_cities") return;
      const query = String(input.value || "").trim().toLowerCase().replace("ё", "е");
      suggestions.innerHTML = "";
      if (query.length < 2) {
        suggestions.classList.add("hidden");
        return;
      }
      const selected = Array.isArray(state.criteriaEditor[field.key]) ? state.criteriaEditor[field.key] : [];
      const matches = citySuggestions
        .filter((city) => city.toLowerCase().replace("ё", "е").includes(query))
        .filter((city) => !selected.some((item) => item.toLowerCase().replace("ё", "е") === city.toLowerCase().replace("ё", "е")))
        .slice(0, 6);
      if (!matches.length) {
        suggestions.classList.add("hidden");
        return;
      }
      matches.forEach((city) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chip-suggestion";
        button.textContent = city;
        button.addEventListener("mousedown", (event) => {
          event.preventDefault();
          addCriteriaChip(field.key, city);
          input.value = "";
          suggestions.classList.add("hidden");
        });
        suggestions.appendChild(button);
      });
      suggestions.classList.remove("hidden");
    }

    function addCriteriaChip(key, rawValue) {
      const value = String(rawValue || "").trim().replace(/\s+/g, " ");
      if (!value) return;
      const values = Array.isArray(state.criteriaEditor[key]) ? state.criteriaEditor[key] : [];
      if (!values.some((item) => item.toLowerCase() === value.toLowerCase())) values.push(value);
      state.criteriaEditor[key] = values;
      renderCriteriaEditor();
    }

    function buildCriteriaCsv() {
      const columns = activeCriteriaColumns();
      if (state.metadataKind === "criteria") columns.push("criterion_importance");
      const row = columns.map((key) => {
        if (key === "criterion_importance") return serializeCriterionImportance();
        const value = state.criteriaEditor[key];
        if (["target_roles_use_description", "salary_missing_penalty"].includes(key)) {
          return isCriteriaToggleEnabled(value) ? "yes" : "";
        }
        return Array.isArray(value) ? value.join("; ") : String(value || "").trim();
      });
      return `${columns.join(",")}\n${row.map(csvEscape).join(",")}\n`;
    }

    function activeCriteriaColumns() {
      const columns = [];
      activeCriteriaFields().forEach((field) => {
        columns.push(field.key);
        (field.toggles || []).forEach((toggle) => columns.push(toggle.key));
      });
      return [...new Set(columns)];
    }

    function serializeCriterionImportance() {
      const values = { ...importanceDefaults, ...state.criteriaImportance };
      return Object.keys(importanceDefaults)
        .map((key) => `${key}:${importanceConfig[values[key]] ? values[key] : importanceDefaults[key]}`)
        .join("; ");
    }

    function csvEscape(value) {
      const text = String(value || "");
      if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
      return text;
    }

    function openCriteriaPrompt() {
      $("criteriaPromptText").value = "";
      $("criteriaPromptStatus").textContent = "";
      $("criteriaPromptStatus").classList.remove("error");
      syncCriteriaPromptButton();
      $("criteriaPromptModal").classList.remove("hidden");
      $("criteriaPromptText").focus();
    }

    function closeCriteriaPrompt() {
      if (state.criteriaPromptBusy) return;
      $("criteriaPromptModal").classList.add("hidden");
      $("criteriaPromptStatus").textContent = "";
      $("criteriaPromptStatus").classList.remove("error");
    }

    async function generateCriteriaFromPrompt() {
      if (state.criteriaPromptBusy) return;
      const text = $("criteriaPromptText").value.trim();
      if (!text) {
        $("criteriaPromptStatus").textContent = "Создаю пустой CSV без LLM.";
        $("criteriaPromptStatus").classList.remove("error");
        state.criteriaPromptBusy = true;
        $("generateCriteria").disabled = true;
        setTopState("Создание пустого файла");
        const result = await postJson("/api/generate-criteria", { text });
        state.criteriaPromptBusy = false;
        $("generateCriteria").disabled = false;
        if (!result.ok) {
          $("criteriaPromptStatus").textContent = result.error || "Не удалось создать файл критериев.";
          $("criteriaPromptStatus").classList.add("error");
          setTopState("Ошибка критериев", false);
          return;
        }
        state.criteriaFiles = result.criteria_files || [];
        await loadFiles("", result.created_path || result.criteria?.path || "");
        closeCriteriaPrompt();
        setTopState("Пустой файл создан", true);
        return;
      }
      state.criteriaPromptBusy = true;
      $("generateCriteria").disabled = true;
      $("criteriaPromptStatus").textContent = "LLM формирует criteria.csv...";
      $("criteriaPromptStatus").classList.remove("error");
      setTopState("Создание критериев");
      const result = await postJson("/api/generate-criteria", { text });
      state.criteriaPromptBusy = false;
      $("generateCriteria").disabled = false;
      if (!result.ok) {
        $("criteriaPromptStatus").textContent = result.error || "Не удалось создать файл критериев.";
        $("criteriaPromptStatus").classList.add("error");
        setTopState("Ошибка критериев", false);
        return;
      }
      state.criteriaFiles = result.criteria_files || [];
      await loadFiles("", result.created_path || result.criteria?.path || "");
      closeCriteriaPrompt();
      setTopState("Критерии созданы", true);
    }

    function openFetchCriteriaPrompt() {
      $("fetchCriteriaPromptText").value = "";
      $("fetchCriteriaPromptStatus").textContent = "";
      $("fetchCriteriaPromptStatus").classList.remove("error");
      syncFetchCriteriaPromptButton();
      $("fetchCriteriaPromptModal").classList.remove("hidden");
      $("fetchCriteriaPromptText").focus();
    }

    function closeFetchCriteriaPrompt() {
      if (state.fetchCriteriaPromptBusy) return;
      $("fetchCriteriaPromptModal").classList.add("hidden");
      $("fetchCriteriaPromptStatus").textContent = "";
      $("fetchCriteriaPromptStatus").classList.remove("error");
    }

    function syncCriteriaPromptButton() {
      const text = $("criteriaPromptText").value.trim();
      $("generateCriteria").textContent = text ? "Создать файл критериев" : "Создать пустой файл";
    }

    function syncFetchCriteriaPromptButton() {
      const text = $("fetchCriteriaPromptText").value.trim();
      $("generateFetchCriteria").textContent = text ? "Создать файл фильтров" : "Создать пустой файл";
    }

    async function generateFetchCriteriaFromPrompt() {
      if (state.fetchCriteriaPromptBusy) return;
      const text = $("fetchCriteriaPromptText").value.trim();
      state.fetchCriteriaPromptBusy = true;
      $("generateFetchCriteria").disabled = true;
      $("fetchCriteriaPromptStatus").textContent = text ? "LLM формирует filters.csv..." : "Создаю пустой CSV без LLM.";
      $("fetchCriteriaPromptStatus").classList.remove("error");
      setTopState(text ? "Создание критериев" : "Создание пустого файла");
      const result = await postJson("/api/generate-criteria", { text, kind: "filter" });
      state.fetchCriteriaPromptBusy = false;
      $("generateFetchCriteria").disabled = false;
      if (!result.ok) {
        $("fetchCriteriaPromptStatus").textContent = result.error || "Не удалось создать файл фильтров.";
        $("fetchCriteriaPromptStatus").classList.add("error");
        setTopState("Ошибка критериев", false);
        return;
      }
      state.filterFiles = result.filter_files || [];
      await loadFiles("", "", result.created_path || result.filter?.path || "");
      closeFetchCriteriaPrompt();
      setTopState(text ? "Фильтры созданы" : "Пустой файл создан", true);
    }

    function updateFetchCriteriaUiState() {
      const enabled = $("hardFiltersToggle").checked;
      $("fetchCriteriaField").classList.toggle("hidden", !enabled);
      $("fetchCriteriaLine").classList.toggle("disabled", !enabled);
      $("fetchCriteriaFile").disabled = !enabled;
      $("editFetchCriteriaMeta").disabled = !enabled;
      $("openFetchCriteriaPrompt").disabled = !enabled;
    }

    function selectedMetadataFile(kind) {
      if (kind === "vacancy") return state.files.find((file) => file.path === $("vacanciesFile").value);
      if (kind === "filter") return state.filterFiles.find((file) => file.path === $("fetchCriteriaFile").value);
      return state.criteriaFiles.find((file) => file.path === $("criteriaFile").value);
    }

    async function saveMetadata() {
      const kind = state.metadataKind;
      if (!kind) return;
      const isVacancy = kind === "vacancy";
      const isFilter = kind === "filter";
      const path = isVacancy ? $("vacanciesFile").value : (isFilter ? $("fetchCriteriaFile").value : $("criteriaFile").value);
      const payload = {
        path,
        name: $("metadataName").value,
        description: $("metadataDescription").value
      };
      if (!isVacancy) payload.csv_content = buildCriteriaCsv();
      const result = await postJson("/api/metadata", payload);
      if (!result.ok) {
        $("metadataStatus").textContent = result.error || "Не удалось сохранить.";
        $("metadataStatus").classList.add("error");
        setTopState("Ошибка метаданных", false);
        return;
      }
      state.files = result.files || [];
      state.criteriaFiles = result.criteria_files || [];
      state.filterFiles = result.filter_files || [];
      const vacancyPath = $("vacanciesFile").value;
      const rankVacancyPath = $("rankVacanciesFile").value;
      const criteriaPath = $("criteriaFile").value;
      const filterPath = $("fetchCriteriaFile").value;
      await loadFiles();
      $("vacanciesFile").value = isVacancy ? path : vacancyPath;
      $("rankVacanciesFile").value = rankVacancyPath;
      $("criteriaFile").value = !isVacancy && !isFilter ? path : criteriaPath;
      $("fetchCriteriaFile").value = isFilter ? path : filterPath;
      updateFileMeta();
      closeMetadataModal();
      setTopState("Метаданные сохранены", true);
    }

    async function deleteMetadataFile() {
      const kind = state.metadataKind;
      if (!kind) return;
      const file = selectedMetadataFile(kind);
      if (!file) return;
      const confirmed = window.confirm(`Удалить файл "${file.filename || file.name}"? Это действие нельзя отменить.`);
      if (!confirmed) return;

      const result = await postJson("/api/delete-file", { path: file.path });
      if (!result.ok) {
        setTopState("Ошибка удаления", false);
        return;
      }

      closeMetadataModal();
      await loadFiles();
      updateFileMeta();
      setTopState("Файл удалён", true);
    }

    renderNotifications();
    loadFiles();
    renderFavorites();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
