#!/usr/bin/env python3
import json, os, random, signal, sys, time, urllib.request
from datetime import datetime, timezone
from lablib import ESClient, INDICES

ES=os.getenv('ES_URL','http://localhost:9200').rstrip('/')
INDEX=os.getenv('INDEX','lab-transactions-v1')
RATE=float(os.getenv('RATE','100'))
BATCH=max(1,int(os.getenv('BATCH','100')))
if RATE <= 0 or BATCH <= 0: raise SystemExit('RATE and BATCH must be positive')
if INDEX != INDICES[0]: raise SystemExit('This live writer supports lab-transactions-v1 only')
ESClient(ES).assert_lab()
running=True

def stop(*_):
    global running
    running=False
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

def bulk(seq):
    lines=[]
    for i in range(BATCH):
        n=seq+i
        lines.append(json.dumps({'index':{'_index':INDEX,'_id':f'LIVE-{n:012d}'}}))
        lines.append(json.dumps({
            '@timestamp':datetime.now(timezone.utc).isoformat(),
            'transaction_id':f'LIVE-{n:012d}',
            'user_id':f'user-{random.randint(0,4999):05d}',
            'device_id':f'dev-{random.randint(0,11999):06d}',
            'institution':random.choice(['alpha-card','beta-bank','gamma-pay','delta-life']),
            'channel':random.choice(['WEB','APP','ATM','ARS']),
            'amount':round(random.uniform(1000,2000000),2),
            'currency':'KRW','country':'KR','merchant_category':random.choice(['travel','food','game','market']),
            'risk_score':random.randint(0,1000),'is_fraud':False,
            'src_ip':f'10.20.{random.randint(0,30)}.{random.randint(1,254)}'
        },separators=(',',':')))
    req=urllib.request.Request(ES+'/_bulk', data=('\n'.join(lines)+'\n').encode(), method='POST', headers={'Content-Type':'application/x-ndjson'})
    with urllib.request.urlopen(req, timeout=30) as r:
        out=json.loads(r.read())
    if out.get('errors'): raise RuntimeError('bulk errors')

seq=int(time.time()*1000)
print(f'live load -> {INDEX}, target rate ~{RATE:.0f} docs/s; Ctrl-C to stop')
while running:
    start=time.time()
    try:
        bulk(seq); seq += BATCH
    except Exception as e:
        print('bulk error:',e,file=sys.stderr)
        time.sleep(1)
        continue
    elapsed=time.time()-start
    sleep=max(0, BATCH/RATE-elapsed)
    if sleep: time.sleep(sleep)
print('stopped')
