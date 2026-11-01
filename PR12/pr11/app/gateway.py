"""LLM gateway: retries, fallback, budget, cache and structured telemetry."""
import os,time,logging,threading,hashlib,queue
from typing import Callable
from dotenv import load_dotenv
from .providers.base import Provider,Request,Result,ProviderError,Chunk
from . import cache,budget,costs
load_dotenv();log=logging.getLogger('app.gateway')
PROVIDER=os.getenv('LLM_PROVIDER','mock');FALLBACK=os.getenv('LLM_FALLBACK','none');TIMEOUT=float(os.getenv('LLM_TIMEOUT','20'));IDLE_TIMEOUT=float(os.getenv('LLM_IDLE_TIMEOUT','8'));TOTAL_TIMEOUT=float(os.getenv('LLM_TOTAL_TIMEOUT','45'));MAX_ATTEMPTS=int(os.getenv('LLM_MAX_ATTEMPTS','3'));BACKOFF_BASE=float(os.getenv('LLM_BACKOFF_BASE','.3'));BACKOFF_MAX=float(os.getenv('LLM_BACKOFF_MAX','3'));MAX_CONCURRENT=int(os.getenv('LLM_MAX_CONCURRENT','2'));MODEL=os.getenv('LLM_MODEL','');TEMPERATURE=float(os.getenv('LLM_TEMPERATURE','.3'));MAX_TOKENS=int(os.getenv('LLM_MAX_TOKENS','600'));_sem=threading.BoundedSemaphore(MAX_CONCURRENT)
def settings():return {'provider':PROVIDER,'fallback':FALLBACK,'model':MODEL,'timeout':TIMEOUT,'idle_timeout':IDLE_TIMEOUT,'total_timeout':TOTAL_TIMEOUT,'max_attempts':MAX_ATTEMPTS,'max_concurrent':MAX_CONCURRENT}
def make_provider(name):
 if name=='mock':
  from .providers.mock import MockProvider
  return MockProvider()
 from .providers.openai_compat import OpenAICompatProvider
  
 api_key=os.getenv('LLM_API_KEY','');base=os.getenv('LLM_BASE_URL','https://api.openai.com/v1')
 if not api_key:raise ProviderError('auth','Не налаштовано LLM_API_KEY',retryable=False)
 return OpenAICompatProvider(base,api_key,os.getenv('LLM_MODEL','gpt-4o-mini'),TIMEOUT)
def _generate(request:Request,on_delta:Callable[[str],None],on_status:Callable[[dict],None],is_cancelled:Callable[[],bool])->Result:
 start=time.perf_counter();rid=request.metadata.get('http_request_id') or request.metadata.get('request_id');did=request.metadata.get('draft_id');pv=request.metadata.get('prompt_version','unknown');key=cache.make_key(request,pv)
 if request.metadata.get('fresh'):
  cache.bypassed();log.info('llm.cache_bypass',extra={'request_id':rid,'draft_id':did,'client':request.metadata.get('client'),'prompt_version':pv})
 else:
  hit=cache.get(key)
  if hit:
   hit.cached=True;hit.cost=0.0;hit.cost_estimated=False;on_delta(hit.text);log.info('llm.cache_hit',extra={'request_id':rid,'draft_id':did,'cache_key':key,'cached':True,'status':'ok','cost':0.0,'prompt_version':pv});return hit
 providers=[PROVIDER]+([FALLBACK] if FALLBACK not in ('none','',PROVIDER) else [])
 last=None;attempt=0;total_cost=0.0;fallback_used=False
 for pname in providers:
  provider=make_provider(pname)
  for n in range(1,MAX_ATTEMPTS+1):
   if is_cancelled():raise ProviderError('cancelled','Генерацію скасовано')
   if time.perf_counter()-start>TOTAL_TIMEOUT:raise ProviderError('timeout','Перевищено загальний час очікування',retryable=False)
   attempt+=1;queued=time.perf_counter();acquired=_sem.acquire(timeout=max(0.1,TOTAL_TIMEOUT-(time.perf_counter()-start)));queue_ms=(time.perf_counter()-queued)*1000
   if not acquired: raise ProviderError('timeout','Перевищено час очікування в черзі',retryable=True)
   reservation=0.0
   try:
    expected_in=max(1,sum(len(m.get('content','')) for m in request.messages)//3)
    expected_cost,_=costs.estimate({'prompt_tokens':expected_in,'completion_tokens':request.max_tokens},provider=pname,model=MODEL)
    try: reservation=budget.reserve(expected_cost or 0.0)
    except budget.BudgetExceeded as exc: raise ProviderError('budget',str(exc),retryable=False,detail=exc.resets_at) from exc
    on_status({'status':'running','attempt':attempt,'provider':pname,'fallback':fallback_used})
    t0=time.perf_counter();first=None;pieces=[];usage=None;finish=None
    for chunk in _stream_with_timeouts(provider.stream(request),start):
     if is_cancelled():raise ProviderError('cancelled','Генерацію скасовано')
     if first is None and chunk.text:first=(time.perf_counter()-t0)*1000
     if chunk.text:pieces.append(chunk.text);on_delta(chunk.text)
     if chunk.usage:usage=chunk.usage
     if chunk.finish_reason:finish=chunk.finish_reason
    text=''.join(pieces);elapsed=time.perf_counter()-t0
    cost,estimated=costs.estimate(usage,provider=pname,model=getattr(provider,'model',MODEL),text=text)
    cost=cost or 0.0;budget.settle(reservation,cost);reservation=0.0;total_cost+=cost
    result=Result(text,pname,getattr(provider,'model',MODEL or 'unknown'),usage,finish,elapsed,cost,estimated,False)
    cache.put(key,result)
    log.info('llm.attempt',extra={'request_id':rid,'draft_id':did,'client':request.metadata.get('client'),'provider':pname,'model':result.model,'prompt_version':pv,'attempt':attempt,'queue_ms':round(queue_ms,1),'first_token_ms':round(first or 0,1),'duration_ms':round(elapsed*1000,1),'status':'ok','kind':None,'fallback':fallback_used,'tokens_input':(usage or {}).get('prompt_tokens'),'tokens_output':(usage or {}).get('completion_tokens'),'cost':cost,'cost_estimated':estimated,'cached':False,'cache_key':key})
    if budget.status()['warning']:log.warning('budget.warning',extra={'spent':budget.status()['spent'],'limit':budget.status()['limit'],'request_id':rid,'draft_id':did})
    return result
   except ProviderError as e:
    last=e;partial=''.join(pieces) if 'pieces' in locals() else '';
    if e.kind=='budget': cost,estimated=0.0,False
    else:
     approx_usage={'prompt_tokens':expected_in,'completion_tokens':max(1,len(partial)//3)};cost,estimated=costs.estimate(approx_usage,provider=pname,model=MODEL)
     estimated=True
    cost=cost or 0;budget.settle(reservation,cost);reservation=0.0;total_cost+=cost
    log.log(logging.WARNING if e.retryable and n<MAX_ATTEMPTS else logging.ERROR,'llm.attempt',extra={'request_id':rid,'draft_id':did,'client':request.metadata.get('client'),'provider':pname,'model':getattr(provider,'model',MODEL),'prompt_version':pv,'attempt':attempt,'queue_ms':round(queue_ms,1),'first_token_ms':None,'duration_ms':round((time.perf_counter()-t0)*1000,1) if 't0' in locals() else 0,'status':'failed','kind':e.kind,'fallback':fallback_used,'tokens_input':None,'tokens_output':None,'cost':cost,'cost_estimated':estimated,'cached':False,'cache_key':key,'detail':e.detail})
    if e.kind in ('budget','cancelled') or not e.retryable or n>=MAX_ATTEMPTS:break
    time.sleep(min(BACKOFF_MAX,BACKOFF_BASE*(2**(n-1))))
   finally:
    if acquired:_sem.release()
  if last and FALLBACK not in ('none','',pname) and pname==PROVIDER and last.kind in ('timeout','rate_limit','unavailable'):
   fallback_used=True;continue
  break
 if last:raise last
 raise ProviderError('unknown','Не вдалося отримати відповідь')



def _stream_with_timeouts(iterator, started):
 """Consume a synchronous provider stream with idle and total deadlines."""
 q=queue.Queue()
 def produce():
  try:
   for item in iterator:q.put((True,item))
  except BaseException as exc:q.put((False,exc))
  finally:q.put((True,None))
 threading.Thread(target=produce,name='llm-stream-reader',daemon=True).start()
 while True:
  remaining=TOTAL_TIMEOUT-(time.perf_counter()-started)
  if remaining<=0:raise ProviderError('timeout','Перевищено загальний час генерації',retryable=True)
  try:ok,item=q.get(timeout=min(IDLE_TIMEOUT,remaining))
  except queue.Empty:raise ProviderError('timeout','Провайдер не надсилав фрагменти вчасно',retryable=True)
  if item is None:return
  if not ok:raise item
  yield item

def generate(request:Request,on_delta:Callable[[str],None],on_status:Callable[[dict],None],is_cancelled:Callable[[],bool])->Result:
 """Serialize identical in-flight cache misses; waiters reuse first result."""
 key=cache.make_key(request,request.metadata.get('prompt_version','unknown'))
 lock=cache.flight_lock(key)
 with lock:
  return _generate(request,on_delta,on_status,is_cancelled)
