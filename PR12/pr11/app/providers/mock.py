"""Deterministic local provider for demos and tests."""
import os,time,random
from typing import Iterator
from .base import Chunk,ProviderError,Request,Result
class MockProvider:
 name='mock';model='mock-1'
 def _text(self,r):
  from .. import samples
  s=samples.get(str(r.metadata.get('request_id')))
  return (s or {}).get('reply_example') or 'Дякуємо за звернення. Ми перевіримо надану інформацію та повернемося з відповіддю.'
 def _failure(self,r):
  kind=os.getenv('MOCK_FAILURE_KIND','unavailable');rate=float(os.getenv('MOCK_FAILURE_RATE','0'))
  if r.metadata.get('mock_failure') or random.random()<rate:
   if kind=='timeout':time.sleep(float(os.getenv('LLM_TIMEOUT','20'))+1)
   raise ProviderError(kind,'Тестова помилка підставного провайдера',retryable=kind in ('timeout','unavailable','rate_limit'),retry_after=1)
 def complete(self,r):
  self._failure(r);time.sleep(int(os.getenv('MOCK_FIRST_DELAY_MS','50'))/1000);t=self._text(r);usage={'prompt_tokens':max(1,sum(len(m['content']) for m in r.messages)//3),'completion_tokens':max(1,len(t)//3),'total_tokens':max(2,(sum(len(m['content']) for m in r.messages)+len(t))//3)}
  return Result(t,self.name,self.model,usage,'stop')
 def stream(self,r)->Iterator[Chunk]:
  self._failure(r);t=self._text(r);delay=int(os.getenv('MOCK_DELAY_MS','15'))/1000;first=int(os.getenv('MOCK_FIRST_DELAY_MS','80'))/1000;time.sleep(first)
  words=t.split(' ')
  for i in range(0,len(words),5):
   yield Chunk(' '.join(words[i:i+5])+(' ' if i+5<len(words) else ''));time.sleep(delay)
  usage={'prompt_tokens':max(1,sum(len(m['content']) for m in r.messages)//3),'completion_tokens':max(1,len(t)//3),'total_tokens':max(2,(sum(len(m['content']) for m in r.messages)+len(t))//3)}
  yield Chunk('',usage,'stop')
