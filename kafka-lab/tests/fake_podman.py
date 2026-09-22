#!/usr/bin/env -S python3 -S
"""Isolated command-routing test double. NEVER a live Kafka test."""
import json
import os
from pathlib import Path
import sys

p=Path(os.environ['FAKE_STATE'])
s=json.loads(p.read_text())
a=sys.argv[1:]
s.setdefault('calls',[]).append(a)
rc=0; out=''
def save(): p.write_text(json.dumps(s))
def container(name): return s['containers'][name]
try:
    if a[:2]==['container','exists']:
        rc=0 if a[2] in s['containers'] else 1
    elif a[:2] in [['volume','exists'],['network','exists']]: rc=1
    elif a[0]=='inspect':
        name=a[-1]; c=container(name); fmt=a[2]
        if '.Config.Labels' in fmt: out=c['owner']
        elif '.State.Running' in fmt: out=str(c['running']).lower()
        elif '.State.Paused' in fmt: out=str(c['paused']).lower()
        elif '.NetworkSettings.Networks' in fmt: out='yes' if c['connected'] else ''
        else: raise RuntimeError('unhandled inspect '+fmt)
    elif a[0] in ['stop','kill','start','pause','unpause']:
        c=container(a[-1])
        if a[0] in ['stop','kill']: c['running']=False
        elif a[0]=='start': c['running']=True
        else: c['paused']=a[0]=='pause'
    elif a[:2]==['network','disconnect']: container(a[-1])['connected']=False
    elif a[:2]==['network','connect']: container(a[-1])['connected']=True
    elif a[0]=='exec':
        if 'zookeeper-shell' in ' '.join(a):
            out='[]'
        elif 'client.py' in ' '.join(a):
            sub=a[a.index('/opt/lab/client.py')+1]
            if sub=='wait' and os.getenv('FAKE_FAIL_WAIT')=='1': rc=12
            elif sub in ['wait','zk-status']: out='{"mock": true}'
            else: out='{"mock_command": '+json.dumps(sub)+'}'
        else: out='mock CLI'
    elif a[0]=='logs': out='mock logs'
    elif a[0]=='ps': out='mock ps'
    elif a[0]=='--version': out='podman version 5.4.0 [MOCK]'
    elif a[0]=='info': out='rootless=true network=netavark [MOCK]'
    else: raise RuntimeError('unhandled fake podman '+repr(a))
except Exception as e:
    print(str(e),file=sys.stderr); rc=90
save()
if out: print(out)
sys.exit(rc)
