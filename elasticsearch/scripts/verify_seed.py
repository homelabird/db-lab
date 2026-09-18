#!/usr/bin/env python3
"""Live integration smoke test. Read-only except PIT open/close in the optional page test."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from lablib import ESClient, INDICES, LAYOUT, ROOT, execute_example, load_catalog, write_json


def verify(client, manifest, min_nodes=5):
    if manifest.get('status') != 'loaded':
        raise RuntimeError('Manifest must have status=loaded; generate-only output is not evidence of indexing.')
    info=client.assert_lab()
    if info['cluster_uuid'] != manifest.get('cluster_uuid'):
        raise RuntimeError('Manifest belongs to another cluster UUID; seed this cluster first.')
    health=client.request('GET','/_cluster/health/'+','.join(INDICES)+'?wait_for_status=green&wait_for_no_relocating_shards=true&wait_for_no_initializing_shards=true&timeout=120s',timeout=130)
    if health.get('timed_out') or health.get('status')!='green':
        raise RuntimeError(f'Lab shards are not stably green: {health}. Restore fault scenarios first.')
    nodes=client.request('GET','/_nodes')
    data_nodes=sum(any(role=='data' or role.startswith('data_') for role in n.get('roles',[])) for n in nodes['nodes'].values())
    if data_nodes<min_nodes: raise RuntimeError(f'Expected >= {min_nodes} data nodes, got {data_nodes}')
    checks=[]
    for index in INDICES:
        detail=client.request('GET',f'/{index}')[index]
        settings=detail['settings']['index']
        expected=manifest['indices'][index]
        actual=client.request('GET',f'/{index}/_count')['count']
        if actual!=expected['documents']: raise RuntimeError(f'{index}: expected={expected["documents"]}, actual={actual}; stop live load / clean up practice data.')
        if (int(settings['number_of_shards']),int(settings['number_of_replicas']))!=LAYOUT[index][:2]:
            raise RuntimeError(f'{index}: shard settings differ from the baseline; restore replica/allocation scenarios.')
        signature=detail['mappings'].get('_meta',{}).get('seed_lab',{}).get('signature')
        if signature!=expected['signature']: raise RuntimeError(f'{index}: mapping seed signature mismatch')
        checks.append({'index':index,'documents':actual,'status':'PASS'})
    shards=client.request('GET','/_cat/shards/'+','.join(INDICES)+'?format=json')
    p=sum(s['prirep']=='p' for s in shards); r=sum(s['prirep']=='r' for s in shards)
    expected_p = sum(value[0] for value in LAYOUT.values())
    expected_r = sum(value[0] * value[1] for value in LAYOUT.values())
    if (p,r)!=(expected_p, expected_r) or any(s['state']!='STARTED' for s in shards):
        raise RuntimeError(f'Expected {expected_p} primary + {expected_r} replica STARTED copies; got p={p}, r={r}')
    for index in INDICES:
        for shard in range(LAYOUT[index][0]):
            copies=[s for s in shards if s['index']==index and int(s['shard'])==shard]
            if len(copies)!=1+LAYOUT[index][1] or len({s['node'] for s in copies})!=len(copies):
                raise RuntimeError(f'Unexpected copy count or shared host for {index} shard {shard}')
    examples=[]
    for entry in load_catalog():
        result=execute_example(client,entry)
        if entry['name'] in ('02-user','03-message','04-high-risk','05-failed-login','07-fraud','08-http-5xx'):
            if result.get('hits',{}).get('total',{}).get('value',0)<=0:
                raise RuntimeError(f'Expected known synthetic incident matches: {entry["name"]}')
        examples.append({'name':entry['name'],'status':'PASS'})
        print(f'[PASS] {entry["name"]}',flush=True)
    return {'status':'PASS','checked_at':datetime.now(timezone.utc).isoformat(),
            'data_nodes':data_nodes,'primary_shards':p,'replica_shards':r,
            'indices':checks,'query_examples':examples,'note':'Live REST verification, not browser/Cerebro UI verification'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,default=ROOT/'reports/seed-manifest.json')
    p.add_argument('--report',type=Path,default=ROOT/'reports/live-verification.json')
    p.add_argument('--min-nodes',type=int,default=5)
    args=p.parse_args()
    try:
        manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
        result=verify(ESClient(),manifest,args.min_nodes)
    except (RuntimeError,ValueError,OSError) as exc:
        write_json(args.report,{'status':'FAIL','error':str(exc)})
        raise
    write_json(args.report,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try: main()
    except (RuntimeError,ValueError,OSError) as exc: sys.exit(f'[error] {exc}')
