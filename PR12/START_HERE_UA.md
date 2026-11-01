# Як запустити ПР12

1. Розпакуйте архів у корінь репозиторію курсу так, щоб `pr11/` і `pr12/` були поруч.
2. Відкрийте `pr12/.env`. Для безключової перевірки залиште `LLM_PROVIDER=mock`.
3. Встановіть залежності: `pip install -r pr12/requirements.txt`.
4. З кореня репозиторію виконайте:
   `python pr12/eval/run.py --target drafts --cases pr12/eval/cases/drafts.json --k 1 --no-judge`
5. Для реального оцінювання налаштуйте провайдера в `pr11/.env`, а модель-суддю — у `pr12/.env` (`JUDGE_BASE_URL`, `JUDGE_API_KEY`, `JUDGE_MODEL`). Суддя має бути іншою моделлю.
6. Для справжнього базового прогону перейдіть у `pr12/` і виконайте:
   `python eval/run.py --target drafts --cases eval/cases/drafts.json --k 3 --label "baseline"`.
7. Додайте свою ПР8 у `pr8/` поруч із `pr11/` та `pr12/`, потім:
   `python eval/run.py --target agent --cases eval/cases/agent.json --k 1 --no-judge`.
8. Відкладений набір запускайте один раз наприкінці: `python eval/run.py --target drafts --cases eval/cases/drafts.heldout.json --k 3 --label "heldout"`.

**Важливо:** архів ПР11 містив `.env` із налаштуваннями доступу. Його не включено до готового архіву з міркувань безпеки. Секрети не вставляйте в код і не надсилайте у звіті.
