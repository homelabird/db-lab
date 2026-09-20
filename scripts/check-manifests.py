#!/usr/bin/env python3
"""Check rendered YAML selectors/probes/resources, not Kubernetes admission."""
import argparse
import json
import sys
import yaml
p=argparse.ArgumentParser(description=__doc__);p.add_argument('file');p.add_argument('release');args=p.parse_args()
with open(args.file) as stream:docs=[d for d in yaml.safe_load_all(stream) if d]
states=[d for d in docs if d.get('kind')=='StatefulSet'];errors=[]
for state in states:
    spec=state['spec'];pod=spec['template'];name=state['metadata']['name']
    labels=pod['metadata']['labels'];selector=spec['selector']['matchLabels']
    if selector.get('app.kubernetes.io/instance')!=args.release:errors.append(name+': wrong release selector')
    if not all(labels.get(k)==v for k,v in selector.items()):errors.append(name+': Pod/StatefulSet selector mismatch')
    for c in pod['spec']['containers']:
        if not c.get('readinessProbe') or not c.get('startupProbe'):errors.append(name+': missing probe')
        if not all(c.get('resources',{}).get(k) for k in ('requests','limits')):errors.append(name+': missing resources')
for svc in [d for d in docs if d.get('kind')=='Service']:
    selector=svc['spec'].get('selector',{});name=svc['metadata']['name']
    matches=[s for s in states if all(s['spec']['template']['metadata']['labels'].get(k)==v for k,v in selector.items())]
    if len(matches)!=1:errors.append(name+': selector must match exactly one StatefulSet Pod template')
    if selector.get('app.kubernetes.io/instance')!=args.release:errors.append(name+': missing release selector')
if not states:errors.append('No StatefulSets rendered')
print(json.dumps({'release':args.release,'objects':len(docs),'statefulsets':len(states),'errors':errors,'scope':'manifest contract only'}))
sys.exit(1 if errors else 0)
