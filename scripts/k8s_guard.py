#!/usr/bin/env python3
"""Guard an explicitly selected lab context; no auto-rotation or data deletion."""
from __future__ import annotations
import argparse
import base64
import json
import re
import secrets
import subprocess
import sys
import tempfile

KEYS=('redis-password','mariadb-root-password','mariadb-sst-password')

def valid_password(value,max_length=128):
    return bool(re.fullmatch(fr'[A-Za-z0-9_-]{{24,{max_length}}}',value))

def helm_list_all_flag(help_text):
    # Helm 3 uses --all; Helm 4 lists every status by default and removed the flag.
    return ['--all'] if re.search(r'^\s*-a,\s+--all(?:\s|$)',help_text,re.M) else []

class Guard:
    def __init__(self,args):
        self.args=args
        self.kube=['kubectl','--context',args.context,'--namespace',args.namespace]
        self.helm=['helm']
        if args.kubeconfig:
            self.kube+=['--kubeconfig',args.kubeconfig]
    def call(self,args,content=None,timeout=30):
        p=subprocess.run(args,input=content,capture_output=True,text=True,timeout=timeout)
        if p.returncode:
            # Do not print raw kubectl/Helm stdout: it may include secret material.
            raise ValueError(f'{args[0]} {" ".join(args[1:2])} failed (exit {p.returncode}); inspect the explicit lab target with read-only commands')
        return p.stdout
    def get(self,kind,name=None):
        argv=self.kube+['get',kind]+([name] if name else [])+['-o','json']
        if name:argv+=['--ignore-not-found']
        text=self.call(argv)
        return json.loads(text) if text.strip() else None
    def check_secret(self,name,required):
        obj=self.get('secret',name)
        if not obj:raise ValueError(f'Missing Secret {name}; run all.sh k8s init first (set DB_LAB_SECRET for a custom name)')
        for key in required:
            try:value=base64.b64decode(obj.get('data',{})[key],validate=True).decode()
            except (ValueError,UnicodeDecodeError,KeyError):raise ValueError(f'Secret {name}: missing or invalid {key}') from None
            maximum=32 if key.startswith('mariadb-') else 128
            if not valid_password(value,maximum):raise ValueError(f'Secret {name}/{key} must be 24..{maximum} URL-safe characters; default/weak passwords are refused')
    def initialize(self):
        ns=self.get('namespace',self.args.namespace)
        if not ns:
            self.call(self.kube+['create','namespace',self.args.namespace])
        if self.get('secret',self.args.secret):
            self.check_secret(self.args.secret,KEYS)
            print('Existing credentials preserved; no rotation performed.',file=sys.stderr);return
        obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':self.args.secret,'namespace':self.args.namespace,
             'labels':{'app.kubernetes.io/part-of':'db-lab'}},'type':'Opaque',
             'stringData':{key:secrets.token_urlsafe(24) for key in KEYS}}
        # Payload uses stdin, never argv, a checked-in file, or a log.
        self.call(self.kube+['create','-f','-'],json.dumps(obj))
        print('Created lab credentials (values not printed).',file=sys.stderr)
    def preflight(self):
        try:import yaml
        except ImportError:raise ValueError('PyYAML is required for Kubernetes manifest preflight: install scripts/requirements-checks.txt') from None
        self.call(self.kube+['get','namespace',self.args.namespace,'-o','name'])
        render=['helm','template',self.args.release,self.args.chart,'--namespace',self.args.namespace]
        # Match the controller's --reset-then-reuse-values semantics: current
        # explicitly supplied values, then caller overrides, on NEW chart defaults.
        target=['--namespace',self.args.namespace,'--kube-context',self.args.context]
        if self.args.kubeconfig:target+=['--kubeconfig',self.args.kubeconfig]
        list_args=helm_list_all_flag(self.call(['helm','list','--help']))
        releases=json.loads(self.call(['helm','list',*list_args,'--filter','^'+re.escape(self.args.release)+'$','-o','json']+target))
        if any(str(x.get('chart','')).startswith('db-lab-0.1.') for x in releases):
            raise ValueError('Chart 0.1.x requires a new namespace/release and verified backup/restore; in-place migration is refused')
        prior={}
        if releases:
            prior=json.loads(self.call(['helm','get','values',self.args.release,'-o','json']+target)) or {}
        with tempfile.TemporaryDirectory(prefix='db-lab-values-') as tmp:
            if prior:
                from pathlib import Path
                path=Path(tmp)/'previous-values.json'
                path.write_text(json.dumps(prior));path.chmod(0o600)
                render+=['-f',str(path)]
            render+=self.args.helm_args
            docs=[x for x in yaml.safe_load_all(self.call(render,timeout=60)) if x]
        states=[x for x in docs if x.get('kind')=='StatefulSet']
        secrets_needed={};bootstrap_states=[];recovery_states=[]
        for obj in states:
            name=obj['metadata']['name']
            expected=obj['spec']['selector']['matchLabels']
            old=self.get('statefulset',name)
            if old and old['spec']['selector']['matchLabels']!=expected:
                raise ValueError(f'{name}: immutable selector migration required; see helmchart/README.md. No forced replacement is performed.')
            for container in obj['spec']['template']['spec']['containers']:
                env={e['name']:e.get('value') for e in container.get('env',[])}
                if env.get('BOOTSTRAP_NEW_CLUSTER')=='yes' or 'cluster.initial_master_nodes' in env:
                    bootstrap_states.append(obj)
                if env.get('RECOVERY_ORDINAL') not in (None,'-1'):
                    recovery_states.append(obj)
                for e in container.get('env',[]):
                    ref=e.get('valueFrom',{}).get('secretKeyRef')
                    if ref:secrets_needed.setdefault(ref['name'],set()).add(ref['key'])
        for name,keys in secrets_needed.items():self.check_secret(name,keys)
        pvcs=self.get('pvc').get('items',[])
        names={p['metadata']['name'] for p in pvcs}
        for obj in bootstrap_states:
            for claim in obj['spec'].get('volumeClaimTemplates',[]):
                for i in range(obj['spec']['replicas']):
                    expected=f"{claim['metadata']['name']}-{obj['metadata']['name']}-{i}"
                    if expected in names:
                        raise ValueError(f'Fresh bootstrap refused: PVC {expected} already exists; unset bootstrapNewCluster and use the documented recovery procedure')
        classes=self.get('storageclass').get('items',[])
        defaults={x['metadata']['name'] for x in classes if x['metadata'].get('annotations',{}).get('storageclass.kubernetes.io/is-default-class')=='true'}
        available={x['metadata']['name'] for x in classes}
        for obj in states:
            for claim in obj['spec'].get('volumeClaimTemplates',[]):
                spec=claim['spec'];sc=spec.get('storageClassName')
                if sc and sc not in available:raise ValueError(f'StorageClass {sc} not found')
                if sc is None and not defaults:raise ValueError('No default StorageClass; select storageClassName explicitly (empty string only for pre-provisioned static PVs)')
                if sc=='':print('WARNING: Static PV provisioning must be checked separately.',file=sys.stderr)
        result={'namespace':self.args.namespace,'release':self.args.release,'statefulsets':len(states),
                'secret_names':sorted(secrets_needed),'seal_needed':bool(bootstrap_states or recovery_states),
                'limits':'No image pull, application health, PVC durability, CNI, resource-capacity, or failover verification is performed here'}
        print(json.dumps(result))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('init','preflight'));p.add_argument('--context',required=True);p.add_argument('--namespace',required=True)
    p.add_argument('--release',default='db-lab');p.add_argument('--secret',default='db-lab-credentials');p.add_argument('--chart');p.add_argument('--kubeconfig')
    args,extra=p.parse_known_args();args.helm_args=extra[1:] if extra and extra[0]=='--' else extra
    if not re.fullmatch(r'[a-z0-9]([-a-z0-9.]*[a-z0-9])?',args.secret):p.error('invalid Secret name')
    try:
        guard=Guard(args)
        if args.action=='init':guard.initialize()
        else:guard.preflight()
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:raise SystemExit('ERROR: '+str(exc))
if __name__=='__main__':main()
