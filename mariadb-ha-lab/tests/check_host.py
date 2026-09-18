#!/usr/bin/env python3
"""Reproducible host-only checks; report missing optional tools rather than claiming they ran."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parent.parent

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-shellcheck',action='store_true')
    args=parser.parse_args()
    os.chdir(ROOT)
    out=ROOT/'reports/validation';out.mkdir(parents=True,exist_ok=True)
    report={'scope':'HOST_ONLY: includes explicit database/runtime test doubles, not a real DB',
            'real_database_started':False,'utc_time':dt.datetime.now(dt.timezone.utc).isoformat(),
            'python':platform.python_version(),'platform':platform.platform(), 'checks':{}}
    failures=[]
    shell=sorted(ROOT.rglob('*.sh'))
    for path in shell:
        p=subprocess.run(['bash','-n',str(path)],text=True,capture_output=True)
        if p.returncode:failures.append(str(path.relative_to(ROOT))+': '+p.stderr)
    report['checks']['bash_syntax']={'count':len(shell),'status':'FAIL' if failures else 'PASS',
                                    'files':[str(p.relative_to(ROOT)) for p in shell]}
    pyfiles=sorted(p for p in ROOT.rglob('*.py') if '__pycache__' not in p.parts)
    bad=[]
    for path in pyfiles:
        try:compile(path.read_bytes(),str(path),'exec')
        except SyntaxError as exc:bad.append(str(exc))
    failures.extend(bad)
    report['checks']['python_syntax']={'count':len(pyfiles),'status':'FAIL' if bad else 'PASS'}
    shellcheck=shutil.which('shellcheck')
    if shellcheck:
        p=subprocess.run([shellcheck,'-S','warning',*[str(p) for p in shell]],capture_output=True,text=True)
        (out/'shellcheck.log').write_text(p.stdout+p.stderr)
        report['checks']['shellcheck']={'status':'PASS' if p.returncode==0 else 'FAIL'}
        if p.returncode:failures.append('ShellCheck reported errors/warnings; see shellcheck.log')
    else:
        report['checks']['shellcheck']={'status':'NOT_RUN','reason':'shellcheck executable is not installed'}
        if args.require_shellcheck:failures.append('Required ShellCheck is unavailable')
    # Discovery includes actual Bash/subprocess execution, with explicitly stubbed DB commands.
    sys.path.insert(0,str(ROOT/'tests'))
    with (out/'host-tests.log').open('w') as log:
        suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    report['checks']['tests']={'status':'PASS' if result.wasSuccessful() else 'FAIL',
        'run':result.testsRun,'passed':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'skip_details':[{'test':str(t),'reason':reason} for t,reason in result.skipped]}
    if not result.wasSuccessful():failures.append('Regression/unit tests failed; see host-tests.log')
    report['status']='FAIL' if failures else 'PASS'
    report['errors']=failures
    (out/'host-validation.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    print('These are host-only checks. Actual DB validation: RUN_DISRUPTIVE_TESTS=1 bash tests/run-real.sh')
    return 1 if failures else 0

if __name__=='__main__':sys.exit(main())
