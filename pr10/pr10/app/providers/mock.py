"""Підставний провайдер: поводиться як справжній, але без мережі й
ключа — і збоїть тоді, коли ви просите.

Рішення:

* **Текст.** Береться `reply_example` за `metadata["request_id"]`. Якщо
  звернення невідоме — сталий рядок-заглушка, чітко позначений як
  підставний. Ззовні це видно й так (`Result.provider == "mock"`), а
  `drafting`/`jobs` додають попередження оператору.
* **Затримки.** `MOCK_FIRST_DELAY_MS` до першого фрагмента,
  `MOCK_DELAY_MS` між наступними. Фрагменти — по дві-три «слова», щоб
  streaming на сторінці був видимим.
* **Збої** (`MOCK_FAILURE_KIND`):
    - `timeout` — мовчить, доки його не перервуть (до `MOCK_HANG_S`):
      так перевіряється таймаут *шлюзу*, а не лише обробка помилки;
    - `rate_limit` — одразу 429 з `retry_after = MOCK_RETRY_AFTER_S`;
    - `unavailable` — після короткої паузи 5xx;
    - `auth` — невірний ключ, повторювати марно;
    - `mid_stream` — віддає `MOCK_BREAK_AFTER` фрагментів і обривається.
* **Випадковість і детермінізм.** `MOCK_FAILURE_RATE` — ймовірність
  збою на звертання (випадково). Для відтворюваних прогонів:
  `MOCK_FAIL_ATTEMPTS=N` — перші N спроб *кожної чернетки* збоять
  завжди, далі працює нормально (сценарій «повтор рятує»). Окремі
  запити можуть перекрити налаштування через
  `metadata["mock_failure"] = {"kind": ..., "rate": 1}`.
* **Скасування.** Усі очікування — через `abort.wait`, тож щойно шлюз
  ставить `Request.abort`, провайдер припиняє й підіймає
  `ProviderError("cancelled")`.

Значення читаються з `.env`; їхній зміст описано в `.env.example`.
"""

import os
import random
import re
import threading
import time
from typing import Iterator

from dotenv import load_dotenv

from .base import Chunk, ProviderError, Request, Result

load_dotenv()

FIRST_DELAY_MS = int(os.getenv("MOCK_FIRST_DELAY_MS", "600"))
DELAY_MS = int(os.getenv("MOCK_DELAY_MS", "40"))
FAILURE_RATE = float(os.getenv("MOCK_FAILURE_RATE", "0"))
FAILURE_KIND = os.getenv("MOCK_FAILURE_KIND", "unavailable")
FAIL_ATTEMPTS = int(os.getenv("MOCK_FAIL_ATTEMPTS", "0"))
RETRY_AFTER_S = float(os.getenv("MOCK_RETRY_AFTER_S", "2"))
BREAK_AFTER = int(os.getenv("MOCK_BREAK_AFTER", "5"))
HANG_S = float(os.getenv("MOCK_HANG_S", "120"))

KINDS = ("timeout", "rate_limit", "unavailable", "auth", "mid_stream")

_MESSAGES = {
    "timeout": "Провайдер не відповів вчасно.",
    "rate_limit": "Провайдер просить зачекати: забагато запитів.",
    "unavailable": "Провайдер тимчасово недоступний.",
    "auth": "Помилка доступу до моделі. Зверніться до адміністратора.",
}

# Скільки спроб кожної чернетки вже зроблено — для MOCK_FAIL_ATTEMPTS.
_calls: dict[str, int] = {}
_calls_lock = threading.Lock()
_CALLS_KEEP = 500


def _count_call(key: str) -> int:
    with _calls_lock:
        _calls[key] = _calls.get(key, 0) + 1
        if len(_calls) > _CALLS_KEEP:  # не росте безмежно
            for old in list(_calls)[: _CALLS_KEEP // 2]:
                _calls.pop(old, None)
        return _calls[key]


def _pieces(text: str, words: int = 2) -> list[str]:
    tokens = re.findall(r"\S+\s*", text)
    return ["".join(tokens[i:i + words]) for i in range(0, len(tokens), words)] or [text]


class MockProvider:
    """Провайдер без моделі: канонічні тексти, затримки, збої на вимогу."""

    name = "mock"
    model = "mock-1"

    def __init__(self, failure_rate: float | None = None, fail_attempts: int | None = None) -> None:
        # Запасний варіант створюється з failure_rate=0: він має
        # рятувати, а не збоїти разом з основним.
        self.failure_rate = FAILURE_RATE if failure_rate is None else failure_rate
        self.fail_attempts = FAIL_ATTEMPTS if fail_attempts is None else fail_attempts

    # -- допоміжне --------------------------------------------------

    @staticmethod
    def _sleep(request: Request, seconds: float) -> None:
        """Чекати, але перервати, щойно шлюз ставить `abort`."""
        if seconds <= 0:
            if request.abort is not None and request.abort.is_set():
                raise ProviderError("cancelled", "Перервано.")
            return
        abort = request.abort or threading.Event()
        if abort.wait(seconds):
            raise ProviderError("cancelled", "Перервано.")

    def _text(self, request: Request) -> str:
        from .. import samples  # лише дані зразків; жодного SDK

        sample = samples.get(request.metadata.get("request_id", ""))
        if sample and sample.get("reply_example"):
            return sample["reply_example"]
        return "[підставна відповідь] Дякуємо за звернення, оператор підготує відповідь вручну."

    def _planned_failure(self, request: Request) -> str | None:
        """Який збій відтворити в цьому звертанні (або None)."""
        override = request.metadata.get("mock_failure") or {}
        kind = override.get("kind", FAILURE_KIND)
        rate = override.get("rate", self.failure_rate)
        if kind not in KINDS:
            kind = "unavailable"
        key = str(request.metadata.get("draft_id") or request.metadata.get("request_id") or "-")
        n = _count_call(key)
        if self.fail_attempts and n <= self.fail_attempts:
            return kind
        if rate > 0 and random.random() < rate:
            return kind
        return None

    def _fail(self, kind: str, request: Request) -> None:
        """Збій до першого фрагмента."""
        if kind == "timeout":
            # Мовчимо довше за будь-який таймаут: шлюз має обірвати нас сам.
            self._sleep(request, HANG_S)
            raise ProviderError("timeout", _MESSAGES["timeout"], retryable=True,
                                detail="mock: hang elapsed")
        if kind == "rate_limit":
            self._sleep(request, 0.2)
            raise ProviderError("rate_limit", _MESSAGES["rate_limit"], retryable=True,
                                retry_after=RETRY_AFTER_S, detail="mock: 429")
        if kind == "auth":
            self._sleep(request, 0.2)
            raise ProviderError("auth", _MESSAGES["auth"], retryable=False, detail="mock: 401")
        self._sleep(request, 0.3)
        raise ProviderError("unavailable", _MESSAGES["unavailable"], retryable=True,
                            detail="mock: 503")

    # -- контракт ---------------------------------------------------

    def stream(self, request: Request) -> Iterator[Chunk]:
        """Віддавати текст фрагментами з затримкою `DELAY_MS` між ними."""
        planned = self._planned_failure(request)
        if planned and planned != "mid_stream":
            self._fail(planned, request)

        text = self._text(request)
        pieces = _pieces(text)
        self._sleep(request, FIRST_DELAY_MS / 1000)
        for i, piece in enumerate(pieces):
            if i:
                self._sleep(request, DELAY_MS / 1000)
            if planned == "mid_stream" and i >= BREAK_AFTER:
                raise ProviderError("unavailable", "З'єднання з провайдером обірвалося.",
                                    retryable=True, detail=f"mock: broken after {i} chunks")
            yield Chunk(text=piece)
        words = len(text.split())
        yield Chunk(text="", finish_reason="stop",
                    usage={"prompt_tokens": 0, "completion_tokens": words,
                           "total_tokens": words})

    def complete(self, request: Request) -> Result:
        """Повернути відповідь цілком — після тих самих затримок і з
        тими самими збоями, що й у `stream`."""
        started = time.monotonic()
        parts: list[str] = []
        usage = finish = None
        for chunk in self.stream(request):
            parts.append(chunk.text)
            usage = chunk.usage or usage
            finish = chunk.finish_reason or finish
        return Result(text="".join(parts), provider=self.name, model=self.model, usage=usage,
                      finish_reason=finish, elapsed=time.monotonic() - started)
