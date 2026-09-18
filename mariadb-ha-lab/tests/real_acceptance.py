#!/usr/bin/env python3
"""Real container + SQL acceptance. WARNING: replaces synthetic data; interrupts/rebuilds lab nodes.
No mocks, no simulated SQL, no success on missing prerequisites. Exit 2 means BLOCKED.
This is run synchronously. It leaves containers/volumes in place for inspection.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent.parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',choices=['core','all'],default='all')
    parser.add_argument('--quorum',action='store_true')
    parser.add_argument('--crash-recovery',action='store_true')
    parser.add_argument('--step-timeout',type=int,default=1800)
    args=parser.parse_args()
    if args.step_timeout<30:parser.error('--step-timeout must be >= 30 seconds')
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+str(os.getpid())
    directory=ROOT/'reports/real-acceptance'/stamp;directory.mkdir(parents=True,exist_ok=False)
    report={'scope':'REAL_CONTAINER_AND_SQL_NO_MOCKS','started_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'status':'RUNNING','suite':args.suite,'steps':[], 'real_database_tests_executed':False,
            'optional':{'quorum':'REQUESTED' if args.quorum else 'NOT_RUN',
                        'full_cluster_crash_recovery':'REQUESTED' if args.crash_recovery else 'NOT_RUN'}}
    def save(status=None):
        if status:report['status']=status
        text=json.dumps(report,indent=2,ensure_ascii=False)+'\n'
        (directory/'result.json').write_text(text)
        (directory.parent/'latest.json').write_text(text)
    def blocked(reason):
        report['reason']=reason;save('BLOCKED')
        print('BLOCKED: '+reason,file=sys.stderr)
        print('Report: '+str(directory/'result.json'),file=sys.stderr)
        return 2
    if os.environ.get('RUN_DISRUPTIVE_TESTS')!='1':
        return blocked('Explicit approval required: RUN_DISRUPTIVE_TESTS=1. Only use a disposable lab: commerce_lab and restore data are replaced.')
    report['runtime_paths']={name:shutil.which(name) for name in ('podman','podman-compose','docker','mariadbd','shellcheck')}
    if not (shutil.which('podman') or shutil.which('docker')):
        return blocked('Neither Podman nor Docker is installed. No containers or SQL tests were started; this is NOT a passing test.')
    secrets=[]
    def redact(text):
        for value in secrets:text=text.replace(value,'<redacted>')
        return text
    def step(label,command):
        number=len(report['steps'])+1
        print('[%02d] %s'%(number,label),flush=True)
        record={'name':label,'command':command,'status':'RUNNING'}
        report['steps'].append(record);save()
        logpath=directory/('%02d.log'%number)
        begin=time.monotonic()
        try:
            # File-backed capture avoids unbounded RAM during a build/seed.
            with logpath.open('w+') as output:
                process=subprocess.Popen(command,cwd=ROOT,env=os.environ,stdout=output,stderr=subprocess.STDOUT,
                                         start_new_session=True,text=True)
                try:code=process.wait(timeout=args.step_timeout)
                except BaseException:
                    # Terminate only this test command's process group, never a global runtime.
                    import signal
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:os.killpg(process.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                        process.wait()
                    raise
                output.seek(0);text=redact(output.read())
            logpath.write_text(text);logpath.chmod(0o600)
            record.update(exit_code=code,duration_seconds=round(time.monotonic()-begin,3),
                          log=logpath.name,status='PASS' if code==0 else 'FAIL')
            print(text[-6000:],end='' if text.endswith('\n') else '\n',flush=True)
            save()
            if code:raise RuntimeError(label+' failed with exit code '+str(code))
        except BaseException as exc:
            record.update(status='FAIL',error=redact(str(exc)),duration_seconds=round(time.monotonic()-begin,3))
            save();raise
    def cli(*command):return ['bash','lab.sh',*command]
    try:
        step('Initialize/validate configuration',cli('init'))
        sys.path.insert(0,str(ROOT/'scripts'))
        import lab
        config=lab.load_env(ROOT/'.env');secrets[:]=[config[key] for key in lab.PASSWORDS]
        instance=lab.Lab()
        report['engine']=instance.engine;report['compose_provider']=instance.compose_base
        # Runtime/Compose access must be real, not merely a binary path check.
        step('Runtime information',[instance.engine,'info'])
        step('Compose provider version',instance.compose_base+['version'])
        step('Configuration/provider/port diagnosis',cli('doctor'))
        step('Stop existing lab containers safely; keep volumes',cli('down'))
        step('Build actual images',cli('build'))
        report['database_start_attempted']=True;save()
        step('Three-node bootstrap and proxy/HTTP readiness',cli('up'))
        report['real_database_tests_executed']=True
        step('Synthetic dataset tiny replacement',cli('seed','--size','tiny','--replace','--confirm-replace'))
        step('Membership, SQL replication, data counts, readonly permission',cli('verify'))
        step('Writer and reader routes',cli('routes'))
        step('Concurrent idempotent transactions',cli('load','--seconds','10','--workers','2'))
        step('Cross-node certification conflict with assertions',cli('conflict'))
        step('Writer node crash and acknowledged-write preservation',cli('scenario','node-failure','--node','galera1','--hold','25'))
        if args.suite=='all':
            for label,command in (
                ('Create and measure slow query',('slow','--rows','5000','--leave-broken')),
                ('Fix and remeasure slow query',('fix','slow')),
                ('Create and measure fragmentation',('fragmentation','--rows','5000','--payload-bytes','1024','--leave-broken')),
                ('Rebuild fragmented table and verify rows',('fix','fragmentation')),
                ('Real InnoDB lock wait',('lock','--hold','6')),
                ('Paused writer node recovery',('node-hang','--node','galera1','--hold','30'))):
                step(label,cli('scenario',*command))
        step('SST rebuild of one lab node',cli('rebuild','galera3','--confirm-rebuild'))
        step('Verify rebuilt node',cli('verify'))
        step('Sequential shutdown retaining volumes',cli('down'))
        step('Restart with existing named containers/volumes',cli('up'))
        step('Verify restarted cluster',cli('verify'))
        step('Logical backup with checksum',cli('backup'))
        step('Restore into isolated standalone node',cli('restore','--confirm-restore'))
        step('Verify source after isolated restore',cli('verify'))
        if args.quorum:
            step('Quorum loss and writer rejection',cli('scenario','quorum','--hold','45','--confirm-quorum'))
            step('Verify quorum recovery',cli('verify'))
            report['optional']['quorum']='PASS'
        if args.crash_recovery:
            step('Kill all three lab DB processes',[instance.engine,'kill','--signal','KILL',*[instance.name(n) for n in lab.NODES]])
            step('Recover all offline positions without bootstrapping',cli('recover'))
            step('Bootstrap highest verified recovered position',cli('recover','--execute','--confirm-recovery'))
            step('Verify full cluster crash recovery',cli('verify'))
            report['optional']['full_cluster_crash_recovery']='PASS'
        report['finished_at']=dt.datetime.now(dt.timezone.utc).isoformat();save('PASS')
        print('PASS: all REQUESTED real-runtime steps succeeded. Omitted optional tests remain NOT_RUN.')
        print('Report: '+str(directory/'result.json'))
        print('Lab left running. Stop without deleting data: bash lab.sh down')
        return 0
    except BaseException as exc:
        report['error']=redact(str(exc));save('FAIL')
        # Best-effort scoped diagnostics. Never reset/prune/wipe on test failure.
        for name,command in [('status',cli('status')),*[(n,cli('logs',n,'--tail','120')) for n in ('galera1','galera2','galera3')]]:
            try:
                result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=35)
                path=directory/('failure-'+name+'.log');path.write_text(redact(result.stdout+result.stderr));path.chmod(0o600)
            except (OSError,subprocess.TimeoutExpired):pass
        print('FAIL: '+report['error'],file=sys.stderr)
        print('No volumes deleted by cleanup. Evidence: '+str(directory),file=sys.stderr)
        return 130 if isinstance(exc,KeyboardInterrupt) else 1

if __name__=='__main__':sys.exit(main())
