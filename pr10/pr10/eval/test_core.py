"""Швидкі перевірки ядра без мережі, ключа й FastAPI:

    python eval/test_core.py

1. гонка «скасування ↔ завершення»: скасована чернетка ніколи не `done`;
2. `drafting.check`: що попереджає, а що ні;
3. класифікація збоїв справжнього провайдера — на заглушці SDK `openai`
   (класи повторюють ієрархію справжнього SDK; перевіряється логіка
   відображення, а не сам SDK — із справжнім його треба прогнати окремо).
"""

import os
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update({"LLM_PROVIDER": "mock", "LLM_API_KEY": "", "MOCK_FIRST_DELAY_MS": "20",
                   "MOCK_DELAY_MS": "5", "LLM_QUEUE_WAIT": "5", "LLM_MAX_CONCURRENT": "8"})

from app import drafting, jobs, samples  # noqa: E402


def test_cancel_race() -> None:
    sample = samples.get("d01")
    bad = 0
    for i in range(60):
        d = jobs.start(sample, "race")
        time.sleep(0.02 + (i % 12) * 0.02)  # від «ще не почалось» до «майже закінчилось»
        try:
            jobs.cancel(d.draft_id)
            accepted = True
        except jobs.AlreadyFinished:
            accepted = False
        time.sleep(0.25)
        if accepted and d.status != "cancelled":
            bad += 1
        assert d.status in jobs.TERMINAL, d.status
        ends = [e["event"] for e in d.events if e["event"] in ("done", "error", "cancelled")]
        assert len(ends) == 1, ends  # рівно одна кінцева подія
        if accepted:
            assert "done" not in ends
    assert bad == 0, bad
    print("cancel race: ok (60 прогонів, скасована чернетка не стала done)")


def test_check() -> None:
    s = samples.get("d01")
    good = samples.get("d01")["reply_example"]
    assert drafting.check(good, s) == [], drafting.check(good, s)
    w = drafting.check("Добрий день! Заявку оформлено, привезіть товар на склад до 20 жовтня, "
                       "повернемо 5000 грн. З повагою, підтримка.", s)
    assert any("R-5001" in x for x in w), w
    assert any("числа" in x for x in w), w
    assert drafting.check("", s)
    assert any("обірвана" in x for x in drafting.check(good[:-30], s))
    print("drafting.check: ok")


def test_openai_mapping() -> None:
    oa = types.ModuleType("openai")

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        def __init__(self, msg, status, headers=None):
            super().__init__(msg)
            self.status_code = status
            self.response = types.SimpleNamespace(headers=headers or {})

    names = ["RateLimitError", "AuthenticationError", "PermissionDeniedError", "BadRequestError",
             "NotFoundError", "UnprocessableEntityError", "InternalServerError"]
    oa.APIError, oa.APIConnectionError, oa.APITimeoutError, oa.APIStatusError = (
        APIError, APIConnectionError, APITimeoutError, APIStatusError)
    for n in names:
        setattr(oa, n, type(n, (APIStatusError,), {}))
    oa.OpenAI = lambda **kw: types.SimpleNamespace(kw=kw)
    httpx = types.ModuleType("httpx")
    httpx.Timeout = lambda *a, **k: ("timeout", a, k)
    sys.modules["openai"], sys.modules["httpx"] = oa, httpx

    from app.providers.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider("http://x", "sk-SECRET123", "m", 5)
    assert p._client.kw["max_retries"] == 0
    cases = [
        (APITimeoutError("t"), "timeout", True),
        (APIConnectionError("c"), "unavailable", True),
        (oa.RateLimitError("r sk-SECRET123", 429, {"retry-after": "7"}), "rate_limit", True),
        (oa.InternalServerError("s", 503), "unavailable", True),
        (oa.AuthenticationError("a", 401), "auth", False),
        (oa.BadRequestError("b", 400), "bad_request", False),
        (oa.NotFoundError("n", 404), "bad_request", False),
        (APIStatusError("x", 408), "timeout", True),
        (RuntimeError("zzz"), "unknown", False),
    ]
    for exc, kind, retry in cases:
        e = p._map_error(exc)
        assert (e.kind, e.retryable) == (kind, retry), (exc, e.kind, e.retryable)
        assert "sk-SECRET123" not in (e.detail or "") + e.message
    assert p._map_error(cases[2][0]).retry_after == 7.0
    print("openai error mapping: ok (на заглушці SDK)")


if __name__ == "__main__":
    test_check()
    test_openai_mapping()
    test_cancel_race()
