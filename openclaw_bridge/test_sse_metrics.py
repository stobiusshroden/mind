import httpx, json, time

# start continuous dynasty_step
inv = httpx.post('http://127.0.0.1:17171/skills/invoke', json={
    'callId':'test','skill':'dynasty_step','args':{'n':0,'emitEvery':50},'context':{'sessionId':'testsess'}
}).json()
job = inv['jobId']
print('job', job)

url = f'http://127.0.0.1:17171/jobs/{job}/events'
print('connecting', url)

with httpx.Client(timeout=None) as hx:
    with hx.stream('GET', url, headers={'Accept':'text/event-stream'}) as r:
        r.raise_for_status()
        event = None
        count = 0
        for line in r.iter_lines():
            if not line:
                continue
            if line.startswith('event:'):
                event = line.split(':',1)[1].strip()
            if line.startswith('data:'):
                data = line.split(':',1)[1].strip()
                try:
                    obj = json.loads(data)
                except Exception:
                    obj = data
                print('EV', event, 'DATA', obj if isinstance(obj, dict) else str(obj)[:120])
                count += 1
                if count >= 10:
                    break

# cancel
httpx.post(f'http://127.0.0.1:17171/jobs/{job}/cancel')
print('canceled')
