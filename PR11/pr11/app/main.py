"""Веб-рівень застосунку: сторінка помічника оператора і JSON-ендпоінти.

Відносно ПР10 тут додано:

* проміжний шар `request_context`: кожному HTTP-запиту — ідентифікатор
  у `telemetry.request_id_var`, у відповіді — заголовок `X-Request-ID`,
  у журналі — запис `http.request` з методом, шляхом, статусом і
  тривалістю. Ідентифікатор, який клієнт прислав у тому самому
  заголовку, береться, якщо він схожий на ідентифікатор;
* `fresh` у замовленні чернетки — обійти кеш (кнопка «Повторити»);
* `GET /api/usage` — стан бюджету й кешу для сторінки;
* `/api/health` віддає ще й версію prompt, налаштування кешу й ліміту.

Обробку помилок, як і в ПР10, тут не зроблено: перенесіть свої
обробники з `pr10/app/main.py`. У тілі помилки має бути `request_id` —
візьміть його з `telemetry.request_id_var.get()`. Необроблений виняток
(500) проходить повз проміжний шар — така відповідь не має ані
заголовка `X-Request-ID`, ані ідентифікатора в тілі: ще одна причина
не лишати збоїв необробленими. Якщо вирішите
перевіряти бюджет ще й тут, до запуску фонової роботи, а не лише в
шлюзі, — додайте обробник `budget.BudgetExceeded` і вирішіть, який
HTTP-статус і заголовки в такої відмови.

Запуск із папки pr11:

    uvicorn app.main:app --reload

Далі відкрийте http://127.0.0.1:8000
"""

import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

from . import budget, cache, costs, drafting, gateway, jobs, limits, samples, telemetry

telemetry.setup()
log = logging.getLogger("app.http")

app = FastAPI(title="Помічник оператора — ПР11")

@app.exception_handler(limits.RateLimited)
async def rate_error(request, exc):
    rid=telemetry.request_id_var.get()
    return JSONResponse(status_code=429,content={"error":{"kind":"rate_limit","message":str(exc),"retryable":True},"request_id":rid},headers={"Retry-After":str(max(1,int(exc.retry_after)))})

@app.exception_handler(budget.BudgetExceeded)
async def budget_error(request, exc):
    rid=telemetry.request_id_var.get()
    return JSONResponse(status_code=429,content={"error":{"kind":"budget","message":str(exc),"retryable":False,"resets_at":exc.resets_at},"request_id":rid},headers={"Retry-After":"60"})

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    rid=telemetry.request_id_var.get()
    return JSONResponse(status_code=exc.status_code,content={"error":{"kind":"http_error","message":str(exc.detail)},"request_id":rid},headers=getattr(exc,"headers",None))

@app.exception_handler(KeyError)
async def missing_error(request, exc):
    rid=telemetry.request_id_var.get()
    return JSONResponse(status_code=404,content={"error":{"kind":"not_found","message":"Ресурс не знайдено"},"request_id":rid})

INDEX_PAGE = Path(__file__).parent / "templates" / "index.html"

# Ідентифікатор, який прислав клієнт, приймаємо лише в такому вигляді:
# у журнал не має потрапити довільний рядок із заголовка.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{4,64}$")


class DraftIn(BaseModel):
    """Замовлення чернетки зі сторінки.

    `request_id` — яке звернення зі зразків. `client` — ідентифікатор
    клієнта сторінки для обмеження частоти. `fresh` — оминути кеш:
    оператор хоче іншу чернетку, а не ту саму.
    """

    request_id: str
    client: str = "anonymous"
    fresh: bool = False


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Ідентифікатор запиту в контексті й у відповіді; запис про запит у
    журнал. Для потоку подій тривалість — до початку відповіді, не до
    кінця потоку."""
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _REQUEST_ID.match(incoming) else telemetry.new_request_id()
    token = telemetry.request_id_var.set(request_id)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        log.exception("http.unhandled", extra={"method": request.method, "path": request.url.path})
        raise
    finally:
        log.info("http.request", extra={
            "method": request.method,
            "path": request.url.path,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        telemetry.request_id_var.reset(token)


def draft_to_dict(draft: jobs.Draft) -> dict:
    """Знімок чернетки для сторінки — без журналу подій."""
    data = asdict(draft)
    data["events"] = len(draft.events)
    return data


def _or_pending(fn) -> dict:
    """Поки модуль — заготовка, сторінка має про це дізнатися, а не
    отримати 500."""
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/", response_class=HTMLResponse)
def page() -> str:
    """Віддати сторінку."""
    return INDEX_PAGE.read_text(encoding="utf-8")


@app.get("/api/health")
def api_health() -> dict:
    """Чинні налаштування — щоб сторінка показала, хто відповідає, яка
    версія prompt і які обмеження діють. Ключа тут немає."""
    return {
        "gateway": gateway.settings(),
        "limits": limits.settings(),
        "cache": cache.settings(),
        "budget": budget.settings(),
        "prompt_version": getattr(drafting, "PROMPT_VERSION", None),
        "log_file": telemetry.settings()["log_file"],
    }


@app.get("/api/usage")
def api_usage() -> dict:
    """Стан витрат і кешу — для панелі на сторінці."""
    return {
        "budget": _or_pending(budget.status),
        "cache": _or_pending(cache.stats),
        "pricing": costs.settings(),
    }


@app.get("/api/requests")
def api_requests() -> list[dict]:
    """Зразки звернень для вибору на сторінці."""
    return [samples.public(s) for s in samples.all_samples()]


@app.post("/api/drafts", status_code=202)
def api_create(payload: DraftIn) -> dict:
    """Замовити чернетку. Повертає її стан одразу, не чекаючи на
    генерацію; далі сторінка слухає події.

    Збої тут не оброблено — як і в ПР10: невідоме звернення, вичерпаний
    ліміт частоти (`limits.RateLimited`), вичерпаний бюджет
    (`budget.BudgetExceeded`, якщо перевіряєте його тут), збій запуску
    фонової роботи — усе це поки що закінчується помилкою 500. Формат
    тіла, статус, заголовок `Retry-After`, `request_id` у тілі —
    вирішуєте ви.
    """
    sample = samples.get(payload.request_id)
    if sample is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Звернення не знайдено")
    limits.acquire(payload.client)
    return draft_to_dict(jobs.start(sample, payload.client, fresh=payload.fresh))


@app.get("/api/drafts")
def api_drafts() -> list[dict]:
    """Останні чернетки, нові першими."""
    return [draft_to_dict(d) for d in jobs.list_drafts()]


@app.get("/api/drafts/{draft_id}")
def api_draft(draft_id: str) -> dict:
    """Знімок однієї чернетки. Невідомий номер поки що дає 500."""
    draft=jobs.get(draft_id)
    if draft is None: raise KeyError(draft_id)
    return draft_to_dict(draft)


@app.get("/api/drafts/{draft_id}/events")
def api_events(draft_id: str, request: Request) -> StreamingResponse:
    """Потік подій чернетки у форматі server-sent events.

    Браузер при обриві зʼєднання перепідключається сам і надсилає
    заголовок `Last-Event-ID` — звідси продовжуємо, не повторюючи
    віддане. Порожній коментар раз на кілька секунд тримає зʼєднання
    живим; якщо клієнт відключився, спроба записати в нього завершує
    генератор.
    """
    try:
        since = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        since = 0

    def stream() -> Iterator[str]:
        for event in jobs.subscribe(draft_id, since):
            if event is None:
                yield ": ping\n\n"
                continue
            data = json.dumps(event["data"], ensure_ascii=False)
            yield f"id: {event['id']}\nevent: {event['event']}\ndata: {data}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/drafts/{draft_id}/cancel")
def api_cancel(draft_id: str) -> dict:
    """Скасувати генерацію. Скасування вже завершеної або невідомої
    чернетки поки що дає 500 — який статус тут доречний, вирішуєте ви."""
    return draft_to_dict(jobs.cancel(draft_id))
