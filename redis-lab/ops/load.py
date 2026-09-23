"""Rate-scheduled bounded workload. No silent catch-up bursts or write retries."""
from __future__ import annotations
import json
import os
import queue
import random
import threading
import time
from collections import Counter
from .common import Histogram, Report, Sampler, atomic_json, cleanup, utc
from .resp import RedisError, ServerError

class Stats:
    def __init__(self):
        self.lock=threading.Lock();self.count=Counter();self.errors=Counter()
        self.rtt=Histogram();self.end_to_end=Histogram()
    def add_count(self,key,n=1):
        with self.lock: self.count[key]+=n
    def result(self,success,op,rtt,total,error=None,send_attempted=True):
        with self.lock:
            self.count['attempted']+=1;self.count['success' if success else 'error']+=1
            self.count[op]+=1;self.rtt.add(rtt);self.end_to_end.add(total)
            if error:
                self.errors[error]+=1
                if op=='SET':
                    self.count['writes_not_sent' if not send_attempted else ('writes_rejected' if error.startswith('server:') else 'writes_uncertain')]+=1
    def snapshot(self):
        with self.lock:
            return {'counts':dict(self.count),'errors':dict(self.errors),'rtt_ms':self.rtt.summary(),
                'scheduled_to_reply_ms':self.end_to_end.summary()}

def validate(seconds,rate,workers,keys,payload):
    for name,value,lo,hi in [('seconds',seconds,1,600),('rate',rate,1,10000),('workers',workers,1,64),
            ('keys',keys,1,50000),('payload',payload,32,8192)]:
        if not lo<=value<=hi: raise ValueError(f'{name} must be {lo}..{hi}')
    if keys*payload>96*1024*1024: raise ValueError('Raw seed data exceeds 96MiB safety limit')

def save_benchmark(report, result, status):
    """Write the shared benchmark-v1 envelope beside the detailed Redis evidence."""
    versions=set()
    metrics=report.path/'metrics.jsonl'
    if metrics.is_file():
        for line in metrics.read_text(encoding='utf-8').splitlines():
            try: version=json.loads(line).get('info',{}).get('redis_version')
            except (ValueError,AttributeError): continue
            if version: versions.add(str(version))
    parameters={key:value for key,value in report.data.get('parameters',{}).items()
                if key not in ('cmd','target')}
    counts=result.get('counts',{});rtt=result.get('rtt_ms',{})
    cleanup_complete=(result.get('cleanup_keys') == parameters.get('keys')
                      and 'cleanup_error' not in result)
    try: runtime_environment=json.loads(os.environ.get('DB_LAB_BENCHMARK_ENV','{}'))
    except (TypeError,ValueError): runtime_environment={}
    if not isinstance(runtime_environment,dict): runtime_environment={}
    normalized={
        'schema_version':1,'run_id':report.id,'status':status,'evidence_kind':'live_database',
        'started_utc':report.data.get('started_utc'),'finished_utc':utc(),
        'duration_seconds':result.get('wall_s'),
        'database':{'product':'Redis','version':','.join(sorted(versions)) or None,
                    'target':result.get('target')},
        'workload':{'name':'mixed-read-write','parameters':parameters},
        'environment':{**runtime_environment,'revision':os.environ.get('GIT_COMMIT'),
                       'container_engine':runtime_environment.get('container_engine',os.environ.get('CONTAINER_ENGINE')),
                       'container_engine_version':runtime_environment.get('container_engine_version',
                                                                          os.environ.get('CONTAINER_ENGINE_VERSION'))},
        'metrics':{'requests_attempted':counts.get('attempted',0),
                   'requests_succeeded':counts.get('success',0),
                   'requests_failed':counts.get('error',0),
                   'success_rps':result.get('achieved_success_rps'),
                   'latency_p50_ms':rtt.get('p50_upper'),
                   'latency_p95_ms':rtt.get('p95_upper'),
                   'latency_p99_ms':rtt.get('p99_upper'),
                   'offered_rps':result.get('offered_target_rps'),
                   'queue_dropped':counts.get('queue_dropped',0),
                   'scheduler_skipped':counts.get('scheduler_skipped',0)},
        'verification':{'workload_completed':status=='PASS',
                        'owned_keys_cleanup_succeeded':cleanup_complete,
                        'dataset_state_verified':cleanup_complete},
        'note':'Redis mixed workload observation. Container image/resource metadata is captured when supplied by the host runner; full-run contention and dataset equivalence are not verified. Not production capacity evidence.'}
    atomic_json(report.path/'benchmark.json',normalized)
    return normalized

def seed(settings,target,prefix,keys,payload):
    with settings.connection(target,timeout=4) as c:
        mem=c.info('memory'); ceiling=int(mem.get('maxmemory',0))
        if ceiling and int(mem['used_memory'])+keys*(payload+160)>ceiling*.85:
            raise RuntimeError('Insufficient headroom for bounded seed; lower --keys or --payload')
        value=b'v'*payload
        for start in range(0,keys,200):
            c.pipeline([('SET',f'{prefix}{i}',value,'EX',1800) for i in range(start,min(keys,start+200))])

def exercise(settings,report,target='perf',seconds=20,rate=300,workers=8,keys=2000,payload=256,
             read_pct=80,hot_pct=70,seed_data=True,phase='load',keep=False):
    validate(seconds,rate,workers,keys,payload)
    if not 0<=read_pct<=100 or not 0<=hot_pct<=100: raise ValueError('Ratios must be 0..100')
    prefix='ops:'+report.id+'-'+phase+':'
    if seed_data: seed(settings,target,prefix,keys,payload)
    jobs=queue.Queue(maxsize=max(8,workers*4));stats=Stats(); stop=threading.Event()
    interval=1/rate; start=time.monotonic(); end=start+seconds
    value=b'v'*payload
    error_log_lock=threading.Lock(); logged=[0]
    def worker(index):
        c=None;rng=random.Random(20260918+index)
        try:
            while not stop.is_set() or not jobs.empty():
                try: scheduled=jobs.get(timeout=.05)
                except queue.Empty: continue
                try:
                    if time.monotonic()>end:
                        stats.add_count('expired_before_send');continue
                    key_index=rng.randrange(max(1,keys//100)) if rng.randrange(100)<hot_pct else rng.randrange(keys)
                    key=f'{prefix}{key_index}'
                    op='GET' if rng.randrange(100)<read_pct else 'SET'
                    sent=time.monotonic();ok=False;error=None;send_attempted=False
                    try:
                        if c is None: c=settings.connection(target,timeout=1)
                        c.last_command_send_attempted=False
                        result=c.execute(op,key) if op=='GET' else c.execute(op,key,value,'EX',1800)
                        send_attempted=c.last_command_send_attempted;ok=True
                        if op=='GET': stats.add_count('cache_miss' if result is None else 'cache_hit')
                    except (RedisError,OSError) as exc:
                        send_attempted=bool(c and c.last_command_send_attempted)
                        code=str(exc).split()[0] if isinstance(exc,ServerError) and str(exc) else type(exc).__name__
                        error=('server:' if isinstance(exc,ServerError) else 'transport:')+code
                        if c: c.close()
                        c=None
                        with error_log_lock:
                            if logged[0]<200:
                                with (report.path/'request-errors.jsonl').open('a') as f:
                                    f.write(json.dumps({'utc':utc(),'phase':phase,'op':op,'error':error,
                                        'outcome':'not-sent' if not send_attempted else ('rejected' if isinstance(exc,ServerError) else 'uncertain')})+'\n')
                                logged[0]+=1
                    stats.add_count('command_send_attempted' if send_attempted else 'command_not_sent')
                    now=time.monotonic(); stats.result(ok,op,(now-sent)*1000,(now-scheduled)*1000,error,send_attempted=send_attempted)
                finally: jobs.task_done()
        finally:
            if c: c.close()
    threads=[threading.Thread(target=worker,args=(i,),daemon=True) for i in range(workers)]
    for t in threads:t.start()
    report.event('load_start',phase=phase,target=target,seconds=seconds,offered_rps=rate)
    sequence=0;next_log=start+1
    try:
        while time.monotonic()<end:
            due=start+sequence*interval
            now=time.monotonic()
            if now<due:
                time.sleep(min(.02,due-now)); continue
            behind=int((now-due)/interval)
            if behind>0:
                stats.add_count('scheduler_skipped',behind); sequence+=behind;due=start+sequence*interval
            stats.add_count('offered');sequence+=1
            try:jobs.put_nowait(due);stats.add_count('enqueued')
            except queue.Full:stats.add_count('queue_dropped')
            if now>=next_log:
                with (report.path/'load-timeseries.jsonl').open('a') as f:
                    f.write(json.dumps({'utc':utc(),'phase':phase,'elapsed':round(now-start,3),**stats.snapshot()})+'\n')
                next_log=now+1
    finally:
        stop.set()
        for t in threads:t.join(10)
        if any(t.is_alive() for t in threads): raise RuntimeError('Workload threads failed to finish')
        result=stats.snapshot(); result.update(phase=phase,target=target,duration_s=seconds,wall_s=round(time.monotonic()-start,3),
            offered_target_rps=rate,achieved_success_rps=round(stats.count['success']/seconds,3),
            correctness_note='Mixed GET/SET benchmark, not a durability or exactly-once test; no command is automatically retried.')
        if not keep:
            try:
                with settings.connection(target,timeout=4) as c:result['cleanup_keys']=cleanup(c,prefix)
            except (RedisError,OSError) as exc:
                result['cleanup_error']=type(exc).__name__;result['cleanup_note']='Seed keys have 1800s TTL.'
        report.data['findings'][phase]=result;report.save()
    return result
