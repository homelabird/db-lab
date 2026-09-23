from __future__ import annotations
import argparse
import json
import signal
import sys
import time
from .common import Blocked,Report,Sampler,redact,recover_journal,root,snapshot
from .load import exercise,save_benchmark
from .resp import Settings,Connection,RedisError
from .scenarios import SCENARIOS,run_scenario,proxy_rule


def limited(lo,hi):
    def parse(value):
        n=int(value)
        if not lo<=n<=hi:raise argparse.ArgumentTypeError(f'Expected {lo}..{hi}')
        return n
    return parse

def parser():
    p=argparse.ArgumentParser(description='Bounded Redis operations lab (internal runner)')
    sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('idle');sub.add_parser('doctor');sub.add_parser('list');sub.add_parser('recover')
    run=sub.add_parser('run');run.add_argument('name',choices=SCENARIOS)
    run.add_argument('--seconds',type=limited(3,60),default=20)
    run.add_argument('--mib',type=limited(16,96),default=64)
    run.add_argument('--fields',type=limited(1000,200000),default=50000)
    run.add_argument('--workers',type=limited(4,32),default=24)
    run.add_argument('--delay-ms',type=limited(10,200),default=40)
    load=sub.add_parser('load')
    load.add_argument('--target',choices=['perf','ha','proxy'],default='perf')
    load.add_argument('--seconds',type=limited(1,600),default=30)
    load.add_argument('--rate',type=limited(1,10000),default=300)
    load.add_argument('--workers',type=limited(1,64),default=8)
    load.add_argument('--keys',type=limited(1,50000),default=2000)
    load.add_argument('--payload',type=limited(32,8192),default=256)
    load.add_argument('--read-pct',type=limited(0,100),default=80)
    load.add_argument('--hot-pct',type=limited(0,100),default=70)
    collect=sub.add_parser('collect');collect.add_argument('--seconds',type=limited(1,1800),default=60)
    return p


def main(argv=None):
    args=parser().parse_args(argv);settings=Settings.env()
    if args.cmd=='run':signal.alarm(600)
    elif args.cmd=='load':signal.alarm(args.seconds+90)
    elif args.cmd=='collect':signal.alarm(args.seconds+30)
    if args.cmd=='idle':
        while True:time.sleep(3600)
    if args.cmd=='list':print('\n'.join(SCENARIOS));return 0
    if args.cmd=='doctor':
        with settings.direct() as c:
            info=snapshot(c)
            if info.get('role')!='master' or info.get('connected_slaves',0)!=0:
                raise Blocked('Perf node is not an independent master')
            c.execute('PING')
        state=proxy_rule(settings)
        print(json.dumps({'perf':info,'proxy':state,'pending_recovery':(root()/'recovery.json').exists()},indent=2))
        return 0
    if args.cmd=='recover':
        with settings.direct() as c:result=recover_journal(c)
        result['proxy_reset']=proxy_rule(settings,{'delay_ms':0,'ttl_s':1})
        print(json.dumps(result,indent=2));return 0
    if args.cmd=='run':return run_scenario(settings,args)
    report=Report(args.cmd,vars(args));status='FAIL'
    try:
        if args.cmd=='load':
            with Sampler(report,{args.target:lambda:settings.connection(args.target,timeout=1)}):
                options={k:v for k,v in vars(args).items() if k!='cmd'}
                result=exercise(settings,report,**options)
            status='PASS' if result['counts'].get('success',0)>0 else 'FAIL'
            save_benchmark(report,result,status)
            report.data['note']='PASS means some real requests succeeded and the run completed. It does NOT mean the offered rate was sustainable or that there were no errors; inspect rates, drops and latency.'
        elif args.cmd=='collect':
            factories={'perf':lambda:settings.direct(timeout=1)}
            for i,(host,port) in enumerate(settings.nodes,1):
                factories[f'redis-{i}']=lambda h=host,p=port:Connection(h,p,settings.password,1)
            for i,(host,port) in enumerate(settings.sentinels,1):
                factories[f'sentinel-{i}']=lambda h=host,p=port:Connection(h,p,settings.sentinel_password,1)
            with Sampler(report,factories):time.sleep(args.seconds)
            status='PASS';report.data['note']='Collection completed. Unreachable nodes remain explicitly marked in metrics.jsonl; PASS is not a health verdict.'
    except Blocked as exc:status='BLOCKED';report.data['error']=redact(exc)
    except (Exception,KeyboardInterrupt) as exc:report.data['error']=redact(exc) or 'Interrupted'
    return report.finish(status)

if __name__=='__main__':
    def interrupted(*_):raise KeyboardInterrupt('Interrupted by signal; running recovery')
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGALRM,interrupted)
    try:sys.exit(main())
    except (Exception,KeyboardInterrupt) as exc:
        print(redact(exc),file=sys.stderr);sys.exit(2 if isinstance(exc,Blocked) else 1)
