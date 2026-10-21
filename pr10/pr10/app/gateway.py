"""Шлюз до моделі: єдиний шлях, яким застосунок звертається до
провайдера.

Решта застосунку не викликає провайдерів напряму. Вона каже шлюзу
«згенеруй» — і отримує або результат, або контрольовану помилку
(`ProviderError`). Між цими двома подіями шлюз робить те, що не повинен
робити ані провайдер, ані веб-рівень, ані фонова задача. Шлюз не знає,
що саме генерується, і звідки прийшов запит; він знає лише
`providers.base`.

**Рішення**

*Три таймаути* (кожен ловить своє):

* `LLM_TIMEOUT` — на спробу. До першого фрагмента це «скільки чекати на
  початок відповіді», далі — стеля на всю спробу. Спливає — спроба
  обривається як `timeout` (повторюваний).
* `LLM_IDLE_TIMEOUT` — пауза між фрагментами вже після першого. Ловить
  «провайдер замовк посередині»: таймаут спроби спрацював би надто
  пізно. До першого фрагмента не застосовується — модель може
  «думати» довше, ніж між словами.
* `LLM_TOTAL_TIMEOUT` — на весь запит: сума спроб і пауз. Відлік
  починається, коли отримано місце до провайдера (час у черзі має
  окрему стелю `LLM_QUEUE_WAIT`). Спливає — нових спроб немає.

*Повтори*: лише `retryable` збої (timeout, rate_limit, unavailable).
`auth`, `bad_request`, `unknown` не повторюються. Пауза —
`min(BACKOFF_MAX, BACKOFF_BASE·2^(n-1)) · U(0.5, 1.5)`; якщо провайдер
надіслав `retry_after`, пауза не менша за нього. Якщо після паузи не
лишилося б часу на осмислену спробу (`MIN_ATTEMPT_S`) — шлюз не чекає,
а віддає помилку одразу: краще швидка чесна відмова (або запасний
варіант), ніж очікування, після якого все одно не встигнемо.

*Обрив посеред потоку*: якщо частину тексту вже віддано, а спроба
обірвалася, шлюз **повторює спочатку** й повідомляє `reset` — фонова
задача очищує текст, сторінка показує «починаємо спочатку». Чернетка
нічого не змінює, тож повтор безпечний, а склеювати хвіст другої
відповіді з початком першої не можна: моделі недетерміновані.
Якщо всі спроби обірвалися, текст останньої залишається на екрані
з помилкою — оператор не втрачає прочитане.

*Запасний варіант* (`LLM_FALLBACK`): вмикається після всіх спроб
основного, одна спроба, зі своїм таймаутом на спробу, не на
`bad_request` (його помилка — наша, а не провайдера) й не на
скасування. Результат позначений `Result.fallback = True`.

*Одночасність* (`LLM_MAX_CONCURRENT`): обмежувач на весь застосунок
з чесною чергою (FIFO — хто замовив раніше, той раніше й отримає місце).
Місце береться на *спробу* і віддається на час паузи між спробами.
Коли місця немає — чекаємо в черзі до `LLM_QUEUE_WAIT`, повідомляючи
`state=queued`, і лише потім відмовляємо (`unavailable`, повторюваний).
Це інше обмеження, ніж `limits.py`: там — скільки замовлень за хвилину
від одного клієнта, тут — скільки звертань одночасно від усіх.

*Скасування*: `is_cancelled()` опитується між фрагментами (кожні
0,2 с), під час паузи між спробами й у черзі за місцем. Провайдеру
передається `Request.abort`, який шлюз ставить, коли споживач
передумав або таймаут спрацював.

*Статуси*: `on_status` отримує словники з ключами `state`
(`queued`/`running`/`waiting`), `attempt`, `max_attempts`, `provider`,
`model`, `fallback`, `detail`, а також `reset` (почати текст спочатку).
"""

import dataclasses
import logging
import os
import queue
import random
import threading
import time
from typing import Callable

from dotenv import load_dotenv

from .providers.base import Chunk, Provider, ProviderError, Request, Result

load_dotenv()

log = logging.getLogger("gateway")

PROVIDER = os.getenv("LLM_PROVIDER", "openai")
FALLBACK = os.getenv("LLM_FALLBACK", "none")

TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))
IDLE_TIMEOUT = float(os.getenv("LLM_IDLE_TIMEOUT", "8"))
TOTAL_TIMEOUT = float(os.getenv("LLM_TOTAL_TIMEOUT", "45"))
MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
BACKOFF_BASE = float(os.getenv("LLM_BACKOFF_BASE", "0.5"))
BACKOFF_MAX = float(os.getenv("LLM_BACKOFF_MAX", "8"))
MAX_CONCURRENT = int(os.getenv("LLM_MAX_CONCURRENT", "2"))
QUEUE_WAIT = float(os.getenv("LLM_QUEUE_WAIT", "30"))
# Менше за стільки секунд на спробу немає сенсу починати.
MIN_ATTEMPT_S = float(os.getenv("LLM_MIN_ATTEMPT", "2"))

# Параметри генерації — спільні для всіх провайдерів.
MODEL = os.getenv("LLM_MODEL")
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "600"))

BASE_URL = os.getenv("LLM_BASE_URL", "")
API_KEY = os.getenv("LLM_API_KEY", "")

POLL = 0.2  # як часто споживач перевіряє скасування, секунд



class _Gate:
    """Обмеження одночасності з чесною чергою (FIFO). Звичайний семафор
    будить того, кого вирішить планувальник, і той, хто замовив раніше,
    може чекати найдовше; тут місце дістається за порядком замовлень, а
    позицію в черзі видно оператору."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._active = 0
        self._queue: list[object] = []
        self._cond = threading.Condition()

    def acquire(self, is_cancelled: Callable[[], bool], max_wait: float,
                on_wait: Callable[[int], None]) -> None:
        ticket = object()
        end = time.monotonic() + max_wait
        reported: int | None = None
        with self._cond:
            if self._active < self.size and not self._queue:
                self._active += 1
                return
            self._queue.append(ticket)
        try:
            while True:
                with self._cond:
                    if self._queue[0] is ticket and self._active < self.size:
                        self._queue.pop(0)
                        self._active += 1
                        self._cond.notify_all()
                        return
                    ahead = self._queue.index(ticket)
                    if ahead == reported:
                        self._cond.wait(timeout=POLL)
                if ahead != reported:
                    reported = ahead
                    on_wait(ahead)
                if is_cancelled():
                    raise _cancelled()
                if time.monotonic() >= end:
                    raise ProviderError(
                        "unavailable", "Сервіс зараз перевантажений: черга до моделі завелика. "
                        "Повторіть за хвилину.", retryable=True,
                        detail=f"queue wait {max_wait}s exceeded")
        except BaseException:
            with self._cond:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                self._cond.notify_all()
            raise

    def release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()


_slots = _Gate(MAX_CONCURRENT)
_providers: dict[str, Provider] = {}
_providers_lock = threading.Lock()


def settings() -> dict:
    """Чинні налаштування шлюзу — для сторінки й журналу. Ключа тут
    немає і бути не може."""
    return {
        "provider": PROVIDER,
        "fallback": FALLBACK,
        "model": MODEL,
        "timeout": TIMEOUT,
        "idle_timeout": IDLE_TIMEOUT,
        "total_timeout": TOTAL_TIMEOUT,
        "max_attempts": MAX_ATTEMPTS,
        "max_concurrent": MAX_CONCURRENT,
        "queue_wait": QUEUE_WAIT,
    }


# ---------------------------------------------------------------- провайдери

def make_provider(name: str, *, fallback: bool = False) -> Provider:
    """Зібрати провайдера за назвою: `openai` — з налаштувань доступу в
    `.env`, `mock` — підставний. Інша назва — та сама адреса й ключ, але
    інша модель (для запасного варіанту).

    `fallback=True` для `mock` означає «без навмисних збоїв»: запасний
    варіант має рятувати. Екземпляри кешуються, щоб не відкривати нове
    зʼєднання на кожну спробу. Модуль справжнього провайдера (з SDK)
    імпортується лише коли він справді потрібен — без `openai` у
    середовищі застосунок усе одно працює з `mock`.
    """
    key = f"{name}|{'fb' if fallback else 'main'}"
    with _providers_lock:
        cached = _providers.get(key)
        if cached is not None:
            return cached
        if name == "mock":
            from .providers.mock import MockProvider
            provider: Provider = MockProvider(failure_rate=0.0, fail_attempts=0) if fallback \
                else MockProvider()
        else:
            if not API_KEY:
                raise ProviderError("auth", "Ключ доступу до моделі не налаштовано. "
                                    "Зверніться до адміністратора.", detail="LLM_API_KEY empty")
            from .providers.openai_compat import OpenAICompatProvider
            model = MODEL if name == "openai" else name
            provider = OpenAICompatProvider(BASE_URL, API_KEY, model or "", TIMEOUT)
        _providers[key] = provider
        return provider


def _fallback_name() -> str | None:
    return None if FALLBACK.strip().lower() in ("", "none") else FALLBACK.strip()


# ------------------------------------------------------------------- допоміжне

def _cancelled() -> ProviderError:
    return ProviderError("cancelled", "Перервано.")


def _backoff(failures: int) -> float:
    """Пауза після `failures`-ї невдалої спроби: експоненційна з джитером."""
    return min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (failures - 1)) * random.uniform(0.5, 1.5)


def _sleep(seconds: float, is_cancelled: Callable[[], bool]) -> None:
    """Пауза, яку можна скасувати."""
    end = time.monotonic() + seconds
    while True:
        if is_cancelled():
            raise _cancelled()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.1, left))


def _acquire_slot(is_cancelled: Callable[[], bool], on_status: Callable[[dict], None],
                  max_wait: float) -> None:
    """Взяти місце до провайдера. Немає місця — чекаємо в черзі (видимо
    для оператора) не довше `max_wait`, не мовчки."""
    def on_wait(ahead: int) -> None:
        on_status({"state": "queued",
                   "detail": f"у черзі: усі {MAX_CONCURRENT} місця до моделі зайняті, "
                             f"попереду {ahead}" if ahead else
                             f"у черзі: усі {MAX_CONCURRENT} місця до моделі зайняті, ви наступні"})

    _slots.acquire(is_cancelled, max_wait, on_wait)


def _pump(provider: Provider, request: Request, out: "queue.Queue") -> None:
    """Робочий потік: читає фрагменти провайдера в чергу. Саме тут, а не в
    споживача, бо читання може блокуватися, а споживач мусить мати змогу
    обірвати очікування за таймаутом."""
    gen = None
    try:
        gen = provider.stream(request)
        for chunk in gen:
            out.put(("chunk", chunk))
            if request.abort is not None and request.abort.is_set():
                break
        out.put(("end", None))
    except ProviderError as exc:
        out.put(("error", exc))
    except Exception as exc:  # noqa: BLE001 — провайдер зобовʼязаний не кидати чужого
        out.put(("error", ProviderError("unknown", "Невідомий збій під час звернення до моделі.",
                                        detail=f"{type(exc).__name__}: {exc}"[:300])))
    finally:
        if gen is not None:
            try:
                gen.close()  # закриває зʼєднання з провайдером
            except Exception:  # noqa: BLE001
                pass


class _State:
    """Спільне між спробами: лічильник і чи віддано вже текст."""

    def __init__(self) -> None:
        self.attempts = 0
        self.delivered = False   # споживач уже бачив фрагменти поточної спроби
        self.deadline: float | None = None


def _attempt(provider: Provider, request: Request, st: _State, on_delta, is_cancelled,
             deadline: float) -> Result:
    """Одна спроба: стежить за трьома таймаутами й скасуванням."""
    abort = threading.Event()
    req = dataclasses.replace(request, abort=abort)
    out: "queue.Queue" = queue.Queue()
    started = time.monotonic()
    attempt_deadline = started + TIMEOUT
    threading.Thread(target=_pump, args=(provider, req, out), daemon=True,
                     name=f"pump-{request.metadata.get('draft_id', '-')}").start()
    parts: list[str] = []
    usage = finish = None
    first_at: float | None = None
    last_at = started
    try:
        while True:
            if is_cancelled():
                raise _cancelled()
            now = time.monotonic()
            left_attempt = attempt_deadline - now
            left_total = deadline - now
            left_idle = (last_at + IDLE_TIMEOUT - now) if first_at is not None else float("inf")
            wait = min(left_attempt, left_total, left_idle)
            if wait <= 0:
                if left_total <= 0 and left_total <= min(left_attempt, left_idle):
                    raise ProviderError("timeout", "Вичерпано час, відведений на чернетку.",
                                        retryable=True, detail=f"total timeout {TOTAL_TIMEOUT}s")
                if left_idle <= left_attempt:
                    raise ProviderError("timeout", "Провайдер замовк посеред чернетки.",
                                        retryable=True,
                                        detail=f"idle timeout {IDLE_TIMEOUT}s after "
                                               f"{len(parts)} chunks")
                raise ProviderError("timeout", "Провайдер не відповів вчасно.", retryable=True,
                                    detail=f"attempt timeout {TIMEOUT}s, "
                                           f"first chunk={'yes' if first_at else 'no'}")
            try:
                kind, value = out.get(timeout=min(wait, POLL))
            except queue.Empty:
                continue
            if kind == "error":
                raise value
            if kind == "end":
                break
            chunk: Chunk = value
            last_at = time.monotonic()
            usage = chunk.usage or usage
            finish = chunk.finish_reason or finish
            if chunk.text:
                if first_at is None:
                    first_at = last_at
                parts.append(chunk.text)
                st.delivered = True
                on_delta(chunk.text)
    finally:
        abort.set()  # просимо провайдера (і його потік) припинити
    model = request.model or getattr(provider, "model", None) or "?"
    return Result(text="".join(parts), provider=provider.name, model=model, usage=usage,
                  finish_reason=finish, elapsed=time.monotonic() - started,
                  first_token=(first_at - started) if first_at else None)


def _run_provider(provider: Provider, request: Request, st: _State, on_delta, on_status,
                  is_cancelled, *, fallback: bool, max_attempts: int, budget: float) -> Result:
    """Спроби до одного провайдера з паузами й бюджетом часу."""
    deadline: float | None = None
    last: ProviderError | None = None
    for n in range(1, max_attempts + 1):
        if is_cancelled():
            raise _cancelled()
        wait_cap = QUEUE_WAIT if deadline is None else max(0.0, min(QUEUE_WAIT, deadline - time.monotonic()))
        try:
            _acquire_slot(is_cancelled, on_status, wait_cap)
        except ProviderError as exc:
            exc.attempts = st.attempts
            raise
        try:
            if deadline is None:
                deadline = time.monotonic() + budget
            st.attempts += 1
            if st.delivered:
                on_status({"reset": True, "detail": "обрив — починаємо чернетку спочатку"})
                st.delivered = False
            on_status({"state": "running", "attempt": st.attempts,
                       "max_attempts": max_attempts, "provider": provider.name,
                       "model": getattr(provider, "model", None), "fallback": fallback,
                       "detail": "запасний варіант" if fallback else
                                 (f"спроба {n} з {max_attempts}" if max_attempts > 1 else "")})
            log.info("draft=%s attempt=%d provider=%s fallback=%s",
                     request.metadata.get("draft_id"), st.attempts, provider.name, fallback)
            try:
                return _attempt(provider, request, st, on_delta, is_cancelled, deadline)
            except ProviderError as exc:
                last = exc
                last.attempts = st.attempts
                (log.info if exc.kind == "cancelled" else log.warning)(
                    "draft=%s attempt=%d failed kind=%s retryable=%s retry_after=%s detail=%s",
                    request.metadata.get("draft_id"), st.attempts, exc.kind, exc.retryable,
                    exc.retry_after, exc.detail)
                if exc.kind == "cancelled":
                    raise
        finally:
            _slots.release()
        # --- спроба невдала; вирішуємо, чи повторювати ---
        if not last.retryable or n >= max_attempts:
            break
        pause = _backoff(n)
        if last.retry_after is not None:
            pause = max(pause, last.retry_after)
        remaining = deadline - time.monotonic()
        if remaining - pause < MIN_ATTEMPT_S:
            log.warning("draft=%s не чекаємо %.1f с: у бюджеті лишилось %.1f с",
                        request.metadata.get("draft_id"), pause, remaining)
            on_status({"detail": f"повтор не вкладається в час ({pause:.0f} с паузи, "
                                 f"лишилось {max(remaining, 0):.0f} с)"})
            break
        on_status({"state": "waiting", "attempt": st.attempts, "kind": last.kind,
                   "pause": round(pause, 1), "provider": provider.name, "fallback": fallback,
                   "detail": f"{last.message} Повтор через {pause:.1f} с…"})
        _sleep(pause, is_cancelled)
    assert last is not None
    raise last


def generate(request: Request, on_delta: Callable[[str], None],
             on_status: Callable[[dict], None], is_cancelled: Callable[[], bool]) -> Result:
    """Згенерувати відповідь, дотримуючись таймаутів, спроб, обмеження
    одночасності й запасного варіанту.

    `on_delta` отримує кожен фрагмент тексту, `on_status` — зміни стану
    (див. докстрінг модуля), `is_cancelled` опитується між фрагментами й
    між спробами.

    Повертає `Result` або підіймає `ProviderError` — уже остаточний,
    після всіх спроб і запасного варіанту. Інших винятків назовні не
    виходить.
    """
    st = _State()
    used_fallback = False
    try:
        try:
            provider = make_provider(PROVIDER)
        except ProviderError as exc:
            exc.attempts = st.attempts
            raise
        result = _run_provider(provider, request, st, on_delta, on_status, is_cancelled,
                               fallback=False, max_attempts=MAX_ATTEMPTS, budget=TOTAL_TIMEOUT)
    except ProviderError as primary:
        if primary.kind in ("cancelled", "bad_request"):
            raise
        name = _fallback_name()
        if name is None:
            raise
        log.warning("draft=%s основний провайдер не впорався (%s), вмикаємо запасний «%s»",
                    request.metadata.get("draft_id"), primary.kind, name)
        on_status({"detail": "основний провайдер не впорався — запасний варіант", "fallback": True})
        used_fallback = True
        try:
            result = _run_provider(make_provider(name, fallback=True), request, st, on_delta,
                                   on_status, is_cancelled, fallback=True, max_attempts=1,
                                   budget=TIMEOUT)
        except ProviderError as second:
            if second.kind == "cancelled":
                raise
            primary.attempts = st.attempts
            primary.detail = f"{primary.detail}; fallback {second.kind}: {second.detail}"
            raise primary from None
    result.attempts = st.attempts
    result.fallback = used_fallback
    return result
