#!/usr/bin/env python3
"""Read a few stable pages with PIT + search_after, and always close the PIT."""
import argparse
import json
import sys
from lablib import ESClient, INDICES


def paginate(client, index, page_size=10, pages=3):
    pit=None
    seen=set()
    try:
        pit=client.request('POST',f'/{index}/_pit?keep_alive=1m')['id']
        after=None
        for number in range(1,pages+1):
            body={'size':page_size,'track_total_hits':False,
                  '_source':{'excludes':['payload']},'pit':{'id':pit,'keep_alive':'1m'},
                  'sort':[{'@timestamp':'asc'},{'_shard_doc':'asc'}], 'query':{'match_all':{}}}
            if after is not None: body['search_after']=after
            result=client.request('POST','/_search?allow_partial_search_results=false',body)
            pit=result.get('pit_id',pit)
            if result.get('timed_out') or result.get('_shards',{}).get('failed',0):
                raise RuntimeError('Pagination returned a timeout or partial shard results')
            hits=result.get('hits',{}).get('hits',[])
            if not hits: break
            for hit in hits:
                identity=(hit['_index'],hit['_id'])
                if identity in seen: raise RuntimeError(f'Duplicate result across pages: {identity}')
                seen.add(identity)
            print(json.dumps({'page':number,'hits':hits},ensure_ascii=False,indent=2))
            after=hits[-1]['sort']
            if len(hits)<page_size: break
        return len(seen)
    finally:
        if pit:
            original=sys.exc_info()[0] is not None
            try: client.request('DELETE','/_pit',{'id':pit})
            except Exception as exc:
                if not original: raise
                print(f'[warning] PIT close failed; it expires automatically: {exc}',file=sys.stderr)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--index',choices=INDICES,default=INDICES[0])
    p.add_argument('--page-size',type=int,default=10)
    p.add_argument('--pages',type=int,default=3)
    args=p.parse_args()
    if not 1<=args.page_size<=1000 or not 1<=args.pages<=10000: p.error('page-size 1..1000; pages 1..10000')
    print(f'[done] {paginate(ESClient(),args.index,args.page_size,args.pages)} distinct hits')


if __name__=='__main__':
    try: main()
    except (RuntimeError,ValueError,OSError) as exc: sys.exit(f'[error] {exc}')
