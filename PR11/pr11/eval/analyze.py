"""Аналіз журналу: повільні, невдалі й повторні виклики, витрати, кеш.

Запуск із папки pr11:

    python eval/analyze.py                       # logs/app.jsonl, усі розділи
    python eval/analyze.py logs/app.jsonl --since 2026-10-23 --section slow

Читання файлу, відбір за датою і допоміжні функції готові. Розділи
звіту — заготовки: поля, за якими вони працюють, — ваші, бо назви
подій і полів ви обрали, коли писали записи в шлюзі й фонових задачах.
`summary` працює на будь-якому журналі й показує, що в ньому взагалі є.

Орієнтир для кожного розділу — питання, на яке він відповідає; воно в
докстрінгу. Відповідь — числа й кілька рядків журналу, а не переказ.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path: Path, since: str | None = None) -> list[dict]:
    """Прочитати JSON lines. Рядки, які не розбираються, рахуються й
    пропускаються; `since` — дата або час у форматі ISO, від якого брати
    записи (порівнюється з полем `ts` як рядок)."""
    records: list[dict] = []
    broken = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                broken += 1
                continue
            if since and str(record.get("ts", "")) < since:
                continue
            records.append(record)
    if broken:
        print(f"(пропущено рядків, що не розбираються: {broken})", file=sys.stderr)
    return records


def percentile(values: list[float], p: float) -> float | None:
    """p-й процентиль (0 ≤ p ≤ 1) за найближчим рангом; `None` для
    порожнього списку."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p)))
    return ordered[index]


def group(records: list[dict], key: str) -> dict:
    """Згрупувати записи за значенням поля."""
    groups: dict = defaultdict(list)
    for record in records:
        groups[record.get(key)].append(record)
    return groups


def summary(records: list[dict]) -> None:
    """Що взагалі є в журналі: скільки записів, за який час, яких подій і
    рівнів скільки. Працює на будь-якому журналі."""
    if not records:
        print("записів немає")
        return
    stamps = [r.get("ts", "") for r in records]
    print(f"записів: {len(records)}; від {min(stamps)} до {max(stamps)}")
    print("рівні:", dict(Counter(r.get("level") for r in records)))
    print("події:")
    for event, count in Counter(r.get("event") for r in records).most_common():
        print(f"  {count:>6}  {event}")


def slow(records: list[dict]) -> None:
    """Які виклики найповільніші й що таке «повільно» в цих числах?

    Розподіл тривалості звертань до провайдера (медіана, p95, максимум),
    а не середнє; десятка найдовших із ідентифікаторами, провайдером,
    моделлю, спробою; де пройшов час — у черзі, до першого фрагмента чи
    в генерації; розріз за провайдером і годиною.
    """
    rows=[r for r in records if r.get('event')=='llm.attempt']
    vals=[float(r.get('duration_ms') or 0) for r in rows]
    if not vals: print('викликів моделі немає'); return
    import statistics
    print(f"викликів: {len(vals)}; медіана: {statistics.median(vals):.1f} ms; p95: {percentile(vals,.95):.1f} ms; максимум: {max(vals):.1f} ms")
    for r in sorted(rows,key=lambda x:float(x.get('duration_ms') or 0),reverse=True)[:10]:
        print(f" {r.get('duration_ms')} ms request={r.get('request_id')} draft={r.get('draft_id')} provider={r.get('provider')} model={r.get('model')} attempt={r.get('attempt')} queue={r.get('queue_ms')} first={r.get('first_token_ms')}")


def failed(records: list[dict]) -> None:
    """Що й чому не вдалося?

    Невдалі спроби за видом збою; остаточні збої чернеток; частка
    чернеток із невдалою першою спробою, які все ж завершилися успіхом
    (чи окупилися повтори); вартість спроб, які нічого не дали.
    """
    rows=[r for r in records if r.get('event')=='llm.attempt']; bad=[r for r in rows if r.get('status')=='failed']
    print(f"невдалі спроби: {len(bad)} / {len(rows)}; за видом: {dict(Counter(r.get('kind') or 'unknown' for r in bad))}")
    spent=sum(float(r.get('cost') or 0) for r in bad);print(f"вартість невдалих спроб: ${spent:.6f}")
    first=defaultdict(list);done=set()
    for r in rows:
        if r.get('draft_id'):first[r['draft_id']].append(r)
    for d,rs in first.items():
        if len(rs)>1 and rs[0].get('status')=='failed' and any(x.get('status')=='ok' for x in rs[1:]):done.add(d)
    candidates=sum(1 for rs in first.values() if rs and rs[0].get('status')=='failed')
    print(f"чернеток, врятованих повтором: {len(done)}; з невдалою першою спробою: {candidates}")


def repeated(records: list[dict]) -> None:
    """Що ми робили двічі?

    Той самий ключ кешу кілька разів за короткий час (кандидати на кеш
    і питання «чому»); чернетки з кількома спробами (повтори шлюзу);
    клієнти з серіями замовлень за хвилину.
    """
    attempts=[r for r in records if r.get('event')=='llm.attempt']; keys=defaultdict(list); drafts=defaultdict(list)
    for r in attempts:
        if r.get('cache_key'):keys[r['cache_key']].append(r)
        if r.get('draft_id'):drafts[r['draft_id']].append(r)
    print('повторені cache_key:',sum(1 for v in keys.values() if len(v)>1))
    for k,v in sorted(keys.items(),key=lambda kv:len(kv[1]),reverse=True)[:5]:
        if len(v)>1:print(f" key={k[:12]} calls={len(v)} drafts={','.join(str(x.get('draft_id')) for x in v)}")
    multi={k:v for k,v in drafts.items() if len(v)>1}
    print(f"чернеток із кількома спробами: {len(multi)}")
    for k,v in list(multi.items())[:10]:print(f" draft={k}: attempts={len(v)} kinds={[x.get('kind') for x in v]}")


def spend(records: list[dict]) -> None:
    """Скільки це коштувало?

    Вартість і токени за день, за модель, за клієнта; середня вартість
    чернетки; частка вартості, яка припала на спроби зі збоєм і на
    запасний варіант; скільки записів без вартості або з оцінкою.
    """
    rows=[r for r in records if r.get('event')=='llm.attempt']; days=defaultdict(float);models=defaultdict(float);clients=defaultdict(float);ti=to=0;estimated=missing=0
    for r in rows:
        c=r.get('cost');
        if c is None:missing+=1;continue
        c=float(c);day=str(r.get('ts',''))[:10];days[day]+=c;models[r.get('model')]+=c;clients[r.get('client')]+=c
        ti+=int(r.get('tokens_input') or 0);to+=int(r.get('tokens_output') or 0);estimated+=bool(r.get('cost_estimated'))
    print('витрати за день:',{k:round(v,6) for k,v in days.items()});print('за моделлю:',{str(k):round(v,6) for k,v in models.items()})
    print('за клієнтом:',{str(k):round(v,6) for k,v in clients.items()});print(f"токени input={ti}, output={to}; оцінок={estimated}; без вартості={missing}")


def cache_report(records: list[dict]) -> None:
    """Що дав кеш?

    Влучання, промахи, обходи (`fresh`) і частка влучань; скільки
    звертань до провайдера й грошей заощаджено; які звернення
    повторювалися найчастіше.
    """
    hits=[r for r in records if r.get('event')=='llm.cache_hit']; attempts=[r for r in records if r.get('event')=='llm.attempt']; bypass=sum(1 for r in records if r.get('event')=='llm.cache_bypass')
    keys=Counter(r.get('cache_key') for r in attempts if r.get('cache_key'));print(f"влучання: {len(hits)}; виклики провайдера: {len(attempts)}; обходи fresh (за журналом): {bypass}; частка кеш-влучань серед завершених запитів: {len(hits)/(len(hits)+len(attempts)) if hits or attempts else 0:.1%}")
    print('найчастіші ключі:',[(k[:12],v) for k,v in keys.most_common(5)])


SECTIONS = {
    "summary": summary,
    "slow": slow,
    "failed": failed,
    "repeated": repeated,
    "spend": spend,
    "cache": cache_report,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", nargs="?", default="logs/app.jsonl")
    parser.add_argument("--since", help="брати записи від цієї дати/часу (ISO), наприклад 2026-10-23")
    parser.add_argument("--section", choices=["all", *SECTIONS], default="all")
    args = parser.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"журналу немає: {path}", file=sys.stderr)
        return 1
    records = load(path, args.since)

    names = list(SECTIONS) if args.section == "all" else [args.section]
    for name in names:
        print(f"\n=== {name}")
        try:
            SECTIONS[name](records)
        except Exception as exc:
            print(f"(помилка розділу: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
