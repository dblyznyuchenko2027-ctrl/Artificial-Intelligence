"""Справжній провайдер: OpenAI-сумісний API — той самий, що в ПР3–ПР9.

Єдиний модуль застосунку, який імпортує SDK `openai`. Його справа —
перетворити наш `Request` на виклик API, а відповідь і збої API — на
`Result`, `Chunk` і `ProviderError`. Що робити зі збоєм — повторити,
зачекати, перейти на запасний варіант — тут не вирішується: це справа
шлюзу.

Рішення:

* **Класифікація збоїв** (`_map_error`):

  | SDK                                         | kind          | retryable |
  |---------------------------------------------|---------------|-----------|
  | `APITimeoutError`                           | `timeout`     | так       |
  | `APIConnectionError`                        | `unavailable` | так       |
  | `RateLimitError` (429)                      | `rate_limit`  | так       |
  | 5xx, 408, 409                               | `unavailable` / `timeout` | так |
  | `AuthenticationError` (401), 403            | `auth`        | ні        |
  | `BadRequestError` (400), 404, 422, інші 4xx | `bad_request` | ні        |
  | усе інше                                    | `unknown`     | ні        |

  `APITimeoutError` — підклас `APIConnectionError`, тому перевіряється
  першим.
* **Таймаут**: `httpx.Timeout(timeout, connect=…)` на клієнті. `read` —
  це пауза між байтами, тож він же виступає запобіжником від
  «зависання» посеред потоку. Точні таймаути на спробу, бездіяльність і
  весь запит тримає шлюз; тут — лише страховка, щоб робочий потік не
  висів вічно.
* **`max_retries=0`**: повторює шлюз, бо тільки він бачить бюджет
  часу, журнал спроб і запасний варіант. Якщо лишити SDK його два
  мовчазні повтори, спроба, яку шлюз вважає однією, насправді триватиме
  втричі довше, а `Retry-After` SDK прочитає й виконає, не спитавши
  бюджету.
* **Streaming**: `stream=True`, `stream_options={"include_usage": True}`
  (якщо API не знає такої опції — одна спроба без неї). Потік
  закривається у `finally`, тож якщо споживач перестав читати —
  генерація на боці провайдера зупиняється, токени, яких ми не
  дочитали, здебільшого не оплачуються.
* **Що куди**: у `message` — готовий текст для людини; у `detail` — клас
  винятку, код статусу й коротке повідомлення SDK, очищене від ключа.
  Текст запиту й ключ не потрапляють нікуди.
"""

import time
from typing import Iterator

import httpx
import openai

from .base import Chunk, ProviderError, Request, Result

_MESSAGES = {
    "timeout": "Провайдер не відповів вчасно.",
    "rate_limit": "Провайдер просить зачекати: забагато запитів.",
    "unavailable": "Провайдер тимчасово недоступний.",
    "auth": "Помилка доступу до моделі. Зверніться до адміністратора.",
    "bad_request": "Провайдер не прийняв запит.",
    "unknown": "Невідомий збій під час звернення до моделі.",
}


def _retry_after(response) -> float | None:
    """Скільки секунд просив зачекати провайдер: `retry-after-ms` або
    `retry-after` (секунди; дата в HTTP-форматі — не підтримується)."""
    if response is None:
        return None
    headers = response.headers
    try:
        if headers.get("retry-after-ms"):
            return max(0.0, float(headers["retry-after-ms"]) / 1000)
        if headers.get("retry-after"):
            return max(0.0, float(headers["retry-after"]))
    except (TypeError, ValueError):
        return None
    return None


class OpenAICompatProvider:
    """Провайдер поверх OpenAI-сумісного API."""

    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            max_retries=0,
        )

    # -- збої -------------------------------------------------------

    def _scrub(self, text: str) -> str:
        if self.api_key:
            text = text.replace(self.api_key, "***")
        return text[:300]

    def _map_error(self, exc: Exception) -> ProviderError:
        """Виняток SDK → `ProviderError`. Єдине місце, що знає ці класи."""
        status = getattr(exc, "status_code", None)
        detail = self._scrub(f"{type(exc).__name__} status={status}: {exc}")
        if isinstance(exc, openai.APITimeoutError):
            kind, retry = "timeout", True
        elif isinstance(exc, openai.APIConnectionError):
            kind, retry = "unavailable", True
        elif isinstance(exc, openai.RateLimitError):
            return ProviderError("rate_limit", _MESSAGES["rate_limit"], retryable=True,
                                 retry_after=_retry_after(getattr(exc, "response", None)),
                                 detail=detail)
        elif isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            kind, retry = "auth", False
        elif isinstance(exc, openai.APIStatusError):
            if status is not None and status >= 500:
                kind, retry = "unavailable", True
            elif status == 408:
                kind, retry = "timeout", True
            elif status == 409:
                kind, retry = "unavailable", True
            else:  # 400, 404 (немає моделі), 422 тощо
                kind, retry = "bad_request", False
        else:
            kind, retry = "unknown", False
        return ProviderError(kind, _MESSAGES[kind], retryable=retry, detail=detail)

    # -- виклики ----------------------------------------------------

    def _params(self, request: Request) -> dict:
        return {
            "model": request.model or self.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }

    def complete(self, request: Request) -> Result:
        """Отримати відповідь цілком."""
        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(**self._params(request))
        except Exception as exc:  # noqa: BLE001 — усе зводимо до контракту
            raise self._map_error(exc) from None
        choice = response.choices[0]
        usage = response.usage.model_dump() if response.usage else None
        return Result(text=choice.message.content or "", provider=self.name,
                      model=response.model or self._params(request)["model"], usage=usage,
                      finish_reason=choice.finish_reason, elapsed=time.monotonic() - started)

    def _open_stream(self, request: Request):
        params = self._params(request)
        try:
            return self._client.chat.completions.create(
                **params, stream=True, stream_options={"include_usage": True})
        except openai.BadRequestError as exc:
            if "stream_options" not in str(exc):
                raise
            return self._client.chat.completions.create(**params, stream=True)

    def stream(self, request: Request) -> Iterator[Chunk]:
        """Віддавати фрагменти відповіді в міру надходження."""
        stream = None
        try:
            stream = self._open_stream(request)
            for event in stream:
                if request.abort is not None and request.abort.is_set():
                    raise ProviderError("cancelled", "Перервано.")
                usage = event.usage.model_dump() if getattr(event, "usage", None) else None
                if not event.choices:  # завершальний фрагмент лише з usage
                    if usage:
                        yield Chunk(text="", usage=usage)
                    continue
                choice = event.choices[0]
                text = (choice.delta.content or "") if choice.delta else ""
                if text or choice.finish_reason or usage:
                    yield Chunk(text=text, usage=usage, finish_reason=choice.finish_reason)
        except ProviderError:
            raise
        except GeneratorExit:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc) from None
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
