"""Per-client sliding-window rate limiter."""
import os,time,threading
from collections import defaultdict,deque
from dotenv import load_dotenv
load_dotenv();PER_MINUTE=int(os.getenv('RATE_LIMIT_PER_MINUTE','10'));_hits=defaultdict(deque);_lock=threading.Lock()
class RateLimited(Exception):
 def __init__(self,retry_after):self.retry_after=retry_after;super().__init__(f'Ліміт запитів вичерпано. Повторіть через {retry_after:.0f} с')
def settings():return {'per_minute':PER_MINUTE}
def acquire(client):
 now=time.monotonic()
 with _lock:
  q=_hits[str(client)]
  while q and now-q[0]>=60:q.popleft()
  if len(q)>=PER_MINUTE:raise RateLimited(max(1,60-(now-q[0])))
  q.append(now)
