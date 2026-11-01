# Практична робота №11 — Спостережуваність і контроль ресурсів AI-компонента

Реалізація на FastAPI з локальним mock-провайдером і OpenAI-сумісним адаптером.

## Швидкий запуск

```bash
cd pr11
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

У комплекті вже є `.env` для демонстрації з `LLM_PROVIDER=mock`. Для власного середовища можна скопіювати `.env.example` у `.env` і змінити параметри. Не додавайте `.env` до Git.

```bash
python check_env.py
uvicorn app.main:app --reload
```

Відкрити http://127.0.0.1:8000. Сторінка містить вісім демонстраційних звернень, створення чернетки, повтор із `fresh`, скасування, стан бюджету й кешу.

## Структура

- `app/telemetry.py` — JSONL-журнал і контекст request ID.
- `app/costs.py` — розрахунок вартості за usage або оцінкою.
- `app/cache.py` — точний кеш SHA-256 з TTL і JSON-файлом.
- `app/budget.py` — денний бюджет, резервування та облік.
- `app/gateway.py` — єдиний шлюз: провайдер, спроби, fallback, кеш і бюджет.
- `app/providers/` — mock і OpenAI-сумісний провайдер.
- `app/jobs.py` — фонові чернетки та SSE-події.
- `app/drafting.py` — prompt, версія prompt і перевірка результату.
- `eval/analyze.py` — аналіз JSONL журналу.
- `eval/findings.md` — результати локального тесту, обмеження й відповіді на контрольні запитання.

## Аналіз журналу

```bash
python eval/analyze.py
python eval/analyze.py logs/app.jsonl --section slow
python eval/analyze.py logs/app.jsonl --since 2026-10-30 --section spend
```

## Налаштування провайдера

Для реального OpenAI-сумісного API в `.env` вкажіть `LLM_PROVIDER=openai`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. Установіть залежності командою `pip install -r requirements.txt`. Ключ не записується до журналу й не має потрапляти в репозиторій.

## Демонстрація помилок

У `.env` можна тимчасово задати `MOCK_FAILURE_KIND=unavailable` та `MOCK_FAILURE_RATE=1`. Для штатного режиму поверніть `MOCK_FAILURE_RATE=0`. Після зміни `.env` перезапустіть сервер.

## Дані й приватність

`logs/` і `state/` виключені з Git. У журналі не записуються текст звернення, факти, повний prompt чи API-ключ. Зразки `samples/requests.json` — навчальні дані з архіву завдання.

## Відомі обмеження реалізації

- Стан кешу й бюджету переживає перезапуск; оперативні лічильники hit/miss — ні.
- Single-flight блокування об'єднує одночасні точні промахи з однаковим ключем; `fresh=true` свідомо обходить кеш.
- Реальна перевірка usage потребує API-ключа та успішного доступу до провайдера.
- У потоковому режимі таймаути провайдера контролюються SDK та загальним обмеженням; окремий watchdog бездіяльності для довільного синхронного ітератора не реалізований.
