"""Фонові задачі: чернетка генерується не в HTTP-запиті, а окремо.

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

Рішення в `start` і `cancel`:

* **Виконання**: окремий потік (`threading.Thread`, daemon) на чернетку.
  Кількість звертань до провайдера обмежує не він, а шлюз (семафор);
  потік у черзі лише чекає й опитує скасування. Кількість потоків
  обмежена лімітом на клієнта та стелею черги `LLM_QUEUE_WAIT`.
* **Порядок**: `start` створює чернетку, пише подію `status: queued` і
  запускає потік. Потік будує запит, викликає шлюз; шлюз повідомляє
  стан (`queued` → `running` → «пауза перед повтором» → `reset`), текст
  потрапляє в `delta`. Підсумок — рівно одна кінцева подія: `done`,
  `error` або `cancelled`.
* **Жодного висячого `running`**: усе тіло потоку обгорнуто так, що
  будь-який виняток (у тому числі непередбачений) завершується подією
  `error` з видом `internal`; у `finally` додатково перевіряється, що
  чернетка в кінцевому статусі.
* **Скасування спільне**: `cancel` ставить `threading.Event` (його
  опитує шлюз між фрагментами й спробами) і *одразу* переводить
  чернетку в `cancelled` під тим самим замком, що й усі інші переходи.
  Кінцевий статус призначається один раз: той, хто встиг першим під
  замком, виграє, а пізніші `delta`/`done`/`error` відкидаються. Тому
  скасована чернетка ніколи не стане `done`, навіть якщо потік закінчив
  роботу на мить пізніше. Текст, що встиг прийти, лишається.
* **`cancel` завершеної чернетки** — `AlreadyFinished` (веб-рівень: 409);
  невідомої — `NotFound` (404). Повторного виконання немає.
* **Клієнт закрив сторінку**: генерація *триває* — чернетка належить
  оператору, а не зʼєднанню; її можна відкрити з переліку. Витрати
  обмежені `LLM_MAX_TOKENS`, спробами й бюджетом часу, а скасувати можна
  будь-коли.
* **Старі чернетки**: у памʼяті лишається не більше `DRAFTS_KEEP`
  завершених (за замовчуванням 200) і не старше `DRAFTS_TTL_S`
  (24 год); активні не чіпаються.

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

import dataclasses
import datetime
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import drafting, gateway
from .providers.base import ProviderError

log = logging.getLogger("jobs")

# Статуси, після яких подій більше не буде.
TERMINAL = {"done", "failed", "cancelled"}

DRAFTS_KEEP = int(os.getenv("DRAFTS_KEEP", "200"))
DRAFTS_TTL_S = float(os.getenv("DRAFTS_TTL_S", "86400"))


class NotFound(Exception):
    """Чернетки з таким номером немає."""


class AlreadyFinished(Exception):
    """Чернетка вже в кінцевому статусі — скасовувати нічого."""

    def __init__(self, draft: "Draft") -> None:
        super().__init__(f"чернетка вже має статус {draft.status}")
        self.draft = draft


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


def snapshot(draft: Draft) -> dict:
    """Знімок чернетки для сторінки: узгоджений (під замком), без
    журналу подій — лише їх кількість."""
    with _lock:
        data = {f.name: getattr(draft, f.name) for f in dataclasses.fields(draft)
                if f.name != "events"}
        data["elapsed"] = dict(draft.elapsed)
        data["warnings"] = list(draft.warnings)
        data["error"] = dict(draft.error) if draft.error else None
        data["usage"] = dict(draft.usage) if draft.usage else None
        data["events"] = len(draft.events)
    return data


_flags: dict[str, threading.Event] = {}
_finished_at: dict[str, float] = {}


def _apply(draft: Draft, event: str, data: dict | None = None, *,
           mutate: Callable[[Draft], None] | None = None, terminal: bool = False) -> bool:
    """Єдиний шлях зміни чернетки: під замком, лише поки вона не в
    кінцевому статусі, разом із подією. Повертає `False`, якщо чернетка
    вже завершена (зміну відкинуто). `terminal=True` — це кінцевий
    перехід; його «виграє» той, хто перший."""
    with _changed:
        if draft.status in TERMINAL:
            return False
        if mutate is not None:
            mutate(draft)
        draft.events.append({"id": len(draft.events) + 1, "event": event, "data": data or {}})
        draft.updated_at = _now()
        if draft.status in TERMINAL:
            _finished_at[draft.draft_id] = time.monotonic()
        _changed.notify_all()
        return True


def _evict() -> None:
    """Прибрати старі завершені чернетки, щоб реєстр не ріс безмежно."""
    now = time.monotonic()
    with _lock:
        finished = [i for i in _order if _drafts[i].status in TERMINAL]
        drop = set(finished[DRAFTS_KEEP:])
        drop |= {i for i in finished if now - _finished_at.get(i, now) > DRAFTS_TTL_S}
        if drop:
            for i in drop:
                _drafts.pop(i, None)
                _flags.pop(i, None)
                _finished_at.pop(i, None)
            _order[:] = [i for i in _order if i not in drop]


def _fail(draft: Draft, kind: str, message: str, retryable: bool, t0: float, **extra) -> None:
    error = {"kind": kind, "message": message, "retryable": retryable,
             "draft_id": draft.draft_id, **extra}

    def mutate(d: Draft) -> None:
        d.status = "failed"
        d.error = error
        d.elapsed["total"] = round(time.monotonic() - t0, 2)

    _apply(draft, "error", dict(error, partial=bool(draft.text)), mutate=mutate, terminal=True)


def _run(draft: Draft, sample: dict, flag: threading.Event, t0: float) -> None:
    """Тіло фонового потоку. Завершується рівно однією кінцевою подією."""

    def on_delta(text: str) -> None:
        def mutate(d: Draft) -> None:
            d.text += text
            d.elapsed.setdefault("first_token", round(time.monotonic() - t0, 2))
        _apply(draft, "delta", {"text": text}, mutate=mutate)

    def on_status(info: dict) -> None:
        if info.get("reset"):
            _apply(draft, "reset", {"detail": info.get("detail", "")},
                   mutate=lambda d: setattr(d, "text", ""))
            return
        state = info.get("state")
        data = {k: info[k] for k in ("attempt", "max_attempts", "provider", "model", "fallback",
                                     "detail", "pause", "kind") if k in info}

        def mutate(d: Draft) -> None:
            if state == "queued":
                d.status = "queued"
            elif state in ("running", "waiting"):
                d.status = "running"
            for key in ("provider", "model", "fallback"):
                if key in info and info[key] is not None:
                    setattr(d, key, info[key])
            if "attempt" in info:
                d.attempts = info["attempt"]
            data["status"] = d.status

        _apply(draft, "status", data, mutate=mutate)

    try:
        request = drafting.build_request(sample)
        request.metadata["draft_id"] = draft.draft_id
        result = gateway.generate(request, on_delta, on_status, flag.is_set)
        try:
            warnings = drafting.check(result.text, sample)
        except Exception:  # noqa: BLE001 — перевірка не має валити чернетку
            log.exception("draft=%s check failed", draft.draft_id)
            warnings = ["Автоматичну перевірку чернетки не виконано — прочитайте текст уважно."]
        if result.fallback:
            warnings.insert(0, "Запасний варіант: основна модель не впоралась — це не її "
                               "відповідь. Перевірте текст особливо уважно.")
        elif result.provider == "mock":
            warnings.insert(0, "Це підставний текст (mock), а не відповідь моделі.")

        def mutate(d: Draft) -> None:
            d.status = "done"
            d.text = result.text
            d.provider, d.model, d.fallback = result.provider, result.model, result.fallback
            d.attempts, d.usage, d.warnings = result.attempts, result.usage, warnings
            d.elapsed["total"] = round(time.monotonic() - t0, 2)
            if result.first_token is not None:
                d.elapsed["provider_first_token"] = round(result.first_token, 2)

        # `done` — лише якщо чернетку до цього моменту не скасовано/не завершено.
        _apply(draft, "done", {
            "text": result.text, "provider": result.provider, "model": result.model,
            "usage": result.usage, "attempts": result.attempts, "fallback": result.fallback,
            "warnings": warnings,
            "elapsed": {"first_token": draft.elapsed.get("first_token"),
                        "total": round(time.monotonic() - t0, 2)},
        }, mutate=mutate, terminal=True)
    except ProviderError as exc:
        if exc.kind == "cancelled" or flag.is_set():
            return  # `cancel` уже поставив кінцевий статус
        log.warning("draft=%s failed kind=%s attempts=%s detail=%s", draft.draft_id, exc.kind,
                    exc.attempts, exc.detail)
        _fail(draft, exc.kind, exc.message, exc.retryable, t0, attempts=exc.attempts)
    except Exception:  # noqa: BLE001 — непередбачене не має лишити `running`
        log.exception("draft=%s unexpected failure", draft.draft_id)
        _fail(draft, "internal", "Внутрішня помилка під час створення чернетки. "
              "Повторіть замовлення; якщо повториться — зверніться до адміністратора.", True, t0)
    finally:
        if draft.status not in TERMINAL:  # страховка від висячого running
            _fail(draft, "internal", "Генерація завершилась без результату. Повторіть замовлення.",
                  True, t0)


def start(sample: dict, client: str) -> Draft:
    """Створити чернетку для звернення `sample` і запустити генерацію у
    фоні. Повертає чернетку одразу — найімовірніше, ще `queued`."""
    if not sample:
        raise ValueError("sample is required")
    draft = new_draft(sample["id"], client)
    flag = threading.Event()
    with _lock:
        _flags[draft.draft_id] = flag
    t0 = time.monotonic()
    _apply(draft, "status", {"status": "queued", "detail": "у черзі"})
    try:
        threading.Thread(target=_run, args=(draft, sample, flag, t0), daemon=True,
                         name=f"draft-{draft.draft_id}").start()
    except Exception:  # noqa: BLE001 — не вдалося запустити потік
        log.exception("draft=%s thread start failed", draft.draft_id)
        _fail(draft, "internal", "Не вдалося запустити генерацію. Повторіть замовлення.", True, t0)
    _evict()
    return draft


def cancel(draft_id: str) -> Draft:
    """Попросити фонову роботу зупинитися. Повертає чернетку; невідома —
    `NotFound`, уже завершена — `AlreadyFinished`.

    Прапорець і кінцевий статус ставляться під одним замком, тож або
    скасування встигло раніше за `done` (і `done` буде відкинуто), або
    чернетка вже завершена (і це відмова). Проміжного стану немає.
    """
    with _changed:
        draft = _drafts.get(draft_id)
        if draft is None:
            raise NotFound(draft_id)
        if draft.status in TERMINAL:
            raise AlreadyFinished(draft)
        flag = _flags.get(draft_id)
        if flag is not None:
            flag.set()
        draft.status = "cancelled"
        draft.events.append({"id": len(draft.events) + 1, "event": "cancelled", "data": {}})
        draft.updated_at = _now()
        _finished_at[draft_id] = time.monotonic()
        _changed.notify_all()
        return draft
