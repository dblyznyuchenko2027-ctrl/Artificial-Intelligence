"""Persistent daily USD budget (local timezone)."""
import os,json,threading,datetime
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(); DAILY_USD=float(os.getenv('BUDGET_DAILY_USD','1.00')); WARN_AT=float(os.getenv('BUDGET_WARN_AT','0.8')); BUDGET_FILE=os.getenv('BUDGET_FILE','state/budget.json'); _lock=threading.RLock()
class BudgetExceeded(Exception):
 def __init__(self,spent,limit,resets_at):
  self.spent=spent;self.limit=limit;self.resets_at=resets_at;super().__init__(f'Денний бюджет вичерпано ({spent:.4f}/{limit:.2f} USD). Відновлення: {resets_at}')
def settings():return {'daily_usd':DAILY_USD,'warn_at':WARN_AT,'file':BUDGET_FILE}
def _day():return datetime.date.today().isoformat()
def _read():
 try:d=json.loads(Path(BUDGET_FILE).read_text());return d if d.get('day')==_day() else {'day':_day(),'spent':0.0,'reserved':0.0}
 except (OSError,ValueError):return {'day':_day(),'spent':0.0,'reserved':0.0}
def _write(d):
 p=Path(BUDGET_FILE);p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix('.tmp');t.write_text(json.dumps(d));t.replace(p)
def _reset():return (datetime.datetime.combine(datetime.date.today()+datetime.timedelta(days=1),datetime.time()).astimezone().isoformat())
def check(expected_cost=None):
 with _lock:
  d=_read();spent=float(d['spent']);reserved=float(d.get('reserved',0))
  if spent+reserved+(expected_cost or 0)>DAILY_USD+1e-12 or spent+reserved>=DAILY_USD:raise BudgetExceeded(spent+reserved,DAILY_USD,_reset())
def reserve(expected_cost):
 expected=max(0.0,float(expected_cost or 0))
 with _lock:
  d=_read();spent=float(d['spent']);reserved=float(d.get('reserved',0))
  if spent+reserved+expected>DAILY_USD+1e-12:raise BudgetExceeded(spent+reserved,DAILY_USD,_reset())
  d['reserved']=round(reserved+expected,8);_write(d);return expected
def settle(reservation,cost):
 with _lock:
  d=_read();d['reserved']=round(max(0,float(d.get('reserved',0))-float(reservation or 0)),8)
  d['spent']=round(float(d['spent'])+max(0,float(cost or 0)),8);_write(d)
def record(cost): settle(0,cost)
def status():
 with _lock:
  d=_read();spent=float(d['spent']);reserved=float(d.get('reserved',0));return {'day':d['day'],'spent':spent,'reserved':reserved,'limit':DAILY_USD,'remaining':max(0,round(DAILY_USD-spent-reserved,8)),'warning':spent+reserved>=DAILY_USD*WARN_AT,'exceeded':spent+reserved>=DAILY_USD,'resets_at':_reset()}

# A reservation cannot represent a live request after process restart.
# Recover it so an interrupted previous process does not lock today's budget.
with _lock:
    _recovered = _read()
    if float(_recovered.get('reserved', 0) or 0) > 0:
        _recovered['reserved'] = 0.0
        _write(_recovered)
