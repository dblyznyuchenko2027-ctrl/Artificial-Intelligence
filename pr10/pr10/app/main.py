"""Веб-рівень застосунку: сторінка помічника оператора і JSON-ендпоінти.

Цей файл не знає ані який провайдер відповідає, ані скільки було спроб,
ані в якому потоці йде генерація — усе це в `app/gateway.py`,
`app/providers/` і `app/jobs.py`. Тут вирішується інше: що застосунок
приймає від сторінки, що віддає їй і з яким HTTP-статусом, і як події
фонової роботи стають потоком server-sent events.

Запуск із папки pr10:

    uvicorn app.main:app --reload

Далі відкрийте http://127.0.0.1:8000

**Єдиний формат помилок** (усе, що не 2xx):

    {"error": {"kind": "...", "message": "...", "retryable": bool, "request_id": "..."}}

`request_id` — той самий, що в заголовку `X-Request-ID` і в записі
журналу (там — і слід стека, і технічні подробиці; на сторінку вони не
потрапляють). Статуси:

* 400 — неправильний запит (`bad_request`, `unknown_request`);
* 404 — немає такої чернетки (`not_found`);
* 409 — скасування вже завершеної чернетки (`already_finished`);
* 429 — наш ліміт на клієнта, із заголовком `Retry-After`
  (`rate_limited`);
* 500 — наша помилка (`internal`) із загальним повідомленням.

Збій *провайдера* сюди не доходить: замовлення прийнято з 202, а збій
приходить подією `error` того самого формату (з видом збою; 503/504 для
синхронного виклику не знадобились, бо синхронного виклику немає).
"""

import json
import logging
import math
import uuid
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import gateway, jobs, limits, samples

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("web")

app = FastAPI(title="Помічник оператора — ПР10")

INDEX_PAGE = Path(__file__).parent / "templates" / "index.html"


class DraftIn(BaseModel):
    """Замовлення чернетки зі сторінки.

    `request_id` — яке звернення зі зразків. `client` — ідентифікатор
    клієнта сторінки для обмеження частоти; у справжньому застосунку
    його взяв би із сесії веб-рівень, а не сторінка.
    """

    request_id: str = Field(max_length=64)
    client: str = Field(default="anonymous", max_length=64)


class ApiError(Exception):
    """Контрольована помилка веб-рівня."""

    def __init__(self, status: int, kind: str, message: str, retryable: bool = False,
                 headers: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message
        self.retryable, self.headers = retryable, headers or {}


def _error_response(status: int, kind: str, message: str, retryable: bool,
                    headers: dict | None = None) -> JSONResponse:
    request_id = uuid.uuid4().hex[:8]
    log.info("request_id=%s status=%s kind=%s", request_id, status, kind)
    body = {"error": {"kind": kind, "message": message, "retryable": retryable,
                      "request_id": request_id}}
    return JSONResponse(body, status_code=status,
                        headers={**(headers or {}), "X-Request-ID": request_id})


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return _error_response(exc.status, exc.kind, exc.message, exc.retryable, exc.headers)


@app.exception_handler(limits.RateLimited)
async def _rate_limited(request: Request, exc: limits.RateLimited) -> JSONResponse:
    wait = max(1, math.ceil(exc.retry_after))
    return _error_response(429, "rate_limited",
                           f"Забагато замовлень за хвилину. Спробуйте через {wait} с.", True,
                           {"Retry-After": str(wait)})


@app.exception_handler(jobs.NotFound)
async def _not_found(request: Request, exc: jobs.NotFound) -> JSONResponse:
    return _error_response(404, "not_found", "Такої чернетки немає (можливо, її вже прибрано).",
                           False)


@app.exception_handler(jobs.AlreadyFinished)
async def _already_finished(request: Request, exc: jobs.AlreadyFinished) -> JSONResponse:
    return _error_response(409, "already_finished",
                           f"Чернетка вже завершена (статус: {exc.draft.status}) — скасовувати "
                           "нічого.", False)


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(400, "bad_request", "Неправильний запит: перевірте поля.", False)


@app.exception_handler(StarletteHTTPException)
async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    kind = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error_response(exc.status_code, kind, "Запит не можна виконати.", False)


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    request_id = uuid.uuid4().hex[:8]
    log.exception("request_id=%s unexpected error on %s %s", request_id, request.method,
                  request.url.path)
    body = {"error": {"kind": "internal", "message": "Внутрішня помилка застосунку. Спробуйте "
                      "ще раз; якщо повториться — назвіть код запиту адміністратору.",
                      "retryable": True, "request_id": request_id}}
    return JSONResponse(body, status_code=500, headers={"X-Request-ID": request_id})


def draft_to_dict(draft: jobs.Draft) -> dict:
    """Знімок чернетки для сторінки — без журналу подій."""
    return jobs.snapshot(draft)


@app.get("/", response_class=HTMLResponse)
def page() -> str:
    """Віддати сторінку."""
    return INDEX_PAGE.read_text(encoding="utf-8")


@app.get("/api/health")
def api_health() -> dict:
    """Чинні налаштування — щоб сторінка показала, хто відповідає і які
    обмеження діють. Ключа тут немає."""
    return {"gateway": gateway.settings(), "limits": limits.settings()}


@app.get("/api/requests")
def api_requests() -> list[dict]:
    """Зразки звернень для вибору на сторінці."""
    return [samples.public(s) for s in samples.all_samples()]


@app.post("/api/drafts", status_code=202)
def api_create(payload: DraftIn) -> dict:
    """Замовити чернетку. Повертає її стан одразу, не чекаючи на
    генерацію; далі сторінка слухає події.

    Порядок важить: спершу перевіряємо звернення (невідоме — 400 і
    ліміт не витрачається), потім ліміт клієнта (429 + `Retry-After`),
    і лише тоді запускаємо роботу. Збій провайдера сюди не доходить —
    він стається пізніше, у фоні, і стає подією `error`.
    """
    sample = samples.get(payload.request_id)
    if sample is None:
        raise ApiError(400, "unknown_request", f"Невідоме звернення «{payload.request_id}».")
    client = payload.client.strip() or "anonymous"
    limits.acquire(client)
    return draft_to_dict(jobs.start(sample, client))


@app.get("/api/drafts")
def api_drafts() -> list[dict]:
    """Останні чернетки, нові першими."""
    return [draft_to_dict(d) for d in jobs.list_drafts()]


@app.get("/api/drafts/{draft_id}")
def api_draft(draft_id: str) -> dict:
    """Знімок однієї чернетки; невідомий номер — 404."""
    draft = jobs.get(draft_id)
    if draft is None:
        raise jobs.NotFound(draft_id)
    return draft_to_dict(draft)


@app.get("/api/drafts/{draft_id}/events")
def api_events(draft_id: str, request: Request) -> StreamingResponse:
    """Потік подій чернетки у форматі server-sent events.

    Браузер при обриві зʼєднання перепідключається сам і надсилає
    заголовок `Last-Event-ID` — звідси продовжуємо, не повторюючи
    віддане. Порожній коментар раз на кілька секунд тримає зʼєднання
    живим; якщо клієнт відключився, спроба записати в нього завершує
    генератор. Невідома чернетка — 404 (а не порожній потік).
    """
    if jobs.get(draft_id) is None:
        raise jobs.NotFound(draft_id)
    try:
        since = max(0, int(request.headers.get("last-event-id") or 0))
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
    """Скасувати генерацію. Невідома чернетка — 404, уже завершена —
    409 (обидва — через обробники помилок)."""
    return draft_to_dict(jobs.cancel(draft_id))
