#!/usr/bin/env python3
"""List or run read-only examples; JSON files also work with curl/Cerebro REST."""
import argparse
import json
import sys
from lablib import ESClient, ROOT, execute_example, load_catalog, write_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('name', nargs='?', default='list', help='list | all | 01-latest | 01')
    p.add_argument('--show-only', action='store_true', help='Print request without contacting Elasticsearch')
    p.add_argument('--save-dir', help='Optionally save responses to this directory')
    args=p.parse_args()
    catalog=load_catalog()
    if args.name == 'list':
        for entry in catalog:
            print(f'{entry["name"]:23s} {entry["title"]}')
        return 0
    selected=catalog if args.name == 'all' else [e for e in catalog if e['name']==args.name or e['name'].split('-')[0]==args.name]
    if not selected:
        p.error('Unknown example. Run ./lab.sh query list')
    client=ESClient()
    for entry in selected:
        print(f'\n=== {entry["name"]}: {entry["title"]} ===\n{entry["method"]} {entry["path"]}')
        if args.show_only:
            if entry.get('file'):
                print((ROOT / 'queries' / entry['file']).read_text(encoding='utf-8'))
            continue
        result=execute_example(client,entry)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.save_dir:
            from pathlib import Path
            write_json(Path(args.save_dir)/(entry['name']+'.json'),result)
    return 0


if __name__=='__main__':
    try: sys.exit(main())
    except (RuntimeError, ValueError, OSError) as exc: sys.exit(f'[error] {exc}')
