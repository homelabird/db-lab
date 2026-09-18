#!/usr/bin/env python3
"""Internal-only, read-only Galera readiness and metrics endpoint."""
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METRICS = ('Threads_connected', 'Threads_running', 'Questions', 'Slow_queries', 'Uptime',
           'Innodb_buffer_pool_read_requests', 'Innodb_buffer_pool_reads',
           'Innodb_row_lock_waits', 'Innodb_row_lock_time', 'Bytes_received', 'Bytes_sent')

def is_ready(status, mode='galera'):
    if not status.get('sql_alive'):
        return False
    if mode == 'standalone':
        return True
    if mode != 'galera':
        return False
    return all((status.get('wsrep_cluster_status') == 'Primary',
                status.get('wsrep_local_state') == '4',
                status.get('wsrep_ready') == 'ON',
                status.get('wsrep_connected') == 'ON'))

def collect():
    result = {'node': os.environ.get('NODE_NAME', 'unknown'), 'sampled_at': time.time(),
              'mode': os.environ.get('LAB_MODE', 'galera'), 'sql_alive': False, 'ready': False}
    sql = "SHOW GLOBAL STATUS LIKE 'wsrep_%'; SHOW GLOBAL STATUS WHERE Variable_name IN (" + ",".join(
        "'" + x + "'" for x in METRICS) + ");"
    try:
        env = dict(os.environ, MYSQL_PWD=os.environ.get('MARIADB_ROOT_PASSWORD', ''))
        p = subprocess.run(['mariadb', '--protocol=socket', '-uroot', '--batch', '--skip-column-names',
                            '--connect-timeout=2', '-e', sql], env=env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        if p.returncode:
            result['error'] = 'SQL probe unavailable'
        else:
            result.update(dict(line.split('\t', 1) for line in p.stdout.splitlines() if '\t' in line))
            result['sql_alive'] = True
            result['ready'] = is_ready(result, result['mode'])
    except (OSError, subprocess.TimeoutExpired):
        result['error'] = 'SQL probe timeout/unavailable'
    return result

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/ready', '/status', '/metrics'):
            self.send_error(404); return
        data = collect()
        if self.path == '/metrics':
            lines = []
            for key, value in data.items():
                if key.startswith('wsrep_') or key in METRICS:
                    try: value = float(value)
                    except (TypeError, ValueError): continue
                    lines.append('mariadb_lab_' + key.lower() + ' ' + str(value))
            lines.append('mariadb_lab_ready ' + str(int(data['ready'])))
            payload = ('\n'.join(lines) + '\n').encode()
            mime = 'text/plain; version=0.0.4'
        else:
            payload = json.dumps(data, ensure_ascii=False).encode()
            mime = 'application/json; charset=utf-8'
        self.send_response(503 if self.path == '/ready' and not data['ready'] else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try: self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError): pass
    def log_message(self, *_): pass

if __name__ == '__main__':
    if '--check' in sys.argv:
        sys.exit(0 if collect()['ready'] else 1)
    if '--json' in sys.argv:
        print(json.dumps(collect())); sys.exit(0)
    ThreadingHTTPServer(('0.0.0.0', 9200), Handler).serve_forever()
