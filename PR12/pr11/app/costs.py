"""Розрахунок вартості викликів за usage або оцінкою."""
import os
from dotenv import load_dotenv
load_dotenv()
PRICE_INPUT=float(os.getenv('LLM_PRICE_INPUT_PER_1M','0.30'))
PRICE_OUTPUT=float(os.getenv('LLM_PRICE_OUTPUT_PER_1M','2.50'))
_cached=os.getenv('LLM_PRICE_CACHED_INPUT_PER_1M','').strip()
PRICE_CACHED_INPUT=float(_cached) if _cached else PRICE_INPUT*.25
MOCK_BILLABLE=os.getenv('MOCK_BILLABLE','1')=='1'
def settings():
 return {'input_per_1m':PRICE_INPUT,'output_per_1m':PRICE_OUTPUT,'cached_input_per_1m':PRICE_CACHED_INPUT,'mock_billable':MOCK_BILLABLE,'currency':'USD'}
def estimate(usage,*,provider=None,model=None,text=None):
 if provider=='mock' and not MOCK_BILLABLE:return 0.0,False
 if usage:
  try:
   inp=int(usage.get('prompt_tokens',usage.get('input_tokens',0)) or 0); out=int(usage.get('completion_tokens',usage.get('output_tokens',0)) or 0)
   details=usage.get('prompt_tokens_details') or {}; cached=min(inp,int(details.get('cached_tokens',0) or 0))
   return round(((inp-cached)*PRICE_INPUT+cached*PRICE_CACHED_INPUT+out*PRICE_OUTPUT)/1_000_000,8),False
  except (TypeError,ValueError):pass
 if text is None:return None,True
 # Approximation: ~3 chars/token for multilingual text; explicitly marked estimated.
 return round(((max(1,len(text)//3))*PRICE_OUTPUT)/1_000_000,8),True
