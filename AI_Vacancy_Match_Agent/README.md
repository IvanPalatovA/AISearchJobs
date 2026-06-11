# AI Vacancy Match Agent

Мини-проект помогает кандидату выбрать подходящие стажировки и junior-вакансии. На вход подаются вакансии и критерии кандидата, на выходе создаются Markdown-отчет, лог запуска и структурированный trace.

## Быстрая сдача

Готовый текст и чек-лист для отправки в Telegram лежит в `SUBMISSION.md`. В нем есть 2-5 команд запуска, пример входных данных, пример результата и короткое описание проекта на 5-7 предложений.

## Что делает агент

- загружает `vacancies.csv` или `vacancies.json`
- читает критерии из `criteria.csv`, `criteria.json` или `criteria.md`
- валидирует пустые файлы, битые строки, пропущенные поля, дубли и отсутствие ссылок
- нормализует разные названия колонок: `city/location`, `format/work_format`, `stack/key_skills/skills`, `link/url`
- применяет мягкие фильтры по формату и городу, не выбрасывая вакансии из scoring
- считает score по всем валидным недублирующимся вакансиям
- ранжирует вакансии и объясняет топ-5
- опционально подключает LLM-слой для комментариев к score, рангу и объяснениям
- пишет `output/report.md`, `output/run.log`, `output/trace.json`

## Режимы работы

### Default / dry-run mode

Проект работает без `OPENAI_API_KEY`. В этом режиме используется rule-based scoring и локальный fallback в `src/agent.py`. Это основной воспроизводимый режим для защиты:

```bash
python3 src/main.py --dry-run
```

Если ключа нет, запуск без `--dry-run` также перейдет в `dry_run` режим.

### LLM mode

Если в окружении есть `OPENAI_API_KEY` и запуск идет без `--dry-run`, включается LLM-слой:

```bash
OPENAI_API_KEY=... python3 src/main.py
```

Также поддержан OpenAI-compatible вариант через `.env`:

```env
OPENAI_COMPATIBLE_API_KEY=...
OPENAI_COMPATIBLE_BASE_URL=https://polza.ai/api/v1
OPENAI_COMPATIBLE_MODEL=deepseek/deepseek-v4-flash
```

Для Polza.ai важно указывать именно `https://polza.ai/api/v1`. Старый вариант `https://api.polza.ai/v1` не соответствует документации Polza и может приводить к зависаниям/ожиданию генерации.

В режиме `auto` весь `score` считается rule-based. В режиме `auto + LLM` модель выставляет часть score за соответствие целевым ролям и навыкам, а остальные критерии остаются автоматическими; `llm_adjustment`, `llm_comment`, `llm_rank_comment` и объяснения топ-5 пишутся отдельно.

## Как запустить

```bash
cd AI_Vacancy_Match_Agent
python3 src/main.py --dry-run
```

Локальный web-интерфейс:

```bash
python3 src/web_app.py
```

После запуска открыть `http://127.0.0.1:8000`. Если в `.env` задан `TELEGRAM_BOT_TOKEN`, вместе с web-интерфейсом стартует Telegram-бот с пультом `/start` и `/menu`. Если порт занят, приложение выберет ближайший свободный порт.
Во вкладке ранжирования можно выбрать CSV, режим `Dry-run`/`LLM` и числовой `Top-K`. Результат разделен на три вкладки: `Report`, `Trace`, `Запуск`; команда запуска и stdout/stderr лежат в `Запуск`, а не смешиваются с отчетом.

В CI/CD используется единый systemd-сервис `superjobsearch` из `deploy/superjobsearch.service`. Он запускает `src/web_app.py`, поэтому web-интерфейс и Telegram-бот поднимаются одним процессом. В production unit задан `REQUIRE_TELEGRAM_BOT=1`: если `TELEGRAM_BOT_TOKEN` отсутствует или не проходит проверку Bot API, сервис не стартует как “успешный”.

Короткий запуск без аргументов тоже работает. Если ключ есть, это будет LLM mode; если ключа нет, это будет dry-run fallback:

```bash
python3 src/main.py
```

Можно явно указать входы и папку результата:

```bash
python3 src/main.py --vacancies vacancies.csv --criteria criteria.csv --output output --dry-run
```

Количество лучших вакансий в отчете задается через `--top-k`:

```bash
python3 src/main.py --vacancies vacancies.csv --criteria criteria.csv --output output --top-k 3 --dry-run
```

Проверка без внешних зависимостей:

```bash
python3 -m unittest discover -s tests
```

Проект не требует LLM/API-ключа для базового запуска. Если в корне есть `.env`, он аккуратно читается как источник переменных окружения, но секреты не хардкодятся.

## Входные файлы

- `vacancies.csv` - база вакансий, сейчас 50 строк.
- `criteria.csv` - профиль кандидата: роли, уровни, форматы, города, навыки, минимальная зарплата, английский, стоп-слова.

Также поддержаны `vacancies.json`, `criteria.json` и простой `criteria.md` с парами `ключ: значение`.

## Pipeline

```text
vacancies.csv + criteria.csv
        ↓
1. Load Data
        ↓
2. Validate Data
        ↓
3. Normalize Data
        ↓
4. Apply Filters
        ↓
5. Calculate Score
   - rule-based score
   - optional LLM score comment / llm_adjustment
        ↓
6. Rank Vacancies
   - base score order
   - optional LLM rank comment
        ↓
7. Agent Explanation
   - local fallback or LLM-enhanced top-5 explanation
        ↓
8. Generate Outputs
        ↓
report.md + run.log + trace.json
```

## Scoring

| Критерий | Баллы |
|---|---:|
| Совпадает роль | +30 в auto; LLM-score в auto + LLM |
| Подходит уровень Internship / Junior / Entry | +20 |
| Совпадают навыки | до +25 в auto; LLM-score в auto + LLM |
| Подходит формат работы | +10 |
| Подходит город / удаленка | +10 |
| Зарплата не ниже минимума | +5 |
| Зарплата не указана при включенном штрафе | до -16 |
| Английский подходит | +5 |
| Вакансия свежая | +5 |
| Senior / Lead / Middle | -40 |
| Нет ссылки | -5 |
| Нерелевантная роль / вне целевых ролей | -80 |

Подробный `score_breakdown` по каждой вакансии сохраняется в `output/trace.json`. В LLM mode модель меняет только часть score за целевые роли и навыки; остальные LLM-предложения пишутся отдельно.

## Где смотреть результат

- `output/report.md` - итоговый отчет с целью, входами, качеством данных, scoring, топ-5, рисками, trace и limitations
- `output/run.log` - краткий лог запуска
- `output/trace.json` - структурированная трассировка валидации, score, LLM-режима и агентных объяснений

## Где обычная логика

- `src/data_loader.py` - чтение CSV/JSON/MD
- `src/validator.py` - проверки качества данных
- `src/main.py` - pipeline, нормализация и мягкие фильтры
- `src/web_app.py` - локальный web-интерфейс для ранжирования и сбора вакансий
- `src/scorer.py` - расчет score, ranking и опциональные LLM-комментарии к этим стадиям
- `src/llm_client.py` - безопасный OpenAI/OpenAI-compatible клиент с fallback
- `src/sources/llm_extract_pipeline.py` - URL HTML fetch, очистка, LLM extraction, нормализация и безопасная CSV-запись для прямых ссылок
- `src/reporter.py` - генерация файлов результата
- `tests/test_pipeline.py` - проверочные примеры для загрузки, валидации и scoring

## Где агентность

- `src/agent.py` объясняет топ-5: почему вакансия подходит, какие критерии совпали, что смущает, что подтянуть кандидату, какой следующий шаг и какой приоритет отклика.
- Агентный слой не просто пересказывает описание: он использует `score_breakdown`, совпавшие навыки, риски, недостающие навыки и извлеченные требования из полей вакансии.
- В LLM mode агентный слой уточняет `why_fit`, риски, `next_step` и вопросы работодателю, но не меняет score.
- В LLM mode поле `LLM-комментарий` формируется как подробное обоснование решения, а поля `Риски` / `Что смущает` заполняются LLM-рисками с fallback на rule-based concerns.
- В URL-режиме сбора вакансий LLM извлекает структуру вакансии из очищенного HTML: роль, компанию, город, формат, уровень, зарплату, навыки, требования и условия.
- Причины низкого ранга также попадают в отчет через раздел рисков и trace.

## Ограничения

- Сопоставление семантики rule-based, поэтому сложные синонимы и спорные роли стоит проверять вручную.
- LLM вызывается опционально при наличии `OPENAI_API_KEY` или OpenAI-compatible ключа и запуске без `--dry-run`.
- Качество ranking зависит от заполненности колонок вакансий.
- Mock-ссылки `example.com` используются как безопасные демонстрационные источники.

## Что улучшить дальше

- добавить unit-тесты на пустые файлы, дубли, битые CSV-строки и крайние случаи scoring
- вынести веса scoring в отдельный конфиг
- вынести LLM-prompts в отдельные шаблоны
- добавить CLI-вопросы для создания критериев кандидата без файла

## Модуль сбора вакансий из внешних источников

В проект добавлен отдельный модуль `src/fetch_vacancies.py`. Он нужен для легального получения вакансий из внешних источников и создания нового CSV-файла без перезаписи текущего `vacancies.csv`.

Поддержанные источники:

- `hh` - официальный API HeadHunter + HTML fallback страницы поиска;
- `superjob` - официальный API SuperJob + HTML fallback страницы поиска, для API можно передать ключ через `SUPERJOB_API_KEY`;
- текущий `vacancies.csv` остается воспроизводимым источником для основного agentic pipeline.

Архитектура источников:

```text
External sources
HH API / SuperJob API
        ↓
HTML fallback if API is unavailable or returns 403
        ↓
LLM HTML fallback if ordinary parser found no cards
        ↓
src/sources/hh_source.py
src/sources/superjob_source.py
src/sources/html_source.py
        ↓
Unified Vacancy Schema
        ↓
new vacancies_YYYYMMDD_HHMMSS.csv
        ↓
python3 src/main.py --vacancies data/collected/<new_file>.csv --criteria criteria.csv --dry-run
```

Для прямых URL вакансий есть отдельный устойчивый путь:

```text
URL list
        ↓
HTMLFetcher
        ↓
HTMLCleaner
        ↓
LLMVacancyExtractor
        ↓
VacancyNormalizer
        ↓
VacancyCSVWriter
        ↓
new vacancies_YYYYMMDD_HHMMSS.csv + trace.json
```

Этот режим нужен для сайтов с разной HTML-структурой: CSS/regex-парсинг используется только как первичный поиск, а извлечение сущностей из страницы вакансии делает LLM. Если LLM отключен, URL-режим не падает, но пишет предупреждение и сохраняет пустой CSV/trace.

Команды запуска:

```bash
# Получить до 50 вакансий с HH по запросу и сохранить новый CSV
python3 src/fetch_vacancies.py --sources hh --text "junior data analyst" --pages 1 --per-page 20 --max-vacancies 50

# Получить до 50 вакансий из HH и SuperJob
python3 src/fetch_vacancies.py --sources hh,superjob --text "junior analyst" --pages 1 --per-page 20 --max-vacancies 50

# Проверить только обычный HTML/API-парсинг без LLM fallback
python3 src/fetch_vacancies.py --sources hh,superjob --text "junior analyst" --pages 1 --per-page 20 --max-vacancies 50 --no-llm-html

# Извлечь вакансии из прямых URL через LLM
python3 src/fetch_vacancies.py --url "https://hh.ru/vacancy/123456" --max-vacancies 1

# Извлечь вакансии из списка URL, один URL на строку
python3 src/fetch_vacancies.py --urls-file data/urls.txt --max-vacancies 50 --delay 1.0

# Запустить основной агент на новом файле
python3 src/main.py --vacancies data/collected/vacancies.csv --criteria criteria.csv --dry-run
```

Настройки:

- `--max-vacancies 50` - верхний лимит сохраняемых вакансий, код дополнительно ограничивает значение 50.
- `--url` - прямой URL вакансии; можно передавать несколько раз.
- `--urls-file` - файл со списком URL, один URL на строку.
- `--delay` - задержка между запросами прямых URL, по умолчанию 0.7 секунды.
- `--no-html` - выключить HTML fallback и использовать только API.
- `--no-llm-html` - выключить LLM fallback для HTML-страниц.
- `HH_USER_AGENT` - рекомендуется задать для HH API по формату `AppName/1.0 (email@example.com)`.
- `SUPERJOB_API_KEY` - нужен для официального API SuperJob; без него модуль попробует HTML fallback.

В web-интерфейсе статус `HTML fallback` означает частичный успех: официальный API источника вернул ошибку, например 403, но HTML-запрос был успешен и вакансии сохранены. Это не ошибка парсинга, а рабочий fallback-режим.

Почему файл не перезаписывается:

- модуль по умолчанию создает `data/collected/vacancies.csv`;
- старый `vacancies.csv` в корне проекта не изменяется;
- если `data/collected/vacancies.csv` уже существует, создается безопасное имя `vacancies_YYYYMMDD_HHMMSS.csv`;
- рядом создается `*.trace.json` с информацией об источниках, количестве строк и предупреждениях.

Единая схема вакансии после парсинга:

```text
vacancy_id, source, title, company, role, level, format, city,
relocation_possible, published_at, deadline, salary_rub, stack,
key_skills, english_level, link, description
```

Ограничение: модуль не обходит капчи, авторизацию и защитные механизмы сайтов. Для MVP используются официальные API, HTML fallback и прямой URL→LLM extraction для страниц вакансий.
