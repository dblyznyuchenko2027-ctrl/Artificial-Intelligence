"""Заготовка з ПР10. Замініть цей файл своїм `pr10/app/jobs.py` і
допишіть те, що перелічено нижче.

Що додати в ПР11:

* ідентифікатор запиту: фонова робота виконується в іншому потоці, і
  контекстна змінна `telemetry.request_id_var` там порожня. Передайте
  ідентифікатор явно (у `Draft`, у `Request.metadata`, в `extra`
  кожного запису) або запустіть роботу в копії контексту
  (`contextvars.copy_context().run`). Усі записи чернетки мають містити
  `draft_id` і `request_id`;
* журнал: `draft.created` (звернення, клієнт, fresh) і `draft.done` /
  `draft.failed` / `draft.cancelled` (спроб, провайдер, кеш, вартість,
  тривалість). Текст чернетки — ні;
* прапорець `fresh` з `POST /api/drafts`: `start(sample, client,
  fresh=False)` → `Request.metadata` → шлюз оминає кеш. Сигнатуру
  заготовки нижче вже змінено — змініть і свою;
* у `Draft`, знімку й події `done` — `cached`, `cost`, `cost_estimated`:
  сторінка показує їх, якщо вони є;
* відмова через бюджет: шлюз підіймає `ProviderError(kind="budget")` —
  це подія `error` з `retryable=False` і часом відновлення в
  повідомленні. Чи перевіряти бюджет ще й до запуску роботи, щоб
  оператор дізнався одразу, — див. `main.py`.

Нижче — заготовка ПР10 без змін.

Фонові задачі: чернетка генерується не в HTTP-запиті, а окремо.

`POST /api/drafts` повертається одразу з номером чернетки, а робота
триває у фоні: сторінка дізнається про стан через потік подій
(`GET /api/drafts/{id}/events`) або знімок (`GET /api/drafts/{id}`) і
може скасувати її (`POST /api/drafts/{id}/cancel`). Так чернетка не
прив'язана до одного HTTP-зʼєднання: сторінку можна перезавантажити,
а потім відкрити ту саму чернетку й побачити все, що встигло статися.

Модуль не знає, як влаштований шлюз і що саме генерується; він знає
лише `Draft`, події і те, що робота виконується не в потоці запиту.

Що тут дано готовим — реєстр чернеток і події. `emit` додає подію до
журналу чернетки й будить тих, хто чекає; `subscribe` віддає події
від указаного номера і далі в міру надходження, доки чернетка не
дійде до кінцевого статусу. Веб-рівень перетворює це на
server-sent events; реалізація фонової роботи до цього не торкається.

Заготовки — `start` і `cancel`. Рішення з розділу 2 практичної роботи:

* у чому виконувати: потік на чернетку, пул потоків, asyncio-задача?
  Що обмежує кількість одночасних робіт — це чи шлюз;
* порядок дій у фоновій роботі: яку подію й який статус виставити
  спершу, що робити з фрагментами, що — з результатом і з помилкою.
  Жоден виняток не має лишити чернетку в стані `running`;
* скасування — спільне: фонова робота сама перевіряє прапорець між
  фрагментами й спробами. Хто його ставить, де він живе, що
  відбувається з текстом, який уже надіслано, і чи може скасована
  чернетка потім стати `done`, якщо потік завершився пізніше;
* `cancel` для чернетки, яка вже завершена чи не існує, — помилка якого
  роду і з яким HTTP-статусом;
* що буває зі старими чернетками: реєстр у памʼяті росте з кожною.
  Скільки тримати і коли прибирати.

Події, які розуміє сторінка (інші вона показує як є):

    status     {"status": "running", "attempt": 1, "provider": "openai",
                "fallback": false, "detail": "…"}
    delta      {"text": "…"}                      приріст тексту
    done       {"text": "…", "provider": "…", "model": "…",
                "usage": {...}, "elapsed": {...}, "attempts": 2,
                "fallback": false, "warnings": ["…"]}
    error      {"kind": "timeout", "message": "…", "retryable": true}
    cancelled  {}

Поля — відправна точка; змінюючи їх, змініть і сторінку.
"""

import datetime
import threading
import logging
import time
from . import drafting, gateway, telemetry
from .providers.base import ProviderError
import uuid
from dataclasses import dataclass, field
from typing import Iterator

# Статуси, після яких подій більше не буде.
TERMINAL = {"done", "failed", "cancelled"}


@dataclass
class Draft:
    """Стан однієї чернетки — те, що зберігається в реєстрі і йде на
    сторінку як знімок.

    `status` — `queued`, `running`, `done`, `failed`, `cancelled`.
    `text` — накопичений текст. `attempts`, `provider`, `model`,
    `fallback` — звідки взявся результат. `error` — остаточна помилка,
    якщо є: `kind`, `message`, `retryable`. `elapsed` — секунди, наприклад
    `{"first_token": 0.8, "total": 4.1}`. `warnings` — зауваження до
    чернетки з `drafting.check`. `events` — журнал подій для потоку;
    у знімок на сторінку не входить.
    """

    draft_id: str
    request_id: str
    client: str
    status: str = "queued"
    text: str = ""
    attempts: int = 0
    provider: str | None = None
    model: str | None = None
    fallback: bool = False
    error: dict | None = None
    usage: dict | None = None
    elapsed: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    events: list[dict] = field(default_factory=list)
    cached: bool = False
    cost: float | None = None
    cost_estimated: bool = False
    fresh: bool = False


_drafts: dict[str, Draft] = {}
_order: list[str] = []
_lock = threading.Lock()
_changed = threading.Condition(_lock)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_draft(request_id: str, client: str) -> Draft:
    """Створити чернетку зі статусом `queued` і зареєструвати її.
    Роботу не запускає — це справа `start`."""
    draft = Draft(draft_id=uuid.uuid4().hex[:8], request_id=request_id, client=client,
                  created_at=_now(), updated_at=_now())
    with _lock:
        _drafts[draft.draft_id] = draft
        _order.insert(0, draft.draft_id)
    return draft


def get(draft_id: str) -> Draft | None:
    with _lock:
        return _drafts.get(draft_id)


def list_drafts(limit: int = 50) -> list[Draft]:
    """Чернетки, нові першими."""
    with _lock:
        return [_drafts[i] for i in _order[:limit]]


def emit(draft: Draft, event: str, data: dict | None = None) -> None:
    """Додати подію до журналу чернетки й розбудити підписників.

    Статус і текст чернетки ця функція не змінює — це робить фонова
    робота разом із подією, під тим самим замком, якщо їй потрібна
    узгодженість між знімком і потоком.
    """
    with _changed:
        draft.events.append({"id": len(draft.events) + 1, "event": event, "data": data or {}})
        draft.updated_at = _now()
        _changed.notify_all()


def subscribe(draft_id: str, since: int = 0, heartbeat: float = 15.0) -> Iterator[dict | None]:
    """Віддавати події чернетки, починаючи з номера `since + 1`.

    Спершу — ті, що вже є, потім — нові в міру надходження. Завершується,
    коли чернетка в кінцевому статусі й усі події віддано, або коли
    чернетки немає. Якщо подій немає довше за `heartbeat` секунд, віддає
    `None`: веб-рівень надсилає порожній коментар, щоб зʼєднання не
    закрив проксі і щоб помітити, що клієнт відключився.
    """
    while True:
        with _changed:
            draft = _drafts.get(draft_id)
            if draft is None:
                return
            pending = draft.events[since:]
            if not pending:
                if draft.status in TERMINAL:
                    return
                _changed.wait(timeout=heartbeat)
                pending = draft.events[since:]
        if not pending:
            yield None
            continue
        for event in pending:
            yield event
        since += len(pending)


def start(sample: dict, client: str, fresh: bool = False) -> Draft:
    """Створити чернетку для звернення `sample` і запустити генерацію у
    фоні. Повертає чернетку одразу — найімовірніше, ще `queued`.
    `fresh` — оминути кеш (кнопка «Повторити»)."""
    draft = new_draft(telemetry.request_id_var.get() or telemetry.new_request_id(), client)
    draft.fresh = fresh
    sample_copy = dict(sample)
    log = logging.getLogger("app.jobs")
    log.info("draft.created", extra={"request_id": draft.request_id, "draft_id": draft.draft_id,
             "client": client, "sample_id": sample.get("id"), "fresh": fresh})
    emit(draft, "status", {"status": "queued"})
    def work():
        started = time.perf_counter()
        first_token_at = None
        try:
            draft.status = "running"; emit(draft, "status", {"status": "running"})
            req = drafting.build_request(sample_copy)
            req.metadata.update({"draft_id": draft.draft_id, "http_request_id": draft.request_id,
                                 "client": client, "fresh": fresh, "sample_id": sample_copy.get("id")})
            def delta(part):
                nonlocal first_token_at
                if draft.status == "running":
                    if part and first_token_at is None: first_token_at = time.perf_counter()
                    draft.text += part; emit(draft, "delta", {"text": part})
            def status(data):
                draft.attempts = max(draft.attempts, int(data.get("attempt", 0)))
                draft.provider = data.get("provider", draft.provider); draft.fallback = bool(data.get("fallback"))
                emit(draft, "status", data)
            result = gateway.generate(req, delta, status, lambda: draft.status == "cancelled")
            if draft.status == "cancelled": return
            draft.text = result.text; draft.provider=result.provider; draft.model=result.model
            draft.usage=result.usage; draft.cached=result.cached; draft.cost=result.cost
            draft.cost_estimated=result.cost_estimated
            draft.elapsed={"first_token": round(first_token_at-started,3) if first_token_at else 0.0,
                           "total": round(time.perf_counter()-started,3)}
            draft.warnings=drafting.check(draft.text,sample_copy); draft.status="done"
            emit(draft,"done",{"text":draft.text,"provider":draft.provider,"model":draft.model,"usage":draft.usage,
                 "elapsed":draft.elapsed,"attempts":draft.attempts,"fallback":draft.fallback,"warnings":draft.warnings,
                 "cached":draft.cached,"cost":draft.cost,"cost_estimated":draft.cost_estimated})
            log.info("draft.done",extra={"request_id":draft.request_id,"draft_id":draft.draft_id,"client":client,
                     "attempts":draft.attempts,"provider":draft.provider,"cached":draft.cached,"cost":draft.cost,
                     "duration_ms":round((time.perf_counter()-started)*1000,1)})
        except ProviderError as exc:
            if exc.kind == "cancelled": draft.status="cancelled";emit(draft,"cancelled",{})
            else:
                draft.status="failed";draft.error={"kind":exc.kind,"message":exc.message,"retryable":exc.retryable}
                emit(draft,"error",draft.error)
            log.error("draft.failed",extra={"request_id":draft.request_id,"draft_id":draft.draft_id,"client":client,"kind":exc.kind})
        except Exception as exc:
            draft.status="failed";draft.error={"kind":"unknown","message":"Внутрішня помилка генерації","retryable":False}
            emit(draft,"error",draft.error)
            log.exception("draft.failed",extra={"request_id":draft.request_id,"draft_id":draft.draft_id,"client":client,"kind":"unknown"})
    threading.Thread(target=work,name=f"draft-{draft.draft_id}",daemon=True).start()
    return draft


def cancel(draft_id: str) -> Draft:
    """Попросити фонову роботу зупинитися. Повертає чернетку; якщо її
    немає або вона вже завершена — вирішуєте ви."""
    draft=get(draft_id)
    if draft is None: raise KeyError(draft_id)
    if draft.status in TERMINAL: return draft
    draft.status="cancelled"; emit(draft,"cancelled",{})
    logging.getLogger("app.jobs").info("draft.cancelled",extra={"request_id":draft.request_id,"draft_id":draft.draft_id,"client":draft.client})
    return draft
