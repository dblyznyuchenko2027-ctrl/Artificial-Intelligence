"""Adapter for OpenAI-compatible chat completions."""
from typing import Iterator
from openai import OpenAI,APITimeoutError,RateLimitError,APIConnectionError,InternalServerError,AuthenticationError,BadRequestError
from .base import Chunk,ProviderError,Request,Result
class OpenAICompatProvider:
 name='openai'
 def __init__(self,base_url,api_key,model,timeout):self.base_url=base_url;self.api_key=api_key;self.model=model;self.timeout=timeout;self.client=OpenAI(base_url=base_url,api_key=api_key,timeout=timeout,max_retries=0)
 def _error(self,e):
  if isinstance(e,APITimeoutError):k='timeout';retry=True
  elif isinstance(e,RateLimitError):k='rate_limit';retry=True
  elif isinstance(e,(APIConnectionError,InternalServerError)):k='unavailable';retry=True
  elif isinstance(e,AuthenticationError):k='auth';retry=False
  elif isinstance(e,BadRequestError):k='bad_request';retry=False
  else:k='unknown';retry=False
  raise ProviderError(k,'Помилка під час звернення до AI-провайдера',retryable=retry,detail=type(e).__name__) from e
 def complete(self,r):
  try:
   x=self.client.chat.completions.create(model=r.model or self.model,messages=r.messages,temperature=r.temperature,max_tokens=r.max_tokens)
   c=x.choices[0];u=x.usage.model_dump() if x.usage else None
   return Result(c.message.content or '',self.name,x.model,u,c.finish_reason)
  except Exception as e:self._error(e)
 def stream(self,r)->Iterator[Chunk]:
  try:
   x=self.client.chat.completions.create(model=r.model or self.model,messages=r.messages,temperature=r.temperature,max_tokens=r.max_tokens,stream=True,stream_options={'include_usage':True})
   for part in x:
    usage=part.usage.model_dump() if getattr(part,'usage',None) else None
    text=''
    if part.choices and part.choices[0].delta:text=part.choices[0].delta.content or ''
    finish=part.choices[0].finish_reason if part.choices else None
    yield Chunk(text,usage,finish)
  except Exception as e:self._error(e)
