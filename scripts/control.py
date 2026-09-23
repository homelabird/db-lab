#!/usr/bin/env python3
"""Read-only preflight/health adapters and secret-free root run receipts."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DIRS = {'elasticsearch':'elasticsearch','elasticsearch9':'elasticsearch-9','kafka':'kafka-lab','mariadb':'mariadb-ha-lab','redis':'redis-lab'}
DEFAULT_PROJECTS = ('elasticsearch','kafka','mariadb','redis')
ES_LIKE = ('elasticsearch', 'elasticsearch9')

def now(): return datetime.now(timezone.utc).isoformat()

def settings(project):
    path=ROOT/DIRS[project]/'.env'
    if not path.exists(): path=path.with_name('.env.example')
    data={}
    for line in path.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#'): continue
        key,sep,value=line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*',key) or key in data:
            raise ValueError(f'{project}: invalid or duplicate KEY=value line')
        value=value.strip()
        if len(value)>1 and value[0] in "\"'" and value[-1]==value[0]: value=value[1:-1]
        data[key]=value
    # ES and Kafka support exported overrides. Other labs intentionally own .env.
    if project in (*ES_LIKE, 'kafka'):
        data.update({k:v for k,v in os.environ.items() if k in data or re.fullmatch(r'KAFKA[0-9]+_PORT',k)})
    return data

def engine_for(project,data):
    if project in ES_LIKE:
        provider=data.get('COMPOSE_PROVIDER','auto')
        if provider.startswith('podman'): return 'podman'
        if provider=='docker': return 'docker'
        if provider!='auto': raise ValueError('Unsupported COMPOSE_PROVIDER')
    else:
        requested=data.get('CONTAINER_ENGINE','auto')
        if requested not in ('auto','podman','docker'): raise ValueError(f'Unsupported CONTAINER_ENGINE: {requested}')
        if requested!='auto': return requested
    return 'podman' if shutil.which('podman') else 'docker'

def compose_command(project,engine,data):
    """Mirror the compose provider each lab will actually use."""
    if project in ES_LIKE:
        provider=data.get('COMPOSE_PROVIDER','auto')
        if provider=='podman-compose': return ['podman-compose']
        if provider=='podman': return ['podman','compose']
        if provider=='docker': return ['docker','compose']
        if provider!='auto': raise ValueError('Unsupported COMPOSE_PROVIDER')
    if engine=='podman' and shutil.which('podman-compose'): return ['podman-compose']
    return [engine,'compose']

def planned_ports(project,data):
    if project=='elasticsearch':
        bind=data.get('ES_BIND_IP','127.0.0.1')
        keys=('ES_PORT','CEREBRO_PORT','KIBANA_PORT')
        defaults=(9200,9000,5601)
    elif project=='elasticsearch9':
        bind=data.get('ES_BIND_IP','127.0.0.1')
        keys=('ES_PORT','KIBANA_PORT')
        defaults=(9201,5602)
    elif project=='kafka':
        bind=data.get('BIND_IP','127.0.0.1');count=int(data.get('NODES','3'))
        if not 1<=count<=100: raise ValueError('NODES must be 1..100')
        if data.get('KAFKA_MODE','zk')=='zk' and count%2==0: raise ValueError('ZooKeeper requires odd NODES')
        keys=tuple(f'KAFKA{i}_PORT' for i in range(1,count+1))
        defaults=tuple(19092+(i-1)*10000 if i<=3 else 40000+(i-4)*100 for i in range(1,count+1))
    elif project=='mariadb':
        bind=data.get('BIND_ADDRESS','127.0.0.1')
        count=int(data.get('NODE_COUNT','3'))
        keys=tuple(f'NODE{i}_PORT' for i in range(1,count+1))+('WRITER_PORT','READER_PORT','DASHBOARD_PORT','HAPROXY_STATS_PORT')
        defaults=tuple(13300+i for i in range(1,count+1))+(13306,13307,18081,18084)
    else: return []  # Redis's normal profile publishes no host ports.
    ip=ipaddress.ip_address(bind)
    if ip.version!=4: raise ValueError('Only IPv4 host publishing is supported by this preflight')
    ports=[]
    for key,default in zip(keys,defaults):
        port=int(data.get(key,default))
        if not 1<=port<=65535: raise ValueError(f'{key}: port out of range')
        ports.append((bind,port,key))
    return ports

def preflight(projects):
    errors=[];warnings=[];plans=[];engines={}
    for project in projects:
        try:
            data=settings(project)
            if project in ES_LIKE and not ipaddress.ip_address(data.get('ES_BIND_IP','127.0.0.1')).is_loopback:
                if data.get('ES_ALLOW_PUBLIC_BIND','no')!='yes':
                    errors.append(f'{project}: existing public binding requires ES_ALLOW_PUBLIC_BIND=yes; loopback is recommended')
                else: warnings.append(f'{project}: explicitly enabled unauthenticated remote binding')
            engine=engine_for(project,data)
            engines[project]=engine
            if not shutil.which(engine): errors.append(f'{project}: {engine} is missing')
            compose=compose_command(project,engine,data)
            if not shutil.which(compose[0]):
                errors.append(f'{project}: {compose[0]} is missing')
            else:
                version_cmd=['podman-compose','--version'] if compose[0]=='podman-compose' else [*compose,'version']
                result=subprocess.run(version_cmd,capture_output=True,text=True,timeout=10)
                if result.returncode:errors.append(f'{project}: {" ".join(compose)} is unavailable')
            for bind,port,key in planned_ports(project,data):
                for old in plans:
                    if port==old['port'] and (bind==old['bind'] or '0.0.0.0' in (bind,old['bind'])):
                        errors.append(f"Port collision: {project}.{key} / {old['project']}.{old['key']} ({port})")
                plans.append({'project':project,'key':key,'bind':bind,'port':port})
                # A listener may belong to a lab already running. Do not kill it or
                # falsely call a successful re-run a conflict. Native ownership checks remain required.
                sock=socket.socket()
                try:sock.bind((bind,port))
                except OSError:warnings.append(f'{project}: {bind}:{port} is busy/unavailable; verify ownership with native doctor before changing it')
                finally:sock.close()
        except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
            errors.append(f'{project}: {exc}')
    for engine in sorted(set(engines.values())):
        if not shutil.which(engine):continue
        try:
            p=subprocess.run([engine,'info'],capture_output=True,text=True,timeout=15)
            if p.returncode:errors.append(f'{engine}: engine info failed; daemon/rootless environment is not ready')
        except (OSError,subprocess.TimeoutExpired):errors.append(f'{engine}: engine info timed out or failed')
    if 'elasticsearch' in projects or 'elasticsearch9' in projects:
        try:
            if int(Path('/proc/sys/vm/max_map_count').read_text())<262144:errors.append('vm.max_map_count < 262144; no automatic sysctl/firewall changes are performed')
        except (OSError,ValueError):warnings.append('vm.max_map_count could not be verified on this host')
    resources={'logical_cpus':os.cpu_count(),'workspace_free_bytes':shutil.disk_usage(ROOT).free}
    try:
        memory=dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
        resources['host_available_memory_bytes']=int(memory['MemAvailable'].split()[0])*1024
        limit=Path('/sys/fs/cgroup/memory.max').read_text().strip()
        resources['cgroup_memory_limit_bytes']=int(limit) if limit!='max' else None
    except (OSError,KeyError,ValueError):pass
    if len(projects)>1:warnings.append('All selected labs accumulate CPU/RAM/storage usage; this is not a capacity or port-ownership certification')
    return {'kind':'preflight','checked_at':now(),'ok':not errors,'projects':projects,'errors':errors,'warnings':warnings,'published_ports':plans,'resources':resources}

def health(projects):
    checks=[]
    for project in projects:
        start=time.monotonic();row={'project':project,'ready':False}
        try:
            if not (ROOT/DIRS[project]/'.env').is_file():raise ValueError('not initialized')
            if project in ES_LIKE:
                data=settings(project);url=data.get('ES_URL','http://127.0.0.1:9200').rstrip('/')
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url+'/_cluster/health',timeout=8) as response: info=json.load(response)
                row['ready']=(info.get('cluster_name')==data.get('LAB_CLUSTER_NAME','cerebro-shard-lab') and info.get('status')=='green' and info.get('number_of_nodes',0)>=5 and not info.get('timed_out'))
                row['details']={k:info.get(k) for k in ('cluster_name','status','number_of_nodes','unassigned_shards')}
            else:
                args=['bash',str(ROOT/DIRS[project]/'lab.sh'),'health']
                if project!='kafka':args.append('--json')
                p=subprocess.run(args,cwd=ROOT/DIRS[project],capture_output=True,text=True,timeout=45,
                                 env=dict(os.environ,STARTUP_TIMEOUT='20'))
                row['ready']=p.returncode==0;row['exit_code']=p.returncode
                # Native output may contain data or credentials; do not mirror it into receipts.
                if p.returncode:row['reason']='native health probe failed; inspect the project health command'
        except subprocess.TimeoutExpired:row['reason']='health probe timed out'
        except (OSError,ValueError) as exc:row['reason']=str(exc)[:200]
        row['elapsed_seconds']=round(time.monotonic()-start,3);checks.append(row)
    return {'kind':'health','checked_at':now(),'ready':all(c['ready'] for c in checks),'scope':'read-only application/topology checks; not a write durability or failover test','checks':checks}

def record(path,action,project,code):
    if path.exists():data=json.loads(path.read_text())
    else:data={'run_id':path.stem,'started_at':now(),'action':action,'status':'running','results':[],
               'controller_sha256':hashlib.sha256((ROOT/'all.sh').read_bytes()).hexdigest(),'health_verified':False}
    if project=='__finish__':data.update(status='completed' if code==0 else 'failed',exit_code=code,finished_at=now())
    elif project!='__start__':data['results'].append({'project':project,'exit_code':code,'status':'command_ok' if code==0 else ('skipped' if code==-1 else 'failed'),'at':now()})
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.receipt-')
    try:
        with os.fdopen(fd,'w') as stream:json.dump(data,stream,indent=2);stream.write('\n')
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def main():
    if len(sys.argv)>1 and sys.argv[1]=='record':
        record(Path(sys.argv[2]),sys.argv[3],sys.argv[4],int(sys.argv[5]));return
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=('preflight','health'));p.add_argument('--json',action='store_true');p.add_argument('projects',nargs='*');a=p.parse_args()
    if any(x not in DIRS for x in a.projects):p.error('projects must be one of: '+', '.join(DIRS))
    result=(preflight if a.command=='preflight' else health)(a.projects or list(DEFAULT_PROJECTS))
    if a.json:print(json.dumps(result,indent=2))
    elif a.command=='preflight':
        for text in result['errors']:print('ERROR:',text)
        for text in result['warnings']:print('WARNING:',text)
        print('Preflight:', 'PASS' if result['ok'] else 'FAIL')
    else:
        for c in result['checks']:print(c['project']+': '+('READY' if c['ready'] else 'NOT READY'))
    if not result.get('ok',result.get('ready',False)):raise SystemExit(1)

if __name__=='__main__':main()
