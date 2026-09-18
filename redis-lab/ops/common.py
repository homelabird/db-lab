from __future__ import annotations
import fcntl
import html
import json
import math
import os
import re
import secrets
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from .resp import Connection, RedisError, ServerError, text

class Blocked(RuntimeError): pass

def utc(): return datetime.now(timezone.utc).isoformat()
def run_id(name): return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+name+'-'+secrets.token_hex(3)
def root():
    p=Path(os.environ.get('OPS_RESULTS','/results/ops')); p.mkdir(parents=True,exist_ok=True); return p

def atomic_json(path,data):
    path=Path(path); tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)

def redact(message):
    msg=str(message)
    for key in ('REDIS_PASSWORD','SENTINEL_PASSWORD','PROXY_TOKEN'):
        value=os.environ.get(key)
        if value: msg=msg.replace(value,'<redacted>')
    return msg

class Report:
    def __init__(self,name,parameters=None):
        self.id=run_id(name); self.path=root()/self.id; self.path.mkdir()
        self.data={'run_id':self.id,'scenario':name,'status':'RUNNING','started_utc':utc(),
            'parameters':parameters or {},'events':[],'findings':{},'recovery':{},
            'note':'Runtime observations, not an assertion of production capacity or lossless HA.'}
        self.lock=threading.Lock(); self.save()
        atomic_json(root()/'latest.json',{'run_id':self.id,'status':'RUNNING'})
    def save(self): atomic_json(self.path/'report.json',self.data)
    def event(self,name,**details):
        with self.lock:
            row={'utc':utc(),'event':name,**details}; self.data['events'].append(row)
            with (self.path/'events.jsonl').open('a') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(f'[{name}] '+json.dumps(details,ensure_ascii=False),flush=True)
    def finish(self,status,findings=None):
        self.data['status']=status; self.data['finished_utc']=utc()
        if findings: self.data['findings'].update(findings)
        self.save(); render(self.path)
        atomic_json(root()/'latest.json',{'run_id':self.id,'status':status})
        print(f'{status}: {self.path}/report.html',flush=True)
        return {'PASS':0,'FAIL':1,'BLOCKED':2,'NOT_REPRODUCED':3}.get(status,1)

def render(path):
    path=Path(path); data=json.loads((path/'report.json').read_text())
    content=html.escape(json.dumps(data,ensure_ascii=False,indent=2))
    rows=''
    metric=path/'metrics.jsonl'
    if metric.exists():
        names=('used_memory','used_memory_rss','allocator_frag_ratio','allocator_frag_bytes','instantaneous_ops_per_sec','connected_clients')
        for line in metric.read_text().splitlines()[::max(1,sum(1 for _ in metric.open())//160)]:
            try: row=json.loads(line)
            except ValueError: continue
            vals=[row.get('utc',''),row.get('node','')]+[row.get('info',{}).get(k,'—') for k in names]
            rows+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in vals)+'</tr>\n'
        headings=['UTC','노드',*names]
        table='<h2>지표 표본 (원본: metrics.jsonl)</h2><div class="wide"><table><tr>'+''.join('<th>'+x+'</th>' for x in headings)+'</tr>'+rows+'</table></div>'
    else: table=''
    doc='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Redis 운영 실습 결과</title><style>body{font:16px/1.6 system-ui,sans-serif;margin:32px auto;max-width:1180px;padding:0 24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:20px;border:1px solid #bbb;border-radius:8px}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #aaa;padding:7px;text-align:left}.wide{overflow:auto}h1{font-size:28px}</style>
<h1>Redis 운영 실습 결과</h1><p>단위: 메모리는 byte, 요청 지연은 ms, slowlog 서버 실행시간은 µs. NOT_REPRODUCED는 목표 현상 미관측, BLOCKED는 실행 조건 부족입니다.</p>'''+table+'<h2>실험·복구 판정</h2><pre>'+content+'</pre></html>'
    (path/'report.html').write_text(doc,encoding='utf-8')

METRICS=('redis_version','run_id','role','used_memory','used_memory_peak','used_memory_dataset','used_memory_rss',
    'maxmemory','maxmemory_policy','mem_allocator','allocator_allocated','allocator_active','allocator_resident',
    'allocator_frag_ratio','allocator_frag_bytes','allocator_rss_ratio','allocator_rss_bytes','mem_fragmentation_ratio',
    'active_defrag_running','active_defrag_hits','active_defrag_misses','total_active_defrag_time','lazyfree_pending_objects',
    'connected_clients','blocked_clients','client_recent_max_output_buffer','mem_clients_normal',
    'instantaneous_ops_per_sec','total_commands_processed','total_error_replies','rejected_connections',
    'evicted_keys','expired_keys','keyspace_hits','keyspace_misses','used_cpu_sys','used_cpu_user',
    'rdb_bgsave_in_progress','rdb_last_bgsave_status','rdb_last_cow_size','current_cow_size','current_cow_peak',
    'aof_enabled','aof_rewrite_in_progress','aof_last_bgrewrite_status','aof_last_cow_size',
    'aof_delayed_fsync','aof_current_size','aof_base_size','mem_not_counted_for_evict',
    'master_repl_offset','slave_repl_offset','master_link_status','master_sync_in_progress','connected_slaves',
    'repl_backlog_size','repl_backlog_histlen','sync_full','sync_partial_ok','sync_partial_err')

def snapshot(c):
    info=c.info('all')
    return {k:v for k,v in info.items() if k in METRICS or k.startswith(('cmdstat_','errorstat_','slave0','slave1','db0','sentinel_','master0'))}

class Sampler:
    def __init__(self,report,factories,interval=1.0):
        self.report,self.factories,self.interval=report,factories,interval
        self.stop=threading.Event(); self.threads=[]; self.lock=threading.Lock()
    def _run(self,name,factory):
        c=None
        try:
            while not self.stop.is_set():
                started=time.monotonic(); row={'utc':utc(),'node':name}
                try:
                    if c is None: c=factory()
                    row['info']=snapshot(c);row['sample_rtt_ms']=round((time.monotonic()-started)*1000,4)
                except (RedisError,OSError,ValueError) as exc:
                    row['error']=redact(str(exc));
                    if c: c.close()
                    c=None
                with self.lock:
                    with (self.report.path/'metrics.jsonl').open('a') as f:
                        f.write(json.dumps(row,ensure_ascii=False)+'\n')
                self.stop.wait(max(.02,self.interval-(time.monotonic()-started)))
        finally:
            if c: c.close()
    def __enter__(self):
        for name,factory in self.factories.items():
            t=threading.Thread(target=self._run,args=(name,factory),daemon=True);t.start();self.threads.append(t)
        return self
    def __exit__(self,*exc):
        self.stop.set()
        for t in self.threads: t.join(8)

class Histogram:
    """Log-bucket upper bounds; about 3% relative bucket width, never exact quantiles."""
    def __init__(self): self.buckets=Counter(); self.n=0;self.total=0.;self.maximum=0.
    def add(self,ms):
        if not math.isfinite(ms) or ms<0: raise ValueError('Invalid latency')
        i=0 if ms==0 else math.ceil(math.log1p(ms*100)/math.log(1.03))
        self.buckets[i]+=1;self.n+=1;self.total+=ms;self.maximum=max(self.maximum,ms)
    def percentile(self,p):
        if not self.n: return None
        rank=max(1,math.ceil(self.n*p)); n=0
        for i,count in sorted(self.buckets.items()):
            n+=count
            if n>=rank: return round((1.03**i-1)/100,4)
    def summary(self):
        return {'count':self.n,'mean':round(self.total/self.n,4) if self.n else None,
            'p50_upper':self.percentile(.5),'p95_upper':self.percentile(.95),'p99_upper':self.percentile(.99),
            'max':round(self.maximum,4),'quantile_method':'log buckets; upper bound, approx 3% width'}


def owned_prefix(value):
    if not re.fullmatch(r'ops:[A-Za-z0-9_-]{8,150}:',value): raise ValueError('Invalid owned key prefix')
    return value

def cleanup(c,prefix):
    owned_prefix(prefix); removed=0
    # Repeated complete scans handle hash-table changes caused by deletion.
    for _ in range(4):
        cursor=0; found=0
        while True:
            cursor,keys=c.execute('SCAN',cursor,'MATCH',prefix+'*','COUNT',300)
            for start in range(0,len(keys),100):
                batch=keys[start:start+100]
                if batch:
                    if any(not k.startswith(prefix.encode()) for k in batch): raise RuntimeError('Foreign key in cleanup')
                    found+=len(batch); removed+=c.execute('UNLINK',*batch)
            if int(cursor)==0: break
        if not found: return removed
    raise RuntimeError('Owned keys still being created; stop concurrent workload before cleanup')

# Only these non-authentication settings may be journaled/changed by scenarios.
CONFIG_ALLOW={'maxmemory','maxmemory-policy','activedefrag','active-defrag-ignore-bytes',
    'active-defrag-threshold-lower','active-defrag-threshold-upper','active-defrag-cycle-min',
    'active-defrag-cycle-max','slowlog-log-slower-than','slowlog-max-len','maxclients',
    'client-output-buffer-limit','save','appendonly','latency-monitor-threshold','hz'}

class Guard:
    def __init__(self,c,report):
        self.c,self.report=c,report;self.prefix=owned_prefix('ops:'+report.id+':')
        self.journal=root()/'recovery.json';self.file=None;self.original={};self.server_id=None
    def __enter__(self):
        self.file=(root()/'scenario.lock').open('a+')
        try: fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close(); raise Blocked('Another perf scenario is active. Do not run scenarios concurrently.')
        try:
            if self.journal.exists(): raise Blocked('Unfinished recovery journal; run ./ops.sh recover --yes first')
            info=self.c.info()
            if info.get('role')!='master' or info.get('connected_slaves',0)!=0:
                raise Blocked('Experiments require the standalone perf master without replicas')
            if int(info.get('used_memory',0))>110*1024*1024:
                raise Blocked('Perf memory already exceeds 110MiB; remove old perf data before experimenting')
            self.server_id=info['run_id'];self.persist()
        except BaseException:
            self.file.close();raise
        return self
    def persist(self):
        atomic_json(self.journal,{'server_id':self.server_id,'prefix':self.prefix,'config':self.original,
            'report_id':self.report.id,'started_utc':self.report.data['started_utc']})
    def configure(self,**settings):
        if not set(settings)<=CONFIG_ALLOW: raise ValueError('Unsafe configuration change refused')
        for k in settings:
            if k not in self.original:
                current=self.c.config(k)
                if k not in current: raise Blocked('CONFIG not supported: '+k)
                self.original[k]=current[k]
        self.persist() # Durable BEFORE touching Redis.
        args=[]
        for k,v in settings.items(): args.extend((k,v))
        self.c.execute('CONFIG','SET',*args)
    def __exit__(self,exc_type,exc,tb):
        try:
            restored=recover_journal(self.c,self.journal,already_locked=True)
            self.report.data['recovery']=restored
        except BaseException as recovery_error:
            self.report.data['recovery']={'ok':False,'error':redact(recovery_error),'journal_retained':True}
            if exc is None: raise
        finally:
            self.file.close()
        return False

def recover_journal(c,journal=None,already_locked=False):
    journal=Path(journal or root()/'recovery.json')
    lock=None
    if not already_locked:
        lock=(root()/'scenario.lock').open('a+')
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close();raise Blocked('An experiment is still active; stop it before recovery')
    try:
        if not journal.exists(): return {'ok':True,'message':'No pending perf configuration changes'}
        data=json.loads(journal.read_text());prefix=owned_prefix(data['prefix'])
        cfg=data['config']
        if not set(cfg)<=CONFIG_ALLOW: raise ValueError('Journal contains forbidden config keys')
        same=c.info('server')['run_id']==data['server_id']
        if same and cfg:
            args=[]
            for k,v in cfg.items(): args.extend((k,v))
            c.execute('CONFIG','SET',*args)
            observed=c.config(*cfg)
            # Redis canonicalizes some units; originals were read from CONFIG GET, so compare strings.
            if any(str(observed.get(k))!=str(v) for k,v in cfg.items()):
                raise RuntimeError('Configuration restoration read-back mismatch')
        n=cleanup(c,prefix)
        if c.execute('PING')!=b'PONG': raise RuntimeError('PING after recovery failed')
        journal.unlink()
        return {'ok':True,'config_restored':same,'server_restarted':not same,'keys_removed':n,
            'note':'Restarted perf nodes use default config; stale configuration was not reapplied.' if not same else 'Original runtime config restored and read back.'}
    finally:
        if lock: lock.close()

class Probe:
    """Independent PING probe: exposes interference with otherwise cheap requests."""
    def __init__(self,report,factory):
        self.report,self.factory=report,factory;self.stop=threading.Event();self.hist=Histogram();self.failures=0
        self.thread=threading.Thread(target=self.run,daemon=True)
    def run(self):
        c=None
        try:
            while not self.stop.is_set():
                start=time.monotonic();row={'utc':utc(),'command':'PING'}
                try:
                    if c is None:c=self.factory()
                    if c.execute('PING')!=b'PONG':raise RuntimeError('Unexpected PING response')
                    ms=(time.monotonic()-start)*1000;self.hist.add(ms);row.update(ok=True,rtt_ms=ms)
                except (RedisError,OSError) as exc:
                    row.update(ok=False,error=type(exc).__name__);self.failures+=1
                    if c:c.close()
                    c=None
                with (self.report.path/'probe.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
                self.stop.wait(max(.01,.1-(time.monotonic()-start)))
        finally:
            if c:c.close()
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*exc):
        self.stop.set();self.thread.join(5)
        self.report.data['findings']['concurrent_ping']={'rtt_ms':self.hist.summary(),'errors':self.failures,
            'note':'10Hz independent PING during the entire experiment including data preparation; correlate with event timestamps.'}
