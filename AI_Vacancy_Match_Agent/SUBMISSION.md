# Сдача проекта

Кому отправить: Telegram `@NiksFok`.

## Что приложить

- Репозиторий или архив проекта: `AI_Vacancy_Match_Agent_submission.zip`.
- Основные файлы проекта находятся в `AI_Vacancy_Match_Agent/`.
- Итоговый пример результата: `output/report.md`.
- Краткий лог запуска: `output/run.log`.
- Структурированный trace: `output/trace.json`.

## Инструкция запуска

```bash
cd AI_Vacancy_Match_Agent
python3 src/main.py --dry-run
python3 -m unittest discover -s tests
```

Опциональный LLM-режим:

```bash
OPENAI_API_KEY=... python3 src/main.py
```

## Пример входных данных

Файл `criteria.csv`:

```csv
candidate_name,target_roles,preferred_levels,preferred_formats,preferred_cities,skills,min_salary,english_level,stop_words
Junior Analytics Candidate,Data Analyst; Product Analyst; Business Analyst; System Analyst; BI Analyst; Marketing Analyst; AI Analyst; LLM App Developer,Internship; Junior; Entry,remote; hybrid,Москва; Удаленно; Санкт-Петербург; Казань,SQL; Python; Excel; Pandas; PowerPoint; BPMN; UML; product metrics; dashboards; A/B tests; hypothesis testing; requirements; analytics,50000,A2+,Senior; Lead; Middle; 3 года; 5 лет; руководитель; team lead
```

Файл `vacancies.csv`, одна строка из примера:

```csv
vacancy_id,title,company,role,level,format,city,published_at,salary_rub,stack,key_skills,english_level,link,description
VAC-001,Стажер Data Analyst,T-Bank,Data Analyst,Internship,hybrid,Москва,2026-05-28,60000-90000,Python|SQL|Pandas|Jupyter|Tableau,SQL|Python|A/B tests|product metrics|dashboards,A2+,https://example.com/vacancies/vac-001,"Команда продуктовой аналитики ищет стажера. Нужно писать SQL-запросы, собирать метрики воронки, помогать с A/B-тестами и готовить дашборды для продуктовых команд."
```

## Пример результата

Файл `output/report.md` содержит топ-5 вакансий и объяснения. Пример первой рекомендации:

```text
1. Стажер Data Analyst
Компания: T-Bank
Score: 110
Приоритет: P1 - высокий
Причины: совпадает роль Data Analyst, подходит уровень Internship, совпали SQL/Python/A-B tests/product metrics/dashboards, подходит hybrid-формат и Москва.
Следующий шаг: адаптировать резюме под совпавшие навыки и отправить отклик первым.
```

## Короткое описание

Агент решает задачу первичного отбора стажировок и junior-вакансий под профиль кандидата, чтобы быстро выделить лучшие варианты для отклика. Pipeline читает `vacancies.csv` или `vacancies.json` и критерии кандидата, валидирует данные, нормализует поля, проверяет дубли и пропуски, считает score, ранжирует вакансии и генерирует Markdown-отчет с trace. Обычная логика используется для загрузки файлов, проверки форматов, дедупликации, фильтров, подсчета score и сортировки, потому что эти шаги должны быть воспроизводимыми. Агентный слой в `src/agent.py` объясняет топ-5: почему вакансия подходит, какие критерии совпали, что смущает, что подтянуть и какой следующий шаг сделать; при наличии API-ключа LLM дополнительно аудирует score, ранжирование и формулировки, не меняя базовые баллы. Ограничения проекта связаны с качеством входных данных, rule-based сопоставлением синонимов и возможными ошибками или недоступностью LLM/API, поэтому спорные вакансии нужно проверять вручную. Сложнее всего было разделить детерминированный scoring и LLM-часть так, чтобы результат оставался объяснимым и работал без ключей. Следующим шагом я бы вынес веса scoring и prompt-шаблоны в конфиг, добавил больше тестов на битые входы и улучшил семантическое сопоставление ролей и навыков.
