#!/usr/bin/env python3
"""Host-side extension. The original HA manager is preserved, not replaced."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import manage
ROOT=manage.ROOT
sys.path.insert(0,str(ROOT.parent/'lib'))
from db_lab_benchmark import HostPressureSampler, environment as host_benchmark_environment
sys.path.insert(0,str(ROOT))
from ops.common import atomic_json,render
from ops.resp import parse_info

NODES=('redis-perf','ops-runner','net-proxy')
VOLUMES=('ops-perf-data','ops-results')
LABEL='io.redis-sentinel-lab.ops'
SCENARIOS=('bigkey','fragmentation','eviction','connections','slow-consumer','persistence','latency','cache-stampede','stream-pending')

class Ops:
    def __init__(self,env):self.base=manage.Lab(env);self.env=env;self.name=env['LAB_NAME']
    def name_of(self,node):
        if node not in NODES:raise ValueError('Not an operations-lab container')
        return self.name+'-'+node
    def inspect(self,node,required=True):
        result=self.base.run(['podman','container','inspect',self.name_of(node)],check=False)
        if result.returncode:
            if required:raise RuntimeError('Run ./ops.sh up first: '+node+' is missing')
            return None
        obj=json.loads(result.stdout)[0]
        if (obj.get('Config',{}).get('Labels') or {}).get(LABEL)!=self.name:
            raise RuntimeError('Refused: container is not owned by this operations lab')
        return obj
    def volume(self,suffix):
        if suffix not in VOLUMES:raise ValueError('Unsafe volume')
        result=self.base.run(['podman','volume','inspect',self.name+'-'+suffix],check=False)
        if result.returncode:return None
        obj=json.loads(result.stdout)[0]
        if (obj.get('Labels') or {}).get(LABEL)!=self.name:raise RuntimeError('Refused: unowned operations volume')
        return obj
    def compose(self,*args):
        return self.base.run(['podman-compose','--in-pod=false','-p',self.name+'-ops','-f',str(ROOT/'compose.ops.yaml'),*args],capture=False,timeout=None)
    def execute(self,node,args,capture=False,check=True,timeout=None,child_env=None):
        self.inspect(node)
        command=['podman','exec']
        for key,value in sorted((child_env or {}).items()):command.extend(['--env',key+'='+value])
        return self.base.run([*command,self.name_of(node),*args],capture=capture,check=check,timeout=timeout)
    def runner(self,*args,**kwargs):return self.execute('ops-runner',['python','-m','ops.cli',*map(str,args)],**kwargs)
    def benchmark_environment(self):
        engine_version=self.base.run([self.base.engine,'version','--format','{{.Client.Version}}'],
                                     check=False).stdout.strip() or None
        containers=[self.name+'-redis-'+str(i) for i in range(1,4)]
        containers.extend((self.name_of('redis-perf'),self.name_of('ops-runner')))
        return {**host_benchmark_environment(self.base.engine,containers),
                'container_engine':self.base.engine,'container_engine_version':engine_version}
    def attach_runtime_observation(self,observation):
        script='''import json, pathlib, sys
root=pathlib.Path('/results/ops')
run=root/json.loads((root/'latest.json').read_text())['run_id']
observation=json.loads(sys.argv[1])
for name in ('report.json','benchmark.json'):
    path=run/name
    if path.is_file():
        report=json.loads(path.read_text())
        report.setdefault('environment',{})['runtime_observation']=observation
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\\n')
'''
        self.execute('ops-runner',['python','-c',script,json.dumps(observation,separators=(',',':'))],timeout=10)
    @contextmanager
    def mutation(self):
        path=ROOT/'.lab'/'ops-mutation.lock';path.parent.mkdir(exist_ok=True)
        with path.open('a+') as f:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Another modifying ops command is active. Finish it first.')
            yield
    def up(self,build=True,only=False):
        if only:
            self.base.doctor();self.base.client('wait','--timeout','30')
        else:self.base.up(build=build)
        for node in NODES:self.inspect(node,required=False)
        for suffix in VOLUMES:self.volume(suffix)
        if build:self.compose('build','redis-perf','ops-runner')
        self.compose('up','-d',*NODES)
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            result=self.runner('doctor',capture=True,check=False,timeout=15)
            if result.returncode==0:
                print(result.stdout);print('Operations lab ready. Next: ./ops.sh run bigkey --yes');return
            time.sleep(1)
        raise RuntimeError('Ops startup did not become ready. Inspect ./ops.sh logs redis-perf and net-proxy.')
    def export(self):
        target=ROOT/'output'/('ops-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+str(time.time_ns()%100000))
        target.mkdir(parents=True)
        self.inspect('ops-runner')
        self.base.run(['podman','cp',self.name_of('ops-runner')+':/results/ops/.',target])
        atomic_json(ROOT/'output'/'ops-latest-export.json',{'directory':str(target.relative_to(ROOT))})
        print('Exported: '+str(target));return target

    @staticmethod
    def compare(first,second):
        from ops.compare import compare_reports
        result=compare_reports(first,second)
        print(json.dumps(result,indent=2,ensure_ascii=False))
        return 0 if result['comparable'] else 2
    def latest_report(self):
        script="import json,pathlib;p=pathlib.Path('/results/ops');m=json.loads((p/'latest.json').read_text());print((p/m['run_id']/'report.json').read_text())"
        return json.loads(self.execute('ops-runner',['python','-c',script],capture=True,timeout=10).stdout)
    def perf_file(self,path):
        if path not in ('cpu.max','cpu.stat','memory.max','memory.events'):raise ValueError('Unsupported cgroup file')
        return self.execute('redis-perf',['cat','/sys/fs/cgroup/'+path],capture=True,timeout=10).stdout.strip()
    def restore_cpu(self):
        path=ROOT/'.lab'/'ops-cpu-recovery.json'
        if not path.exists():return {'ok':True,'message':'No CPU mutation journal'}
        data=json.loads(path.read_text());obj=self.inspect('redis-perf')
        if obj.get('Id',obj.get('ID'))!=data['container_id']:
            path.unlink();return {'ok':True,'message':'Old container removed; stale CPU setting not applied to replacement'}
        quota,period=data['cpu_max'].split()
        self.base.run(['podman','update','--cpu-period',period,'--cpu-quota','-1' if quota=='max' else quota,self.name_of('redis-perf')])
        if self.perf_file('cpu.max').split()!=[quota,period]:raise RuntimeError('CPU restoration read-back mismatch; journal retained')
        path.unlink();return {'ok':True,'restored_cpu_max':data['cpu_max']}
    def cpu(self,args):
        out=ROOT/'output'/('cpu-'+str(time.time_ns()));out.mkdir(parents=True)
        report={'scenario':'cpu-quota','status':'RUNNING','phases':{}};path=out/'report.json';atomic_json(path,report)
        journal=ROOT/'.lab'/'ops-cpu-recovery.json';mutated=False
        try:
            if journal.exists():raise RuntimeError('Pending CPU journal; run ./ops.sh recover --yes')
            try:original=self.perf_file('cpu.max')
            except RuntimeError as exc:
                report.update(status='BLOCKED',error='cgroup v2 CPU controller not readable: '+self.base.redact(str(exc)))
                return 2
            self.runner('load','--seconds',args.seconds,'--rate',args.rate,'--workers','16',timeout=args.seconds+60)
            report['phases']['baseline']=self.latest_report()
            info=self.inspect('redis-perf')
            atomic_json(journal,{'container_id':info.get('Id',info.get('ID')),'cpu_max':original})
            mutated=True
            try:
                self.base.run(['podman','update','--cpu-period','100000','--cpu-quota','25000',self.name_of('redis-perf')])
                applied=self.perf_file('cpu.max')
                if applied.split()!=['25000','100000']:raise RuntimeError('CPU limit was not applied')
            except RuntimeError as exc:
                report.update(status='BLOCKED',error=self.base.redact(str(exc)));return 2
            before=dict(x.split() for x in self.perf_file('cpu.stat').splitlines())
            self.runner('load','--seconds',args.seconds,'--rate',args.rate,'--workers','16',timeout=args.seconds+60)
            report['phases']['limited']=self.latest_report()
            after=dict(x.split() for x in self.perf_file('cpu.stat').splitlines())
            report['nr_throttled_delta']=int(after.get('nr_throttled',0))-int(before.get('nr_throttled',0))
            report['applied_cpu_max']=applied
            report['status']='PASS' if report['nr_throttled_delta']>0 else 'NOT_REPRODUCED'
            report['note']='PASS means actual CPU throttling was observed. Compare load rates and latency; it is not a production sizing result.'
            return 0 if report['status']=='PASS' else 3
        except Exception as exc:report.update(status='FAIL',error=self.base.redact(str(exc)));return 1
        finally:
            if mutated:
                try:report['recovery']=self.restore_cpu()
                except Exception as exc:
                    report.update(status='FAIL',recovery_error=self.base.redact(str(exc)))
                    atomic_json(path,report);render(out)
                    raise RuntimeError('CPU recovery failed; inspect journal and run ./ops.sh recover --yes') from exc
            atomic_json(path,report);render(out);print(str(out/'report.html'))
    def replication(self):
        """Owned HA nodes only. Stop one replica, exceed backlog, then verify re-sync."""
        out=ROOT/'output'/('replication-'+str(time.time_ns()));out.mkdir(parents=True)
        report={'scenario':'replication-resync','status':'RUNNING'};path=out/'report.json';atomic_json(path,report)
        journal=ROOT/'.lab'/'ops-replication-recovery.json';master=None;node=None
        try:
            if journal.exists():raise RuntimeError('Pending replication journal; run ./ops.sh recover --yes')
            self.base.client('wait','--timeout','60')
            master=self.base.master();node=next(n for n in manage.REDIS if n!=master)
            cfg=self.base.cli(master,['CONFIG','GET','repl-backlog-size']).stdout.strip().splitlines()[-1]
            atomic_json(journal,{'master':master,'replica':node,'backlog':cfg})
            before=parse_info(self.base.cli(master,['INFO','stats']).stdout)
            self.base.cli(master,['CONFIG','SET','repl-backlog-size','65536'])
            self.base.fault('stop',node)
            # This bounded run creates >1MiB of writes, substantially exceeding a 64KiB backlog.
            self.runner('load','--target','ha','--seconds','8','--rate','400','--workers','8','--keys','4000','--payload','512','--read-pct','0',timeout=120)
            report['load']=self.latest_report()
            if self.base.master()!=master:raise RuntimeError('Unexpected master change during replication exercise')
            self.base.recover(node,wait=True)
            after=parse_info(self.base.cli(master,['INFO','stats']).stdout)
            replica_info=parse_info(self.base.cli(node,['INFO','replication']).stdout)
            delta=after.get('sync_full',0)-before.get('sync_full',0)
            report.update(sync_full_delta=delta,replica=replica_info)
            report['status']='PASS' if delta>0 and replica_info.get('master_link_status')=='up' else 'NOT_REPRODUCED'
            report['note']='sync_full is measured on the master. This isolates/restarts no other replica and does not guarantee every future rejoin is full sync.'
            return 0 if report['status']=='PASS' else 3
        except Exception as exc:report.update(status='FAIL',error=self.base.redact(str(exc)));return 1
        finally:
            if journal.exists():
                try:report['recovery']=self.restore_replication()
                except Exception as exc:
                    report.update(status='FAIL',recovery_error=self.base.redact(str(exc)))
                    atomic_json(path,report);render(out)
                    raise RuntimeError('Replication recovery failed; journal retained') from exc
            atomic_json(path,report);render(out);print(str(out/'report.html'))
    def restore_replication(self):
        path=ROOT/'.lab'/'ops-replication-recovery.json'
        if not path.exists():return {'ok':True,'message':'No replication mutation journal'}
        data=json.loads(path.read_text());master=data['master'];node=data['replica']
        if master not in manage.REDIS or node not in manage.REDIS:raise ValueError('Invalid recovery nodes')
        if not str(data['backlog']).isdigit():raise ValueError('Invalid backlog value in journal')
        self.base.recover(node,wait=True)
        self.base.cli(master,['CONFIG','SET','repl-backlog-size',str(data['backlog'])])
        check=self.base.cli(master,['CONFIG','GET','repl-backlog-size']).stdout.strip().splitlines()[-1]
        if check!=str(data['backlog']):raise RuntimeError('Backlog restoration mismatch')
        path.unlink();return {'ok':True,'original_node':master,'restored_backlog':check}
    def recover(self):
        print(json.dumps(self.restore_cpu(),indent=2));print(json.dumps(self.restore_replication(),indent=2))
        self.runner('recover',timeout=60)
    def down(self,reset=False):
        self.restore_replication()
        if not reset:
            self.restore_cpu()
            runner=self.inspect('ops-runner',required=False);perf=self.inspect('redis-perf',required=False)
            if runner and perf and runner.get('State',{}).get('Running') and perf.get('State',{}).get('Running'):
                self.runner('recover',timeout=60)
        for n in NODES:self.inspect(n,required=False)
        for v in VOLUMES:self.volume(v)
        self.compose('down',*(['-v'] if reset else []))
        if reset:
            (ROOT/'.lab'/'ops-cpu-recovery.json').unlink(missing_ok=True)
            for v in VOLUMES:
                if self.volume(v):self.base.run(['podman','volume','rm',self.name+'-'+v])
        print('Ops stopped. Base HA nodes remain unchanged. Base stop: ./lab.sh down')
    def validate(self,args):
        out=ROOT/'output'/'ops-live-validation.json'
        report={'status':'RUNNING','scope':'Real Podman/Redis integration','steps':[]};atomic_json(out,report)
        try:
            if not shutil.which('podman') or not shutil.which('podman-compose'):
                report.update(status='BLOCKED',error='Podman and podman-compose must be installed');return 2
            self.up(build=not args.no_build,only=args.only)
            self.runner('doctor',timeout=15)
            for name in ('bigkey','eviction','connections','stream-pending','cache-stampede','latency','slow-consumer','persistence','fragmentation'):
                try:previous_id=self.latest_report().get('run_id')
                except Exception:previous_id=None
                result=self.runner('run',name,'--seconds','10' if name!='fragmentation' else '30','--mib','48',capture=True,check=False,timeout=600)
                latest=self.latest_report()
                expected_code={'PASS':0,'FAIL':1,'BLOCKED':2,'NOT_REPRODUCED':3}.get(latest.get('status'))
                if latest.get('run_id')==previous_id or latest.get('scenario')!=name or result.returncode!=expected_code:
                    raise RuntimeError('Missing, stale or inconsistent scenario report: '+name)
                report['steps'].append({'scenario':name,'exit':result.returncode,'status':latest['status'],'run_id':latest['run_id']})
                atomic_json(out,report)
                print(name+': '+latest['status'],flush=True)
                if latest['status']=='FAIL':report['status']='FAIL';break
            else:
                states={x['status'] for x in report['steps']}
                report['status']='PASS' if states=={'PASS'} else ('BLOCKED' if 'BLOCKED' in states else 'NOT_REPRODUCED')
            self.export()
            report['note']='This validates perf scenarios only. CPU quota, replication re-sync and base HA failover are separate explicit tests.'
            return {'PASS':0,'FAIL':1,'BLOCKED':2,'NOT_REPRODUCED':3}[report['status']]
        except Exception as exc:report.update(status='FAIL',error=self.base.redact(str(exc)));return 1
        finally:atomic_json(out,report);print(str(out))


def parser():
    p=argparse.ArgumentParser(description='Redis operations extension — see docs/ops/00-start.md')
    sub=p.add_subparsers(dest='command',required=True)
    for cmd in ('up','validate'):
        q=sub.add_parser(cmd);q.add_argument('--no-build',action='store_true');q.add_argument('--only',action='store_true',help='Use an already running base HA lab')
        if cmd=='validate':q.add_argument('--yes',action='store_true')
    sub.add_parser('list');sub.add_parser('doctor');sub.add_parser('results')
    q=sub.add_parser('compare',help='Compare two exported Redis load reports with identical settings')
    q.add_argument('baseline',type=Path);q.add_argument('candidate',type=Path)
    for cmd in ('down','reset','recover','replication','ha-test'):
        q=sub.add_parser(cmd)
        if cmd!='down':q.add_argument('--yes',action='store_true')
    q=sub.add_parser('logs');q.add_argument('node',choices=NODES)
    q=sub.add_parser('cli');q.add_argument('redis_args',nargs=argparse.REMAINDER)
    q=sub.add_parser('collect');q.add_argument('--seconds',type=int,default=60)
    q=sub.add_parser('load')
    q.add_argument('--target',choices=['perf','ha','proxy'],default='perf')
    for key,default in [('seconds',30),('rate',300),('workers',8),('keys',2000),('payload',256),('read-pct',80),('hot-pct',70)]:q.add_argument('--'+key,type=int,default=default)
    q=sub.add_parser('run');q.add_argument('name',choices=SCENARIOS);q.add_argument('--yes',action='store_true')
    for key,default in [('seconds',20),('mib',64),('fields',50000),('workers',24),('delay-ms',40)]:q.add_argument('--'+key,type=int,default=default)
    q=sub.add_parser('cpu');q.add_argument('--yes',action='store_true');q.add_argument('--seconds',type=int,default=15);q.add_argument('--rate',type=int,default=5000)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    if args.command=='list':print('\n'.join(SCENARIOS));return 0
    if args.command=='compare':return Ops.compare(args.baseline,args.candidate)
    if args.command in ('run','reset','recover','replication','ha-test','cpu','validate') and not args.yes:
        raise ValueError('This command changes the isolated lab. Review scope and rerun with --yes.')
    env=manage.load_env();ops=Ops(env);command=args.command
    if command=='up':
        with ops.mutation():ops.up(build=not args.no_build,only=args.only)
    elif command=='doctor':ops.runner('doctor',timeout=15)
    elif command=='results':ops.export()
    elif command=='logs':
        ops.inspect(args.node);ops.base.run(['podman','logs','--tail','200',ops.name_of(args.node)],capture=False)
    elif command=='cli':
        ops.execute('redis-perf',['sh','-c','export REDISCLI_AUTH="$REDIS_PASSWORD"; exec redis-cli -e --raw "$@"','cli',*args.redis_args])
    elif command in ('load','collect'):
        argv=[command]
        for key,value in vars(args).items():
            if key!='command':argv+=['--'+key.replace('_','-'),str(value)]
        options={'check':False,'timeout':args.seconds+120}
        if command=='load':
            with ops.mutation():
                options['child_env']={'DB_LAB_BENCHMARK_ENV':json.dumps(ops.benchmark_environment(),separators=(',',':'))}
                pressure=HostPressureSampler().start()
                try:result=ops.runner(*argv,**options)
                finally:observation=pressure.stop()
                ops.attach_runtime_observation(observation)
        else:result=ops.runner(*argv,**options)
        return result.returncode
    else:
        with ops.mutation():
            if command=='run':
                argv=['run',args.name]
                for key,value in vars(args).items():
                    if key not in ('command','name','yes'):argv+=['--'+key.replace('_','-'),str(value)]
                return ops.runner(*argv,check=False,timeout=900).returncode
            if command=='recover':ops.recover()
            elif command in ('down','reset'):ops.down(reset=command=='reset')
            elif command=='cpu':
                if not 3<=args.seconds<=60 or not 1<=args.rate<=10000:raise ValueError('seconds 3..60, rate 1..10000')
                return ops.cpu(args)
            elif command=='replication':return ops.replication()
            elif command=='ha-test':ops.base.test_failover()
            elif command=='validate':return ops.validate(args)
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (Exception,KeyboardInterrupt) as exc:
        print(str(exc) or 'Interrupted; inspect recovery journals and use ./ops.sh recover --yes',file=sys.stderr);sys.exit(1)
