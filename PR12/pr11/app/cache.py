"""Persistent exact-response cache with TTL and bounded size."""
import os,json,time,hashlib,threading
from pathlib import Path
from dataclasses import asdict
from dotenv import load_dotenv
from .providers.base import Request,Result
load_dotenv(); ENABLED=os.getenv('CACHE_ENABLED','1')=='1'; TTL_SECONDS=float(os.getenv('CACHE_TTL_SECONDS','3600')); MAX_ENTRIES=int(os.getenv('CACHE_MAX_ENTRIES','500')); CACHE_FILE=os.getenv('CACHE_FILE','state/cache.json')
_lock=threading.RLock(); _stats={'hits':0,'misses':0,'bypassed':0}
def settings():return {'enabled':ENABLED,'ttl_seconds':TTL_SECONDS,'max_entries':MAX_ENTRIES,'file':CACHE_FILE or None}
def _load():
 if not CACHE_FILE:return {}
 try:return json.loads(Path(CACHE_FILE).read_text(encoding='utf-8'))
 except (OSError,ValueError):return {}
def _save(data):
 if not CACHE_FILE:return
 p=Path(CACHE_FILE);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');tmp.replace(p)
def make_key(request:Request,prompt_version:str)->str:
 payload={'messages':request.messages,'model':request.model or os.getenv('LLM_MODEL',''),'temperature':request.temperature,'max_tokens':request.max_tokens,'provider':os.getenv('LLM_PROVIDER','openai'),'prompt_version':prompt_version}
 raw=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'))
 return hashlib.sha256(raw.encode()).hexdigest()
def get(key:str)->Result|None:
 if not ENABLED:return None
 with _lock:
  data=_load();item=data.get(key)
  if not item:_stats['misses']+=1;return None
  if time.time()-item['created']>TTL_SECONDS:data.pop(key,None);_save(data);_stats['misses']+=1;return None
  _stats['hits']+=1;r=dict(item['result']);r['cached']=True;return Result(**r)
def put(key:str,result:Result)->None:
 if not ENABLED or result.cached:return
 with _lock:
  data=_load();data[key]={'created':time.time(),'result':asdict(result)}
  data={k:v for k,v in data.items() if time.time()-v.get('created',0)<=TTL_SECONDS}
  while len(data)>MAX_ENTRIES:data.pop(next(iter(data)))
  _save(data)
def bypassed():
 with _lock:_stats['bypassed']+=1
def stats():
 with _lock:
  data=_load();return {**_stats,'entries':len(data),'enabled':ENABLED}

# Single-flight locks: concurrent identical misses wait for the first result.
_flights: dict[str, threading.Lock] = {}
_flights_guard = threading.Lock()
def flight_lock(key: str) -> threading.Lock:
    with _flights_guard:
        lock = _flights.get(key)
        if lock is None:
            lock = threading.Lock()
            _flights[key] = lock
        return lock
