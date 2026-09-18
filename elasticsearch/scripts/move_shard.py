#!/usr/bin/env python3
"""Choose a data node that has no copy of this shard; dry-run allocation before moving."""
import os
import sys
from lablib import APIError, ESClient, INDICES


def main():
    client=ESClient(); client.assert_lab()
    index=os.getenv('INDEX',INDICES[0]); shard=int(os.getenv('SHARD','0'))
    if index not in INDICES or shard<0: raise ValueError('Use one of the seed indices and SHARD >= 0')
    rows=client.request('GET',f'/_cat/shards/{index}?format=json')
    copies=[row for row in rows if int(row['shard'])==shard]
    source=next((row.get('node') for row in copies if row['prirep']=='p' and row['state']=='STARTED'),None)
    if not source: raise RuntimeError('No STARTED primary. Wait for recovery or select another shard.')
    if any(row['state'] not in ('STARTED','UNASSIGNED') for row in copies):
        raise RuntimeError('This shard is recovering/relocating; wait before trying another move.')
    occupied={row.get('node') for row in copies}
    nodes=client.request('GET','/_nodes')['nodes']
    candidates=sorted(n['name'] for n in nodes.values() if n['name'] not in occupied and any(r=='data' or r.startswith('data_') for r in n.get('roles',[])))
    failures=[]
    for destination in candidates:
        body={'commands':[{'move':{'index':index,'shard':shard,'from_node':source,'to_node':destination}}]}
        try: client.request('POST','/_cluster/reroute?dry_run=true',body)
        except APIError as exc:
            if exc.status!=400: raise
            failures.append(str(exc)); continue
        client.request('POST','/_cluster/reroute',body)
        print(f'Move submitted: {index}/{shard} primary {source} -> {destination}')
        print(f'Watch: curl -s "{client.url}/_cat/shards/{index}?v&s=shard,prirep"')
        return
    raise RuntimeError('No legal destination. Restore allocation filters / awareness / disk settings. '+ '\n'.join(failures))


if __name__=='__main__':
    try: main()
    except (RuntimeError,ValueError,OSError) as exc: sys.exit(f'[error] {exc}')
