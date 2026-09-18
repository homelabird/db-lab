from __future__ import annotations
import hashlib
import http.client
import json
import os
import random
import re
import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from .common import Blocked,Guard,Report,Sampler,Probe,snapshot,redact
from .load import exercise
from .resp import Connection,RedisError,ServerError,text,pairs


def metric(c):
    # Redis allocator statistics are refreshed periodically. Do not sample immediately after mutation.
    time.sleep(.25);return snapshot(c)

def measured(c,*cmd):
    start=time.perf_counter();answer=c.execute(*cmd)
    return (time.perf_counter()-start)*1000,answer

def populate_hash(c,key,fields):
    for start in range(0,fields,200):
        args=[]
        for i in range(start,min(fields,start+200)):args.extend((f'f:{i:08d}',f'{i:08d}'+('x'*56)))
        c.execute('HSET',key,*args)
    if c.execute('HLEN',key)!=fields:raise RuntimeError('Hash population count mismatch')

def bigkey(s,c,g,r,a):
    fields=a.fields;g.configure(**{'slowlog-log-slower-than':500})
    key=g.prefix+'big-hash';populate_hash(c,key,fields)
    r.event('hash_ready',fields=fields,memory_bytes=c.execute('MEMORY','USAGE',key))
    full=[];small=[]
    for _ in range(8):
        duration,answer=measured(c,'HGETALL',key);full.append(duration)
        if len(answer)!=fields*2:raise RuntimeError('HGETALL response length mismatch')
        duration,answer=measured(c,'HMGET',key,'f:00000000','f:00000001');small.append(duration)
        if answer[0] is None:raise RuntimeError('HMGET missing known field')
    del_ms,_=measured(c,'DEL',key)
    populate_hash(c,key,fields)
    unlink_ms,_=measured(c,'UNLINK',key)
    deadline=time.monotonic()+15
    while c.info('memory').get('lazyfree_pending_objects',0) and time.monotonic()<deadline:time.sleep(.1)
    if c.execute('EXISTS',key):raise RuntimeError('Deleted key is still visible')
    slow=[]
    for item in c.execute('SLOWLOG','GET',20):
        if g.prefix.encode() in b' '.join(x for x in item[3] if isinstance(x,bytes)):
            slow.append({'id':item[0],'timestamp':item[1],'server_duration_us':item[2],
                'command':text(item[3][0])})
    r.data['findings'].update({'hgetall_median_ms':statistics.median(full),'hmget_median_ms':statistics.median(small),
        'del_ms':del_ms,'unlink_ms':unlink_ms,'slowlog':slow,
        'lazyfree_pending_after':c.info('memory').get('lazyfree_pending_objects'),
        'note':'DEL and UNLINK measured once each; this does not prove a universal speed ordering.'})
    return 'PASS' if statistics.median(full)>max(.5,statistics.median(small)*2) else 'NOT_REPRODUCED'


def value_for(i):
    size=(384,400,448,480,512)[i%5]
    prefix=f'{i:010d}:'.encode()
    return prefix+b'F'*(size-len(prefix))

def fragment_digest(c,prefix,n):
    digest=hashlib.sha256();count=0
    for start in range(0,n,800):
        ids=[i for i in range(start,min(n,start+800)) if i%4==0]
        values=c.pipeline([('GET',prefix+str(i)) for i in ids])
        for i,value in zip(ids,values):
            if value!=value_for(i):raise RuntimeError(f'Survivor data mismatch: {i}')
            digest.update(str(i).encode()+b'\0'+value);count+=1
    return {'sha256':digest.hexdigest(),'keys':count}

def fragmentation(s,c,g,r,a):
    before=metric(c)
    if 'jemalloc' not in str(before.get('mem_allocator','')):
        raise Blocked('This allocator is not jemalloc; active defragmentation/PURGE experiment is not comparable')
    g.configure(activedefrag='no')
    n=a.mib*1024*1024//560 # raw payload + conservative per-key planning allowance
    prefix=g.prefix+'frag:'
    for start in range(0,n,200):
        c.pipeline([('SET',prefix+str(i),value_for(i)) for i in range(start,min(n,start+200))])
        if start%10000==0 and c.info('memory')['used_memory']>210*1024*1024:
            raise Blocked('Memory safety ceiling reached; reduce --mib')
    filled=metric(c);r.event('fragmentation_filled',keys=n,used_memory=filled.get('used_memory'))
    for start in range(0,n,300):
        keys=[prefix+str(i) for i in range(start,min(n,start+300)) if i%4!=0]
        if keys:c.execute('DEL',*keys)
    fragmented=metric(c);digest_before=fragment_digest(c,prefix,n)
    observed=(fragmented.get('allocator_frag_ratio',0)>=1.15 and fragmented.get('allocator_frag_bytes',0)>=4*1024*1024
              and fragmented.get('allocator_frag_bytes',0)>before.get('allocator_frag_bytes',0)+2*1024*1024)
    r.event('fragmentation_observation',observed=observed,ratio=fragmented.get('allocator_frag_ratio'),
        wasted_bytes=fragmented.get('allocator_frag_bytes'))
    phases={'before':before,'filled':filled,'after_sparse_delete':fragmented}
    time.sleep(3);phases['no_action']=metric(c)
    purge_ms,_=measured(c,'MEMORY','PURGE');time.sleep(1);phases['after_purge']=metric(c)
    r.event('purge_complete',command_ms=purge_ms)
    supported=True;support_error=None
    try:
        g.configure(**{'active-defrag-ignore-bytes':1048576,'active-defrag-threshold-lower':1,
            'active-defrag-threshold-upper':100,'active-defrag-cycle-min':5,'active-defrag-cycle-max':25,'activedefrag':'yes'})
    except ServerError as exc:
        supported=False;support_error=redact(exc)
    active_seen=False;reduced=False
    if supported:
        deadline=time.monotonic()+a.seconds
        while time.monotonic()<deadline:
            current=metric(c)
            if (current.get('active_defrag_running',0)>0 or
                current.get('active_defrag_hits',0)>fragmented.get('active_defrag_hits',0)):active_seen=True
            reduced=current.get('allocator_frag_bytes',0)<fragmented.get('allocator_frag_bytes',0)*.8
            if active_seen and reduced:break
            time.sleep(.75)
        phases['after_active_defrag']=metric(c)
    digest_after=fragment_digest(c,prefix,n)
    if digest_after!=digest_before:raise RuntimeError('Fragmentation survivor digest changed')
    r.data['findings'].update({'phases':phases,'fragmentation_observed':observed,'active_defrag_supported':supported,
        'active_defrag_error':support_error,'active_work_observed':active_seen,'wasted_bytes_reduced_20pct':reduced,
        'survivor_digest_before':digest_before,'survivor_digest_after':digest_after,
        'comparison_note':'Sequential observation on one dataset, NOT independent randomized A/B trials. PURGE does not move live objects.'})
    if not supported:return 'BLOCKED'
    return 'PASS' if observed and active_seen and reduced else 'NOT_REPRODUCED'


def eviction(s,c,g,r,a):
    base=metric(c);limit=int(base['used_memory'])+12*1024*1024
    g.configure(**{'maxmemory':limit,'maxmemory-policy':'noeviction'})
    denied=False;accepted=0;payload=b'e'*2048
    for i in range(18000):
        try:c.execute('SET',g.prefix+'ev:'+str(i),payload);accepted+=1
        except ServerError as exc:
            if 'OOM' in str(exc).upper():denied=True;break
            raise
    noevict=metric(c)
    g.configure(**{'maxmemory-policy':'allkeys-lru'})
    for i in range(accepted,accepted+4000):c.execute('SET',g.prefix+'ev:'+str(i),payload)
    lru=metric(c)
    evicted=lru.get('evicted_keys',0)-noevict.get('evicted_keys',0)
    r.data['findings'].update({'maxmemory_bytes':limit,'noeviction_write_rejected':denied,'accepted_before_rejection':accepted,
        'evicted_after_lru':evicted,'snapshots':{'before':base,'noeviction':noevict,'lru':lru},
        'note':'Redis maxmemory rejection is not a container OOM kill. Perf-only allkeys-lru may evict older perf keys.'})
    return 'PASS' if denied and evicted>0 else 'NOT_REPRODUCED'


def connections(s,c,g,r,a):
    initial=metric(c); current=int(initial.get('connected_clients',1))
    limit=max(24,current+12);g.configure(maxclients=limit)
    opened=[];rejects=[]
    try:
        for _ in range(limit+10):
            extra=s.direct(timeout=1)
            try:extra.execute('PING');opened.append(extra)
            except RedisError as exc:
                extra.close();rejects.append(redact(exc));break
        peak=metric(c)
    finally:
        for extra in opened:extra.close()
    r.data['findings'].update({'maxclients':limit,'extra_connections':len(opened),'errors':rejects,
        'rejected_connections_delta':peak.get('rejected_connections',0)-initial.get('rejected_connections',0),
        'connected_after_close':c.info('clients').get('connected_clients'),
        'note':'FD limits and maxclients are separate constraints; this scenario changes only Redis maxclients.'})
    return 'PASS' if rejects and peak.get('rejected_connections',0)>initial.get('rejected_connections',0) else 'NOT_REPRODUCED'


def clients_by_id(c):
    out={}
    for line in text(c.execute('CLIENT','LIST')).splitlines():
        item=dict(field.split('=',1) for field in line.split() if '=' in field)
        out[item['id']]=item
    return out

def slowconsumer(s,c,g,r,a):
    cfg=c.config('client-output-buffer-limit')['client-output-buffer-limit']
    replacement=re.sub(r'pubsub\s+\S+\s+\S+\s+\S+','pubsub 262144 131072 1',cfg)
    if replacement==cfg:raise Blocked('Cannot identify pubsub buffer limits in CONFIG GET')
    g.configure(**{'client-output-buffer-limit':replacement})
    consumer=s.direct(timeout=2)
    try:
        consumer.connect();consumer.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024)
        consumer.execute('CLIENT','SETNAME','ops-slow-consumer')
        ident=str(consumer.execute('CLIENT','ID'));channel=g.prefix+'channel'
        consumer.execute('SUBSCRIBE',channel)
        max_omem=0;disconnected=False;sent=0;deadline=time.monotonic()+a.seconds
        payload=b'p'*16384
        while time.monotonic()<deadline and sent<4096:
            c.execute('PUBLISH',channel,payload);sent+=1
            if sent%20==0:
                obj=clients_by_id(c).get(ident)
                if obj is None:disconnected=True;break
                max_omem=max(max_omem,int(obj.get('omem',0)))
            time.sleep(.001)
        r.data['findings'].update({'published_mib':sent/64,'peak_observed_output_bytes':max_omem,
            'slow_subscriber_disconnected':disconnected,'subscriber_id':ident,
            'note':'Buffer peaks can be shorter than the sampling interval. Pub/Sub does not replay lost messages.'})
        return 'PASS' if disconnected else 'NOT_REPRODUCED'
    finally:consumer.close()


def persistence(s,c,g,r,a):
    # Disable scheduling so the requested BGSAVE is the single trigger.
    g.configure(save='',appendonly='no')
    count=max(1024,a.mib*1024*1024//4096);payload=b'A'*4096;prefix=g.prefix+'cow:'
    for start in range(0,count,128):
        c.pipeline([('SET',prefix+str(i),payload) for i in range(start,min(count,start+128))])
    marker=g.prefix+'marker';c.execute('SET',marker,'unchanged')
    before=metric(c);stop=threading.Event();errors=[];updates=[0]
    def writer():
        try:
            with s.direct(timeout=3) as w:
                i=0
                while not stop.is_set():
                    w.pipeline([('SET',prefix+str((i+j)%count),b'B'*4096) for j in range(64)])
                    i+=64;updates[0]+=64
        except RedisError as exc:errors.append(redact(exc))
    thread=threading.Thread(target=writer,daemon=True);thread.start()
    try:
        time.sleep(.1);c.execute('BGSAVE');r.event('bgsave_started')
        deadline=time.monotonic()+60;peak=0;seen=False
        while time.monotonic()<deadline:
            info=c.info('persistence');seen|=bool(info.get('rdb_bgsave_in_progress',0))
            peak=max(peak,int(info.get('current_cow_size',0)))
            if not info.get('rdb_bgsave_in_progress',0):break
            time.sleep(.02)
        else:raise RuntimeError('BGSAVE did not complete within 60s')
    finally:stop.set();thread.join(5)
    after=metric(c)
    if errors:raise RuntimeError('Writer failed: '+';'.join(errors))
    if thread.is_alive():raise RuntimeError('COW writer failed to stop')
    if after.get('rdb_last_bgsave_status')!='ok' or c.execute('GET',marker)!=b'unchanged':
        raise RuntimeError('BGSAVE status or marker verification failed')
    r.data['findings'].update({'before':before,'after':after,'updates':updates[0],'in_progress_seen':seen,
        'peak_current_cow_seen':peak,'last_cow_bytes':after.get('rdb_last_cow_size'),
        'note':'BGSAVE completion is verified. A large latency/COW spike is hardware/workload dependent, not guaranteed. The perf RDB contains synthetic keys.'})
    return 'PASS' if after.get('rdb_last_cow_size',0)>0 else 'NOT_REPRODUCED'


def proxy_rule(s,rule=None):
    conn=http.client.HTTPConnection(s.proxy_host,8080,timeout=3)
    try:
        headers={'Authorization':'Bearer '+os.environ.get('PROXY_TOKEN',s.sentinel_password)}
        if rule is None:conn.request('GET','/state',headers=headers)
        else:
            headers['Content-Type']='application/json';conn.request('POST','/state',json.dumps(rule),headers)
        response=conn.getresponse();body=response.read(8192)
        if response.status!=200:raise RuntimeError(f'Proxy control failed: HTTP {response.status}')
        return json.loads(body)
    finally:conn.close()


def latency(s,c,g,r,a):
    delay=a.delay_ms
    try:
        proxy_rule(s,{'delay_ms':0,'ttl_s':120})
        baseline=exercise(s,r,target='proxy',seconds=a.seconds,rate=80,workers=8,keys=500,phase='normal')
        r.event('proxy_delay_enabled',delay_each_direction_ms=delay,auto_expiry_seconds=120)
        proxy_rule(s,{'delay_ms':delay,'ttl_s':120})
        impaired=exercise(s,r,target='proxy',seconds=a.seconds,rate=80,workers=8,keys=500,phase='delayed')
        proxy_rule(s,{'delay_ms':0,'ttl_s':1})
        recovered=exercise(s,r,target='proxy',seconds=a.seconds,rate=80,workers=8,keys=500,phase='recovered')
        p0=baseline['rtt_ms']['p50_upper'];p1=impaired['rtt_ms']['p50_upper'];p2=recovered['rtt_ms']['p50_upper']
        r.data['findings']['proxy']={'per_direction_per_chunk_delay_ms':delay,'control_after':proxy_rule(s),
            'scope':'Only ops client ↔ perf Redis. HA/Sentinel and replication links are NOT impaired.',
            'note':'Request RTT includes client queue/connection setup where applicable. Server SLOWLOG excludes network time.'}
        if not all(x is not None for x in (p0,p1,p2)):return 'FAIL'
        return 'PASS' if p1>p0+delay and p2<p1*.7 else 'NOT_REPRODUCED'
    finally:
        # This auto-expiring rule is outside the Redis config recovery journal.
        proxy_rule(s,{'delay_ms':0,'ttl_s':1})


def cache_stampede(s,c,g,r,a):
    workers=min(32,a.workers);key=g.prefix+'cache';lockkey=g.prefix+'lock';answer=b'origin-profile'
    phases={}
    for singleflight in (False,True):
        c.execute('SET',key,answer,'PX',30);time.sleep(.05)
        barrier=threading.Barrier(workers);sem=threading.Semaphore(4);mutex=threading.Lock()
        calls=[0];durations=[];errors=[]
        def origin():
            with mutex:calls[0]+=1
            with sem:time.sleep(.06)
            return answer
        def task(index):
            begin=time.perf_counter()
            try:
                with s.direct(timeout=2) as rc:
                    barrier.wait(timeout=5);value=rc.execute('GET',key)
                    if value is None:
                        if not singleflight:
                            value=origin();rc.execute('SET',key,value,'EX',60)
                        else:
                            token=f'owner-{index}';deadline=time.monotonic()+5
                            while time.monotonic()<deadline:
                                value=rc.execute('GET',key)
                                if value is not None:break
                                if rc.execute('SET',lockkey,token,'NX','PX',2000):
                                    try:
                                        value=rc.execute('GET',key)
                                        if value is None:value=origin();rc.execute('SET',key,value,'EX',60)
                                    finally:
                                        rc.execute('EVAL',"if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",1,lockkey,token)
                                    break
                                time.sleep(.005)
                            else:raise RuntimeError('Singleflight wait timed out')
                    if value!=answer:raise RuntimeError('Cache value mismatch')
            except Exception as exc:
                with mutex:errors.append(redact(exc))
            finally:
                with mutex:durations.append((time.perf_counter()-begin)*1000)
        with ThreadPoolExecutor(max_workers=workers) as pool:list(pool.map(task,range(workers)))
        name='singleflight' if singleflight else 'naive'
        phases[name]={'requests':workers,'origin_calls':calls[0],'median_ms':statistics.median(durations),
            'max_ms':max(durations),'errors':errors};r.event('cache_phase',phase=name,**phases[name])
    r.data['findings'].update({'cache':phases,'origin_model':'In-runner semaphore(4) + 60ms simulated I/O; not a real DB benchmark.',
        'lock_scope':'Single Redis demonstration only. Does not establish distributed-lock safety across Sentinel failover.'})
    if any(v['errors'] for v in phases.values()):return 'FAIL'
    return 'PASS' if phases['singleflight']['origin_calls']==1 and phases['naive']['origin_calls']>1 else 'NOT_REPRODUCED'


def stream_pending(s,c,g,r,a):
    key=g.prefix+'events';dedupe=g.prefix+'processed';group='workers'
    ids=c.pipeline([('XADD',key,'*','transaction',f'tx-{i}') for i in range(40)])
    c.execute('XGROUP','CREATE',key,group,'0')
    read=c.execute('XREADGROUP','GROUP',group,'consumer-a','COUNT',20,'STREAMS',key,'>')
    messages=read[0][1]
    if len(messages)!=20:raise RuntimeError('Expected 20 delivered messages')
    # Consumer performs 10 effects, then "dies" before ACK. No destructive process kill needed.
    for ident,fields in messages[:10]:c.execute('SADD',dedupe,ident)
    pending_before=c.execute('XPENDING',key,group)[0];time.sleep(.2)
    claimed=c.execute('XAUTOCLAIM',key,group,'consumer-b',100,'0-0','COUNT',100)[1]
    duplicates=0;new=0
    for ident,fields in claimed:
        if c.execute('SADD',dedupe,ident):new+=1
        else:duplicates+=1
        c.execute('XACK',key,group,ident)
    pending_after=c.execute('XPENDING',key,group)[0];effects=c.execute('SCARD',dedupe)
    r.data['findings'].update({'pending_before':pending_before,'claimed':len(claimed),'pending_after':pending_after,
        'new_effects_on_recovery':new,'duplicate_effects_prevented':duplicates,'unique_effects':effects,
        'note':'20 further messages are still undelivered. This simulates consumer inactivity after work, before ACK; it does not kill a container.'})
    return 'PASS' if pending_before==20 and len(claimed)==20 and pending_after==0 and effects==20 and duplicates==10 else 'FAIL'

SCENARIOS={
    'bigkey':bigkey,'fragmentation':fragmentation,'eviction':eviction,'connections':connections,
    'slow-consumer':slowconsumer,'persistence':persistence,'latency':latency,
    'cache-stampede':cache_stampede,'stream-pending':stream_pending,
}

def run_scenario(settings,args):
    report=Report(args.name,vars(args));status='FAIL'
    try:
        with settings.direct(timeout=5) as c,Guard(c,report) as guard:
            with Sampler(report,{'perf':lambda:settings.direct(timeout=1)},interval=.5), Probe(report,lambda:settings.direct(timeout=1)):
                status=SCENARIOS[args.name](settings,c,guard,report,args)
    except Blocked as exc:status='BLOCKED';report.data['error']=redact(exc)
    except (Exception,KeyboardInterrupt) as exc:status='FAIL';report.data['error']=redact(exc) or 'Interrupted'
    if report.data.get('recovery',{}).get('ok') is False:status='FAIL'
    return report.finish(status)
