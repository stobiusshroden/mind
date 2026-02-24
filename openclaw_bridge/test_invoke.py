import time, json
import httpx

inv = httpx.post('http://127.0.0.1:17171/skills/invoke', json={'callId':'test','skill':'openclaw_status','args':{},'context':{}}).json()
print('invoke:', json.dumps(inv, indent=2))
jid = inv['jobId']
for i in range(40):
    j = httpx.get(f'http://127.0.0.1:17171/jobs/{jid}').json()
    print(i, j.get('status'), j.get('returncode'))
    if j.get('status') in ('succeeded','failed','canceled'):
        print('final:', json.dumps(j, indent=2))
        break
    time.sleep(0.25)
