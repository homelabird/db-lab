#!/usr/bin/env python3
"""Read-only dashboard. Polls internal node health APIs, never the container socket."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time
import urllib.request

import os
NODES = tuple(filter(None, os.environ.get('GALERA_NODES', 'galera1,galera2,galera3').split(',')))

def fetch(node):
    try:
        with urllib.request.urlopen('http://' + node + ':9200/status', timeout=4) as response:
            return json.load(response)
    except Exception as exc:
        return {'node':node,'ready':False,'error':'Node endpoint unreachable','sampled_at':time.time()}

def cluster():
    with ThreadPoolExecutor(max_workers=3) as pool: nodes = list(pool.map(fetch,NODES))
    healthy = [n for n in nodes if n.get('ready')]
    uuids = {n.get('wsrep_cluster_state_uuid') for n in healthy}
    return {'sampled_at':time.time(),'ready_nodes':len(healthy),
            'single_uuid':len(uuids)==1 if healthy else False,'nodes':nodes}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/','/index.html'):
            payload = Path('/opt/lab/dashboard.html').read_bytes(); mime='text/html; charset=utf-8'
        elif self.path == '/api/cluster':
            payload=json.dumps(cluster(),ensure_ascii=False).encode(); mime='application/json; charset=utf-8'
        else: self.send_error(404); return
        self.send_response(200); self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(payload))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.end_headers()
        try: self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError): pass
    def log_message(self,*_): pass

if __name__ == '__main__': ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
