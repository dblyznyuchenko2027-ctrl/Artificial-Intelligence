"""Прогін сценаріїв стійкості з eval/README.md без браузера й мережі.

Кожен сценарій виконується в окремому процесі зі своїм набором
налаштувань (модулі читають `.env` при імпорті), працює з реальними
`jobs` → `gateway` → `providers.mock` і друкує таймлайн подій. Таймаути
в сценаріях зменшено, щоб прогін тривав хвилину, а не годину; співвідношення
між ними — як у `.env.example`.

    python eval/run_scenarios.py            # усі
    python eval/run_scenarios.py 4 9b       # вибрані
    python eval/run_scenarios.py --json out.json

Не охоплює: сценарії 16 (потрібен ключ і мережа), 18 (поведінка сторінки)
і HTTP-рівень сценаріїв 11–15 — їх перевіряють на запущеному застосунку.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BASE = {
    "LLM_PROVIDER": "mock", "LLM_FALLBACK": "none", "LLM_API_KEY": "",
    "LLM_TIMEOUT": "4", "LLM_IDLE_TIMEOUT": "1.5", "LLM_TOTAL_TIMEOUT": "12",
    "LLM_MAX_ATTEMPTS": "3", "LLM_BACKOFF_BASE": "0.5", "LLM_BACKOFF_MAX": "4",
    "LLM_MAX_CONCURRENT": "2", "LLM_QUEUE_WAIT": "20", "LLM_MIN_ATTEMPT": "1.5",
    "MOCK_FIRST_DELAY_MS": "300", "MOCK_DELAY_MS": "30", "MOCK_FAILURE_RATE": "0",
    "MOCK_FAILURE_KIND": "unavailable", "MOCK_FAIL_ATTEMPTS": "0", "MOCK_RETRY_AFTER_S": "2",
    "MOCK_BREAK_AFTER": "5", "MOCK_HANG_S": "60",
}

# id: (назва, перевизначення .env, режим)
SCENARIOS = {
    "1":  ("Звичайна чернетка", {}, "run"),
    "2":  ("Таймаут спроби", {"MOCK_FIRST_DELAY_MS": "9000", "LLM_TIMEOUT": "2",
                              "LLM_TOTAL_TIMEOUT": "20"}, "run"),
    "3":  ("Таймаут бездіяльності", {"MOCK_DELAY_MS": "2500", "LLM_IDLE_TIMEOUT": "1",
                                     "LLM_TIMEOUT": "10", "LLM_TOTAL_TIMEOUT": "20"}, "run"),
    "4":  ("Тимчасовий збій, повтор рятує", {"MOCK_FAIL_ATTEMPTS": "2"}, "run"),
    "5":  ("Збій назавжди", {"MOCK_FAILURE_RATE": "1"}, "run"),
    "6":  ("Запасний варіант", {"MOCK_FAILURE_RATE": "1", "LLM_FALLBACK": "mock"}, "run"),
    "6b": ("Запасний: основний без ключа", {"LLM_PROVIDER": "openai", "LLM_FALLBACK": "mock"}, "run"),
    "7":  ("Збій без повтору (auth)", {"MOCK_FAILURE_RATE": "1", "MOCK_FAILURE_KIND": "auth"}, "run"),
    "8":  ("429, Retry-After вміщується", {"MOCK_FAILURE_RATE": "1", "MOCK_FAILURE_KIND": "rate_limit",
                                           "MOCK_RETRY_AFTER_S": "2"}, "run"),
    "8b": ("429, Retry-After 90 с > бюджет", {"MOCK_FAILURE_RATE": "1", "MOCK_FAILURE_KIND": "rate_limit",
                                              "MOCK_RETRY_AFTER_S": "90"}, "run"),
    "9":  ("Обрив посеред потоку, повтор рятує", {"MOCK_FAIL_ATTEMPTS": "1",
                                                  "MOCK_FAILURE_KIND": "mid_stream"}, "run"),
    "9b": ("Обрив посеред потоку назавжди", {"MOCK_FAILURE_RATE": "1",
                                             "MOCK_FAILURE_KIND": "mid_stream"}, "run"),
    "10": ("Бюджет часу менший за спроби", {"MOCK_FIRST_DELAY_MS": "9000", "LLM_TIMEOUT": "3",
                                            "LLM_TOTAL_TIMEOUT": "5", "LLM_MAX_ATTEMPTS": "5"}, "run"),
    "11": ("Скасування під час генерації", {"MOCK_DELAY_MS": "150"}, "cancel_during"),
    "12": ("Скасування завершеної", {}, "cancel_after"),
    "13": ("Перепідключення (відтворення подій)", {"MOCK_DELAY_MS": "100"}, "replay"),
    "14": ("П'ять поспіль, одночасність 2", {"MOCK_FIRST_DELAY_MS": "1000"}, "burst"),
    "15": ("Ліміт на клієнта", {}, "ratelimit"),
    "17": ("Без ключа, mock", {}, "run"),
    "17b": ("Без ключа, openai, без запасного", {"LLM_PROVIDER": "openai"}, "run"),
}


def worker(mode: str) -> None:
    sys.path.insert(0, str(ROOT))
    import logging
    logging.basicConfig(level=logging.WARNING, format="    log: %(name)s %(message)s")
    from app import jobs, limits, samples

    sample = samples.get("d01")
    out: dict = {}

    def timeline(draft, t0):
        lines = []
        for ev in list(draft.events):
            d = ev["data"]
            if ev["event"] == "delta":
                continue
            short = {k: v for k, v in d.items() if k in
                     ("status", "attempt", "provider", "fallback", "detail", "pause", "kind",
                      "message", "retryable", "partial", "attempts")}
            lines.append(f'{ev["event"]} {json.dumps(short, ensure_ascii=False)}')
        return lines

    def wait(draft, limit=90):
        end = time.monotonic() + limit
        while draft.status not in jobs.TERMINAL and time.monotonic() < end:
            time.sleep(0.05)

    def summary(draft, t0):
        deltas = sum(1 for e in draft.events if e["event"] == "delta")
        return {"status": draft.status, "attempts": draft.attempts, "fallback": draft.fallback,
                "provider": draft.provider, "text_len": len(draft.text), "deltas": deltas,
                "elapsed": draft.elapsed, "error": draft.error, "warnings": draft.warnings,
                "total_wall": round(time.monotonic() - t0, 2), "timeline": timeline(draft, t0)}

    if mode == "run":
        t0 = time.monotonic()
        d = jobs.start(sample, "eval")
        wait(d)
        out = summary(d, t0)
    elif mode == "cancel_during":
        t0 = time.monotonic()
        d = jobs.start(sample, "eval")
        time.sleep(1.2)
        before = len(d.text)
        jobs.cancel(d.draft_id)
        time.sleep(1.5)  # потік мав зупинитися; текст не росте
        out = summary(d, t0)
        out["text_at_cancel"] = before
        out["text_after_1.5s"] = len(d.text)
        try:
            jobs.cancel(d.draft_id)
        except jobs.AlreadyFinished as exc:
            out["second_cancel"] = f"AlreadyFinished: {exc}"
    elif mode == "cancel_after":
        t0 = time.monotonic()
        d = jobs.start(sample, "eval")
        wait(d)
        out = summary(d, t0)
        try:
            jobs.cancel(d.draft_id)
            out["cancel"] = "ПРИЙНЯТО (помилка!)"
        except jobs.AlreadyFinished as exc:
            out["cancel"] = f"AlreadyFinished (409): {exc}"
        try:
            jobs.cancel("nonexistent")
        except jobs.NotFound:
            out["cancel_unknown"] = "NotFound (404)"
        out["status_after"] = d.status
    elif mode == "replay":
        t0 = time.monotonic()
        d = jobs.start(sample, "eval")
        time.sleep(1.0)  # «перезавантаження» посеред генерації
        mid = len(d.events)
        got = []
        for ev in jobs.subscribe(d.draft_id, 0):
            if ev is not None:
                got.append(ev)
        resumed = [ev["id"] for ev in jobs.subscribe(d.draft_id, 10) if ev is not None]
        out = summary(d, t0)
        out["events_when_reopened"] = mid
        out["events_replayed"] = len(got)
        out["replay_text_equals"] = "".join(e["data"]["text"] for e in got if e["event"] == "delta") == d.text
        out["last_event_id_resume_first_id"] = resumed[0] if resumed else None
    elif mode == "burst":
        t0 = time.monotonic()
        ds = [jobs.start(sample, "eval") for _ in range(5)]
        time.sleep(0.5)
        out["after_0.5s"] = [d.status for d in ds]
        for d in ds:
            wait(d)
        out["wall_all_done"] = round(time.monotonic() - t0, 2)
        out["drafts"] = [{"status": d.status, "first_token": d.elapsed.get("first_token"),
                          "total": d.elapsed.get("total"),
                          "queue_details": [e["data"]["detail"] for e in d.events
                                            if e["event"] == "status"
                                            and e["data"].get("status") == "queued"
                                            and e["data"].get("detail") != "у черзі"]}
                         for d in ds]
    elif mode == "ratelimit":
        ok, denied = 0, []
        for _ in range(limits.PER_MINUTE + 3):
            try:
                limits.acquire("eval")
                ok += 1
            except limits.RateLimited as exc:
                denied.append(round(exc.retry_after, 1))
        out = {"accepted": ok, "denied": len(denied), "retry_after": denied,
               "other_client_ok": True}
        try:
            limits.acquire("someone-else")
        except limits.RateLimited:
            out["other_client_ok"] = False
        # годинник: через Retry-After запит проходить
        fake = [time.monotonic()]
        limits._clock = lambda: fake[0]
        limits._hits.clear()
        for _ in range(limits.PER_MINUTE):
            limits.acquire("t")
        try:
            limits.acquire("t")
        except limits.RateLimited as exc:
            wait_s = exc.retry_after
        fake[0] += wait_s - 0.01
        try:
            limits.acquire("t")
            out["passes_just_before_retry_after"] = True
        except limits.RateLimited:
            out["passes_just_before_retry_after"] = False
        fake[0] += 0.02
        try:
            limits.acquire("t")
            out["passes_after_retry_after"] = True
        except limits.RateLimited:
            out["passes_after_retry_after"] = False
    print("RESULT " + json.dumps(out, ensure_ascii=False, default=str))


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--worker":
        worker(args[1])
        return
    json_path = None
    if "--json" in args:
        i = args.index("--json")
        json_path = args[i + 1]
        del args[i:i + 2]
    wanted = args or list(SCENARIOS)
    results = {}
    for sid in wanted:
        title, overrides, mode = SCENARIOS[sid]
        env = {**os.environ, **BASE, **overrides}
        print(f"\n=== Сценарій {sid}: {title} ===")
        print("  налаштування:", {k: v for k, v in overrides.items()} or "(базові)")
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, __file__, "--worker", mode], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        for line in proc.stderr.splitlines():
            print(line)
        res = None
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                res = json.loads(line[7:])
        if res is None:
            print("  !! немає результату:", proc.stdout[-500:])
            continue
        results[sid] = {"title": title, "env": overrides, "wall": round(time.monotonic() - t0, 1),
                        **res}
        for k, v in res.items():
            if k == "timeline":
                for i, row in enumerate(v, 1):
                    print(f"    {i:2d}. {row}")
            else:
                print(f"  {k}: {v}")
    if json_path:
        Path(json_path).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
