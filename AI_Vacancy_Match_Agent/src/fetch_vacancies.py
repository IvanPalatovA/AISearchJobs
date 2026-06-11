from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sources.base import FetchResult, deduplicate
from sources.generic_html_source import canonical_html_source, fetch_generic_html_vacancies, supported_html_sources
from sources.hh_source import fetch_hh_vacancies
from sources.html_source import parse_hh_detail_html, parse_superjob_detail_html
from sources.llm_extract_pipeline import HTMLCleaner, HTMLFetcher, LLMVacancyExtractor, VacancyCSVWriter, VacancyNormalizer
from sources.quality import prepare_vacancies_for_output
from sources.superjob_source import fetch_superjob_vacancies
from llm_client import LLMClient


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _load_env_file(PROJECT_ROOT / ".env")
    hard_filters = _load_criteria(_resolve_path(args.criteria)) if args.criteria and not args.skip_criteria_filters else {}
    setattr(args, "hard_filters", hard_filters)
    output_dir = _resolve_path(args.output_dir)
    all_vacancies: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_stats: dict[str, dict[str, Any]] = {}
    request_log: list[dict[str, Any]] = []
    llm_client = LLMClient.from_env(dry_run=args.no_llm_html)
    max_limit = max(1, min(args.llm_max_cap, 300)) if args.llm_only_html else 300
    max_vacancies = max(1, min(args.max_vacancies, max_limit))
    if args.llm_only_html and not llm_client.enabled:
        warnings.append("Mixed automatic + LLM parsing is enabled but LLM client is disabled; automatic HTML parsing will still run.")
    url_inputs, input_warnings = _collect_url_inputs(args)
    warnings.extend(input_warnings)

    if url_inputs:
        url_inputs = _deduplicate_urls(url_inputs)
        if len(url_inputs) > max_vacancies:
            warnings.append(f"URL list is limited to first {max_vacancies} entries.")
        url_inputs = url_inputs[:max_vacancies]
        total_steps = len(url_inputs) + 3
        _safe_print(f"Fetch mode: url_html_llm={llm_client.mode}, urls={len(url_inputs)}")
        url_result = _fetch_url_inputs(url_inputs, args, llm_client=llm_client, total_steps=total_steps)
        all_vacancies.extend(url_result.vacancies)
        warnings.extend(url_result.warnings)
        request_log.extend(url_result.request_log)
        source_stats = _build_source_stats(url_result)
        sources_requested = ["url-list"]
        queries = [args.text]
        query_limits = {args.text: max_vacancies}
        source_limits_by_query: dict[str, dict[str, int]] = {}
    else:
        sources = [source.strip().lower() for source in args.sources.split(",") if source.strip()]
        queries = _parse_queries(args.queries, args.text)
        query_limits = _allocate_query_limits(queries, max_vacancies)
        source_priorities = _parse_source_priorities(args.source_priorities)
        source_limits_by_query = {
            query: _allocate_source_limits(sources, source_priorities, query_limit)
            for query, query_limit in query_limits.items()
        }
        total_steps = len(queries) * len(sources) + 3
        _safe_print(f"Fetch mode: html={'off' if args.no_html else 'on'}, llm_html={llm_client.mode}, mixed_html={'on' if args.llm_only_html else 'off'}")

        if args.llm_only_html:
            parallel_result = _fetch_llm_only_sources_parallel(
                args,
                queries=queries,
                sources=sources,
                source_limits_by_query=source_limits_by_query,
                source_priorities=source_priorities,
                llm_client=llm_client,
                total_steps=total_steps,
            )
            all_vacancies.extend(parallel_result.vacancies)
            warnings.extend(parallel_result.warnings)
            request_log.extend(parallel_result.request_log)
            for stat in parallel_result.source_stats:
                _merge_source_stat(source_stats, **stat)
        else:
            step = 0
            for query in queries:
                for source in sources:
                    step += 1
                    source_key = _canonical_source_key(source)
                    source_limit = source_limits_by_query.get(query, {}).get(source_key, 0)
                    if source_limit <= 0:
                        _merge_source_stat(source_stats, source_key, query=query, limit=0)
                        continue
                    per_page, pages = _source_fetch_window(source_limit, args.per_page, args.pages)
                    _progress(f"Fetch {source} / {query}", step, total_steps)
                    result = _fetch_source(source, args, text=query, llm_client=llm_client, max_items=source_limit, per_page=per_page, pages=pages)
                    all_vacancies.extend(result.vacancies)
                    warnings.extend(result.warnings)
                    request_log.extend(result.request_log)
                    _merge_source_stat(
                        source_stats,
                        result.source,
                        query=query,
                        limit=source_limit,
                        per_page=per_page,
                        pages=pages,
                        priority=source_priorities.get(source_key, "medium"),
                        vacancies=len(result.vacancies),
                        requests=len(result.request_log),
                        successful_requests=sum(1 for item in result.request_log if item.get("ok")),
                    )
                    if len(deduplicate(all_vacancies)) >= max_vacancies:
                        warnings.append(f"Max vacancies limit reached: {max_vacancies}")
                        break
                if len(deduplicate(all_vacancies)) >= max_vacancies:
                    break
        if len(deduplicate(all_vacancies)) < max_vacancies:
            refill_result = _refill_vacancies(
                args,
                queries=queries,
                sources=sources,
                source_stats=source_stats,
                source_priorities=source_priorities,
                llm_client=llm_client,
                existing_vacancies=all_vacancies,
                max_vacancies=max_vacancies,
            )
            all_vacancies.extend(refill_result.vacancies)
            warnings.extend(refill_result.warnings)
            request_log.extend(refill_result.request_log)
        sources_requested = sources

    collection_steps = total_steps - 3
    _progress("Deduplicate", collection_steps + 1, total_steps)
    quality_vacancies, quality_report = prepare_vacancies_for_output(all_vacancies)
    if quality_report.get("dropped_rows"):
        warnings.append(f"Quality gate dropped noisy/duplicate rows: {quality_report['dropped_rows']}")
    unique_vacancies = quality_vacancies[:max_vacancies]
    if hard_filters:
        unique_vacancies = [vacancy for vacancy in unique_vacancies if _vacancy_matches_criteria(vacancy, hard_filters)]

    _progress("Write CSV", collection_steps + 2, total_steps)
    output_path = Path(VacancyCSVWriter().write(unique_vacancies, output_dir=str(output_dir), filename=args.filename))

    _progress("Write trace", total_steps, total_steps)
    quality_path = output_path.with_suffix(".quality.json")
    quality_path.write_text(json.dumps(quality_report, ensure_ascii=False, indent=2), encoding="utf-8")
    trace = {
        "query": args.text,
        "queries": queries,
        "query_limits": query_limits,
        "max_vacancies": max_vacancies,
        "sources_requested": sources_requested,
        "source_priorities": source_priorities if not url_inputs else {},
        "source_limits": source_limits_by_query if not url_inputs else {},
        "hard_filters": hard_filters,
        "url_inputs": url_inputs,
        "source_stats": source_stats,
        "fetch_metrics": _fetch_parse_metrics(all_vacancies),
        "raw_rows": len(all_vacancies),
        "quality_rows": len(quality_vacancies),
        "unique_rows": len(unique_vacancies),
        "quality_report": quality_report,
        "quality_report_file": str(quality_path),
        "output_file": str(output_path),
        "request_log": request_log,
        "llm_html_mode": llm_client.mode,
        "llm_trace": llm_client.call_trace,
        "warnings": warnings,
        "note": "Existing vacancies.csv is not overwritten. A timestamped file is created instead.",
    }
    trace_path = output_path.with_suffix(".trace.json")
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")

    _safe_print(f"Fetched raw vacancies: {len(all_vacancies)}")
    _safe_print(f"Quality-kept vacancies: {len(quality_vacancies)}")
    _safe_print(f"Unique vacancies: {len(unique_vacancies)}")
    _safe_print(f"Created: {_display_path(output_path)}")
    _safe_print(f"Quality: {_display_path(quality_path)}")
    _safe_print(f"Trace: {_display_path(trace_path)}")
    if request_log:
        _safe_print("Requests:")
        for item in request_log[:10]:
            _safe_print(f"- {item.get('source')} {item.get('method')} status={item.get('status')} ok={item.get('ok')}")
    if warnings:
        _safe_print("Warnings:")
        for warning in warnings[:10]:
            _safe_print(f"- {warning}")
    return 0


def _fetch_llm_only_sources_parallel(
    args: argparse.Namespace,
    *,
    queries: list[str],
    sources: list[str],
    source_limits_by_query: dict[str, dict[str, int]],
    source_priorities: dict[str, str],
    llm_client: LLMClient,
    total_steps: int,
) -> argparse.Namespace:
    tasks: list[dict[str, Any]] = []
    step = 0
    stats: list[dict[str, Any]] = []
    for query in queries:
        for source in sources:
            step += 1
            source_key = _canonical_source_key(source)
            source_limit = source_limits_by_query.get(query, {}).get(source_key, 0)
            if source_limit <= 0:
                stats.append({"source": source_key, "query": query, "limit": 0})
                continue
            per_page, pages = _source_fetch_window(source_limit, args.per_page, args.pages)
            tasks.append(
                {
                    "order": step,
                    "source": source,
                    "source_key": source_key,
                    "query": query,
                    "limit": source_limit,
                    "per_page": per_page,
                    "pages": pages,
                }
            )

    if not tasks:
        return argparse.Namespace(vacancies=[], warnings=[], request_log=[], source_stats=stats)

    def fetch_task(task: dict[str, Any]) -> tuple[dict[str, Any], FetchResult]:
        result = _fetch_source(
            task["source"],
            args,
            text=task["query"],
            llm_client=llm_client,
            max_items=task["limit"],
            per_page=task["per_page"],
            pages=task["pages"],
        )
        return task, result

    completed = 0
    results: list[tuple[dict[str, Any], FetchResult]] = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fetch_task, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                task, result = future.result()
            except Exception as error:  # noqa: BLE001 - one flaky source must not abort the whole collection.
                source_key = task.get("source_key") or _canonical_source_key(task.get("source"))
                result = FetchResult(
                    source=source_key,
                    vacancies=[],
                    warnings=[f"{source_key} fetch failed for query {task.get('query')}: {type(error).__name__}: {error}"],
                    request_log=[
                        {
                            "source": source_key,
                            "method": "parallel-fetch",
                            "query": task.get("query"),
                            "status": type(error).__name__,
                            "ok": False,
                            "error": str(error),
                        }
                    ],
                )
            results.append((task, result))
            completed += 1
            _progress(f"Fetch {task['source']} / {task['query']}", completed, total_steps)

    vacancies: list[dict[str, Any]] = []
    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []
    for task, result in sorted(results, key=lambda item: item[0]["order"]):
        vacancies.extend(result.vacancies)
        warnings.extend(result.warnings)
        request_log.extend(result.request_log)
        stats.append(
            {
                "source": result.source,
                "query": task["query"],
                "limit": task["limit"],
                "per_page": task["per_page"],
                "pages": task["pages"],
                "priority": source_priorities.get(task["source_key"], "medium"),
                "vacancies": len(result.vacancies),
                "requests": len(result.request_log),
                "successful_requests": sum(1 for item in result.request_log if item.get("ok")),
            }
        )

    return argparse.Namespace(vacancies=vacancies, warnings=warnings, request_log=request_log, source_stats=stats)


def _fetch_source(
    source: str,
    args: argparse.Namespace,
    *,
    text: str,
    llm_client: LLMClient,
    max_items: int,
    per_page: int,
    pages: int,
    start_page: int = 0,
) -> FetchResult:
    if source == "hh":
        return fetch_hh_vacancies(
            text=text,
            area=args.hh_area,
            per_page=per_page,
            pages=pages,
            fetch_details=True,
            timeout=args.timeout,
            use_html=not args.no_html,
            llm_client=llm_client,
            llm_only_html=args.llm_only_html,
            allow_llm_fallback=not args.no_llm_html,
            max_items=max_items,
            start_page=start_page,
            hard_filters=getattr(args, "hard_filters", {}),
        )
    if source in {"superjob", "sj"}:
        return fetch_superjob_vacancies(
            text=text,
            town=args.superjob_town,
            count=per_page,
            pages=pages,
            timeout=args.timeout,
            use_html=not args.no_html,
            llm_client=llm_client,
            llm_only_html=args.llm_only_html,
            allow_llm_fallback=not args.no_llm_html,
            max_items=max_items,
            start_page=start_page,
            hard_filters=getattr(args, "hard_filters", {}),
        )
    canonical_source = canonical_html_source(source)
    if canonical_source in supported_html_sources():
        if args.no_html:
            return FetchResult(source=canonical_source, vacancies=[], warnings=[f"{canonical_source} requires HTML parsing and was skipped because --no-html is set."])
        return fetch_generic_html_vacancies(
            source=canonical_source,
            text=text,
            per_page=per_page,
            pages=pages,
            timeout=args.timeout,
            llm_client=llm_client,
                llm_only_html=args.llm_only_html,
                allow_llm_fallback=not args.no_llm_html,
            max_items=max_items,
            start_page=start_page,
        )
    return FetchResult(source=source, vacancies=[], warnings=[f"Unknown source skipped: {source}"])


def _fetch_url_inputs(
    urls: list[str],
    args: argparse.Namespace,
    *,
    llm_client: LLMClient,
    total_steps: int,
) -> FetchResult:
    fetcher = HTMLFetcher(timeout=args.timeout, delay=args.delay)
    cleaner = HTMLCleaner()
    extractor = LLMVacancyExtractor(llm_client)
    normalizer = VacancyNormalizer()
    vacancies: list[dict[str, Any]] = []
    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []

    if getattr(llm_client, "enabled", False) and urls:
        def fetch_parse_url(index: int, url: str) -> tuple[int, dict[str, Any], list[str], dict[str, Any] | None]:
            page = HTMLFetcher(timeout=args.timeout, delay=0).fetch(url)
            log = {
                "source": page.source_site,
                "method": "url-html",
                "url": page.url,
                "status": page.status,
                "ok": page.ok,
            }
            if not page.ok:
                return index, log, [f"{page.url}: {page.error or 'request failed'}"], None

            auto_vacancy = _parse_known_detail_url(page.html, url=page.url, source_site=page.source_site)
            if auto_vacancy:
                return index, log, [], auto_vacancy

            cleaned = cleaner.clean(page.html)
            raw_vacancy, error = extractor.extract(text=cleaned, url=page.url, source_site=page.source_site)
            if error:
                return index, log, [f"{page.url}: {error}"], None
            vacancy = normalizer.normalize(raw_vacancy, source_site=page.source_site, url=page.url) if raw_vacancy else None
            return index, log, [], vacancy

        results = []
        with ThreadPoolExecutor(max_workers=len(urls)) as executor:
            futures = [executor.submit(fetch_parse_url, index, url) for index, url in enumerate(urls, start=1)]
            completed = 0
            for future in as_completed(futures):
                results.append(future.result())
                completed += 1
                _progress(f"Fetch URL {completed}/{len(urls)}", completed, total_steps)

        for _, log, url_warnings, vacancy in sorted(results, key=lambda item: item[0]):
            request_log.append(log)
            warnings.extend(url_warnings)
            if vacancy:
                vacancies.append(vacancy)
        return FetchResult(source="url-list", vacancies=vacancies, warnings=warnings, request_log=request_log)

    for index, url in enumerate(urls, start=1):
        _progress(f"Fetch URL {index}/{len(urls)}", index, total_steps)
        page = fetcher.fetch(url)
        request_log.append(
            {
                "source": page.source_site,
                "method": "url-html",
                "url": page.url,
                "status": page.status,
                "ok": page.ok,
            }
        )
        if not page.ok:
            warnings.append(f"{page.url}: {page.error or 'request failed'}")
            continue

        auto_vacancy = _parse_known_detail_url(page.html, url=page.url, source_site=page.source_site)
        if auto_vacancy:
            vacancies.append(auto_vacancy)
            continue

        cleaned = cleaner.clean(page.html)
        raw_vacancy, error = extractor.extract(text=cleaned, url=page.url, source_site=page.source_site)
        if error:
            warnings.append(f"{page.url}: {error}")
            continue
        if raw_vacancy:
            vacancies.append(normalizer.normalize(raw_vacancy, source_site=page.source_site, url=page.url))

    return FetchResult(source="url-list", vacancies=vacancies, warnings=warnings, request_log=request_log)


def _parse_known_detail_url(html: str, *, url: str, source_site: str) -> dict[str, Any] | None:
    if source_site == "hh-url":
        return parse_hh_detail_html(html, page_url=url)
    if source_site == "superjob-url":
        return parse_superjob_detail_html(html, page_url=url)
    return None


def _build_source_stats(result: FetchResult) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for item in result.request_log:
        source = str(item.get("source") or result.source)
        source_stats = stats.setdefault(source, {"vacancies": 0, "requests": 0, "successful_requests": 0})
        source_stats["requests"] += 1
        if item.get("ok"):
            source_stats["successful_requests"] += 1
    for vacancy in result.vacancies:
        source = str(vacancy.get("source") or result.source)
        source_stats = stats.setdefault(source, {"vacancies": 0, "requests": 0, "successful_requests": 0})
        source_stats["vacancies"] += 1
    return stats


def _refill_vacancies(
    args: argparse.Namespace,
    *,
    queries: list[str],
    sources: list[str],
    source_stats: dict[str, dict[str, Any]],
    source_priorities: dict[str, str],
    llm_client: LLMClient,
    existing_vacancies: list[dict[str, Any]],
    max_vacancies: int,
) -> FetchResult:
    vacancies: list[dict[str, Any]] = []
    warnings: list[str] = []
    request_log: list[dict[str, Any]] = []
    unique_count = len(deduplicate(existing_vacancies))
    if unique_count >= max_vacancies:
        return FetchResult(source="refill", vacancies=vacancies, warnings=warnings, request_log=request_log)

    source_order = sorted(
        [_canonical_source_key(source) for source in sources if _canonical_source_key(source)],
        key=lambda source: (
            source_stats.get(source, {}).get("vacancies", 0) <= 0,
            -_priority_weight(source_priorities.get(source, "medium")),
            source,
        ),
    )
    productive_sources = [source for source in source_order if source_stats.get(source, {}).get("vacancies", 0) > 0]
    if not productive_sources:
        warnings.append("Refill skipped: no source returned vacancies in the initial pass.")
        return FetchResult(source="refill", vacancies=vacancies, warnings=warnings, request_log=request_log)

    rounds_without_growth = 0
    while unique_count < max_vacancies and rounds_without_growth < 1:
        before_round = unique_count
        remaining = max_vacancies - unique_count
        tasks: list[dict[str, Any]] = []
        for query in queries:
            for source in productive_sources:
                query_stats = source_stats.get(source, {}).get("queries", {}).get(query, {})
                start_page = int(query_stats.get("pages") or 0)
                per_page, pages = _source_fetch_window(remaining, args.per_page, args.pages)
                tasks.append(
                    {
                        "source": source,
                        "query": query,
                        "remaining": remaining,
                        "per_page": per_page,
                        "pages": pages,
                        "start_page": start_page,
                    }
                )

        def fetch_refill(task: dict[str, Any]) -> tuple[dict[str, Any], FetchResult]:
            result = _fetch_source(
                task["source"],
                args,
                text=task["query"],
                llm_client=llm_client,
                max_items=task["remaining"],
                per_page=task["per_page"],
                pages=task["pages"],
                start_page=task["start_page"],
            )
            return task, result

        results: list[tuple[dict[str, Any], FetchResult]] = []
        if tasks:
            with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
                futures = [executor.submit(fetch_refill, task) for task in tasks]
                for future in as_completed(futures):
                    results.append(future.result())

        for task, result in results:
            vacancies.extend(result.vacancies)
            warnings.extend(result.warnings)
            request_log.extend(result.request_log)
            _merge_source_stat(
                source_stats,
                result.source,
                query=task["query"],
                limit=task["remaining"],
                per_page=task["per_page"],
                pages=task["pages"],
                priority=source_priorities.get(task["source"], "medium"),
                vacancies=len(result.vacancies),
                requests=len(result.request_log),
                successful_requests=sum(1 for item in result.request_log if item.get("ok")),
                refill=True,
            )
        unique_count = len(deduplicate(existing_vacancies + vacancies))
        if unique_count <= before_round:
            rounds_without_growth += 1
            warnings.append("Refill stopped: additional pages did not add unique vacancies.")

    return FetchResult(source="refill", vacancies=vacancies, warnings=warnings, request_log=request_log)


def _fetch_parse_metrics(vacancies: list[dict[str, Any]]) -> dict[str, int]:
    html_with_llm = 0
    html_without_llm = 0
    html_mixed = 0
    for vacancy in vacancies:
        source = str(vacancy.get("source") or "").lower()
        if source.endswith("-mixed-html"):
            html_mixed += 1
        elif source.endswith("-llm-html"):
            html_with_llm += 1
        elif source.endswith("-html"):
            html_without_llm += 1
    return {
        "total_considered": len(vacancies),
        "html_without_llm": html_without_llm,
        "html_with_llm": html_with_llm,
        "html_mixed": html_mixed,
    }


def _load_criteria(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists() or path.suffix.lower() != ".csv":
        return {}
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
    if not rows:
        return {}
    row = rows[0]
    criteria: dict[str, Any] = {}
    list_fields = {
        "target_roles",
        "preferred_levels",
        "preferred_formats",
        "preferred_cities",
        "skills",
        "stop_words",
        "english_level",
        "search_fields",
        "working_hours",
        "employment_contract",
    }
    bool_fields = {"salary_defined", "income_specified", "accredited_it", "temporary_contract", "accept_temporary"}
    for key, value in row.items():
        normalized_key = str(key or "").strip().lstrip("\ufeff")
        if not normalized_key:
            continue
        if normalized_key == "min_salary":
            criteria[normalized_key] = _parse_int(value)
        elif normalized_key in list_fields:
            criteria[normalized_key] = _split_criteria_values(value)
        elif normalized_key in bool_fields:
            criteria[normalized_key] = _parse_bool(value)
        else:
            criteria[normalized_key] = str(value or "").strip()
    return criteria


def _split_criteria_values(value: Any) -> list[str]:
    return [item.strip().lower() for item in re.split(r"[;|,]", str(value or "")) if item.strip()]


def _parse_int(value: Any) -> int:
    digits = re.findall(r"\d+", str(value or ""))
    return int("".join(digits)) if digits else 0


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower().replace("ё", "е").replace("_", " ")
    return normalized in {"1", "true", "yes", "y", "on", "да", "истина", "указан", "указана", "есть", "with salary"}


def _vacancy_matches_criteria(vacancy: dict[str, Any], criteria: dict[str, Any]) -> bool:
    if not criteria:
        return True
    haystack = " ".join(
        str(vacancy.get(key) or "")
        for key in (
            "title",
            "company",
            "role",
            "level",
            "format",
            "city",
            "stack",
            "key_skills",
            "english_level",
            "description",
            "requirements",
            "responsibilities",
            "conditions",
        )
    ).lower()
    if any(term and term in haystack for term in criteria.get("stop_words", [])):
        return False
    min_salary = int(criteria.get("min_salary") or 0)
    vacancy_salary = _parse_int(vacancy.get("salary_rub"))
    if min_salary and vacancy_salary and vacancy_salary < min_salary:
        return False
    if criteria.get("salary_defined") and not _vacancy_has_salary(vacancy):
        return False
    selected_hours = {str(item).strip().lower() for item in criteria.get("working_hours", []) if str(item).strip()}
    vacancy_hours = str(vacancy.get("working_hours") or "").lower()
    if selected_hours and vacancy_hours:
        vacancy_hour_values = set(re.findall(r"\d+", vacancy_hours)) | ({vacancy_hours.strip()} if vacancy_hours.strip() else set())
        if not selected_hours & vacancy_hour_values:
            return False
    service_filtered_source = _is_service_filtered_source(vacancy)
    for field, keys in (
        ("target_roles", ("role", "title", "description", "requirements", "responsibilities")),
        ("preferred_levels", ("level", "title", "description", "requirements", "conditions")),
        ("preferred_formats", ("format", "work_format", "description", "conditions")),
        ("preferred_cities", ("city", "location", "description", "conditions")),
        ("skills", ("stack", "key_skills", "description", "requirements")),
    ):
        if service_filtered_source and field in {"preferred_levels", "preferred_formats", "preferred_cities"}:
            continue
        values = criteria.get(field, [])
        if values and not any(any(term in str(vacancy.get(key) or "").lower() for key in keys) for term in values):
            return False
    english_levels = criteria.get("english_level") or []
    if english_levels and not any(level in haystack for level in english_levels):
        return False
    return True


def _is_service_filtered_source(vacancy: dict[str, Any]) -> bool:
    source = str(vacancy.get("source") or "").lower()
    return source.startswith("hh") or source.startswith("superjob")


def _vacancy_has_salary(vacancy: dict[str, Any]) -> bool:
    salary_text = " ".join(
        str(vacancy.get(key) or "")
        for key in ("salary_rub", "salary_text", "salary", "compensation", "pay")
    )
    if _parse_int(salary_text):
        return True
    nearby_text = " ".join(
        str(vacancy.get(key) or "")
        for key in ("description", "conditions", "raw_detail_text")
    )[:1200]
    return bool(re.search(r"\d[\d\s]{2,}\s*(?:₽|руб|rur|rub|\$|eur|usd)", nearby_text, flags=re.IGNORECASE))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch vacancies from legal external sources and save a new timestamped CSV without overwriting the existing vacancies.csv."
    )
    parser.add_argument(
        "--sources",
        default="hh",
        help="Comma-separated sources: hh,superjob,rabota_ru,avito,zarplata,gorodrabot,jooble,habr,geekjob,trudvsem",
    )
    parser.add_argument("--source-priorities", default="", help="Comma-separated source priorities, for example hh:high,jooble:low. Weights are high=4, medium=2, low=1")
    parser.add_argument("--text", default="junior data analyst", help="Search query")
    parser.add_argument("--queries", default="", help="JSON list of search keywords. Limits are split evenly between keywords")
    parser.add_argument("--url", action="append", default=[], help="Direct vacancy URL. Can be passed multiple times or as comma/space separated values")
    parser.add_argument("--urls-file", default="", help="Text file with one vacancy URL per line. URL mode requires LLM unless --no-llm-html is used for a dry trace")
    parser.add_argument("--pages", type=int, default=0, help="Override number of pages per source. Default 0 means auto")
    parser.add_argument("--per-page", type=int, default=0, help="Override vacancies per page. Default 0 means auto")
    parser.add_argument("--max-vacancies", type=int, default=300, help="Maximum vacancies to save, capped at 300")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")
    parser.add_argument("--delay", type=float, default=0.7, help="Delay between direct URL requests in seconds")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "collected"), help="Directory for generated CSV")
    parser.add_argument("--filename", default="vacancies.csv", help="Preferred output filename. If it exists, a timestamped name is used automatically")
    parser.add_argument("--hh-area", default="1", help="HH area id. 1 is Moscow. Empty string disables area filter")
    parser.add_argument("--superjob-town", default="4", help="SuperJob town id. 4 is Moscow. Empty string disables town filter")
    parser.add_argument("--no-html", action="store_true", help="Disable HTML fallback parsing")
    parser.add_argument("--no-llm-html", action="store_true", help="Disable LLM parsing for HTML pages (auto parsing only)")
    parser.add_argument("--llm-only-html", action="store_true", help="Use mixed automatic + LLM parsing for HTML pages")
    parser.add_argument("--llm-max-cap", type=int, default=50, help=argparse.SUPPRESS)
    parser.add_argument("--criteria", default="", help="Path to criteria.csv for hard filtering")
    parser.add_argument("--skip-criteria-filters", action="store_true", help="Disable hard filtering by criteria")
    return parser.parse_args(argv)


def _collect_url_inputs(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    warnings: list[str] = []
    for value in args.url or []:
        urls.extend(_split_url_value(value))

    if args.urls_file:
        path = _resolve_path(args.urls_file)
        if not path.exists():
            warnings.append(f"URLs file does not exist: {path}")
        else:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                cleaned = line.strip()
                if not cleaned or cleaned.startswith("#"):
                    continue
                urls.extend(_split_url_value(cleaned))
    return urls, warnings


def _split_url_value(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\s,]+", str(value or "")) if part.strip()]


def _deduplicate_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        normalized = url.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _parse_queries(value: str, fallback: str) -> list[str]:
    raw_items: list[Any] = []
    if value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        if isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = [part for part in re.split(r"[,;\n]+", str(parsed)) if part.strip()]
    if not raw_items:
        raw_items = [fallback]

    queries: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        query = " ".join(str(item or "").split())[:120]
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            queries.append(query)
    return queries or ["junior data analyst"]


def _allocate_query_limits(queries: list[str], max_vacancies: int) -> dict[str, int]:
    queries = [query for query in queries if query]
    if not queries:
        return {}
    max_vacancies = max(1, min(max_vacancies, 300))
    limits = {query: 0 for query in queries}
    remaining = max_vacancies
    if max_vacancies >= len(queries):
        limits = {query: 1 for query in queries}
        remaining -= len(queries)
    if remaining <= 0:
        return limits
    base, extra = divmod(remaining, len(queries))
    for index, query in enumerate(queries):
        limits[query] += base + (1 if index < extra else 0)
    return limits


def _parse_source_priorities(value: str) -> dict[str, str]:
    priorities: dict[str, str] = {}
    for item in str(value or "").split(","):
        if ":" not in item:
            continue
        source, priority = item.split(":", 1)
        source_key = _canonical_source_key(source)
        priority_value = priority.strip().lower()
        if source_key and priority_value in {"high", "medium", "low"}:
            priorities[source_key] = priority_value
    return priorities


def _merge_source_stat(
    stats: dict[str, dict[str, Any]],
    source: str,
    *,
    query: str,
    limit: int,
    per_page: int = 0,
    pages: int = 0,
    priority: str = "medium",
    vacancies: int = 0,
    requests: int = 0,
    successful_requests: int = 0,
    refill: bool = False,
) -> None:
    item = stats.setdefault(
        source,
        {
            "vacancies": 0,
            "requests": 0,
            "successful_requests": 0,
            "limit": 0,
            "priority": priority,
            "queries": {},
        },
    )
    item["vacancies"] += vacancies
    item["requests"] += requests
    item["successful_requests"] += successful_requests
    item["limit"] += limit
    item["priority"] = priority
    query_item = item["queries"].setdefault(
        query,
        {
            "vacancies": 0,
            "requests": 0,
            "successful_requests": 0,
            "limit": 0,
            "per_page": per_page,
            "pages": 0,
            "refill_requests": 0,
        },
    )
    query_item["vacancies"] += vacancies
    query_item["requests"] += requests
    query_item["successful_requests"] += successful_requests
    query_item["limit"] += limit
    query_item["per_page"] = per_page
    query_item["pages"] += pages
    if refill:
        query_item["refill_requests"] += requests


def _allocate_source_limits(sources: list[str], priorities: dict[str, str], max_vacancies: int) -> dict[str, int]:
    source_keys = [_canonical_source_key(source) for source in sources if _canonical_source_key(source)]
    if not source_keys:
        return {}
    max_vacancies = max(1, min(max_vacancies, 300))
    weights = {source: _priority_weight(priorities.get(source, "medium")) for source in source_keys}
    limits = {source: 0 for source in source_keys}
    remaining = max_vacancies
    if max_vacancies >= len(source_keys):
        limits = {source: 1 for source in source_keys}
        remaining -= len(source_keys)
    if remaining <= 0:
        return limits

    total_weight = sum(weights.values()) or len(source_keys)
    shares: list[tuple[str, float, int]] = []
    assigned = 0
    for source in source_keys:
        raw_share = remaining * weights[source] / total_weight
        base = int(raw_share)
        limits[source] += base
        assigned += base
        shares.append((source, raw_share - base, weights[source]))

    leftover = remaining - assigned
    for source, _, _ in sorted(shares, key=lambda item: (item[1], item[2]), reverse=True)[:leftover]:
        limits[source] += 1
    return limits


def _source_fetch_window(source_limit: int, per_page_override: int = 0, pages_override: int = 0) -> tuple[int, int]:
    source_limit = max(1, min(int(source_limit), 300))
    per_page = int(per_page_override or 0)
    pages = int(pages_override or 0)
    if per_page <= 0:
        per_page = min(20, source_limit)
    else:
        per_page = max(1, min(per_page, 50))
    if pages <= 0:
        pages = max(5, math.ceil(source_limit / per_page))
    else:
        pages = max(1, min(pages, 20))
    return per_page, pages


def _canonical_source_key(source: str) -> str:
    normalized = str(source or "").strip().lower()
    if normalized == "sj":
        return "superjob"
    return canonical_html_source(normalized)


def _priority_weight(priority: str) -> int:
    return {"high": 4, "medium": 2, "low": 1}.get(str(priority or "").lower(), 2)


def _resolve_path(value: str | None) -> Path:
    path = Path(value or ".")
    return path if path.is_absolute() else PROJECT_ROOT / path


def _progress(stage: str, completed: int, total: int) -> None:
    total = max(total, 1)
    percent = int((completed / total) * 100)
    width = 24
    filled = int(width * completed / total)
    bar = "#" * filled + "-" * (width - filled)
    _safe_print(f"[{bar}] {percent:3d}% {stage}")


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_print(value: Any = "") -> None:
    text = str(value)
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write((text + "\n").encode(encoding, errors="backslashreplace"))
        sys.stdout.flush()


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


if __name__ == "__main__":
    raise SystemExit(main())
