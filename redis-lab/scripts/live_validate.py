"""Real Podman acceptance suite. No mocks or alternate in-process Redis backend.
Missing runtime -> BLOCKED (exit 2), never PASS. Logs are written per phase.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import manage


class Suite:
    def __init__(self, lab, no_build=False):
        self.lab = lab
        self.no_build = no_build
        self.root = manage.ROOT
        self.run_id = 'live-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.out = self.root / 'output' / self.run_id
        self.out.mkdir(parents=True)
        self.report = {'run_id': self.run_id, 'status': 'RUNNING', 'passed': False,
            'time_utc': datetime.now(timezone.utc).isoformat(), 'steps': [],
            'engine': 'actual podman containers only', 'logs': str(self.out.relative_to(self.root)),
            'note': 'One host, one observed run; PASS is not a production HA or zero-data-loss guarantee.'}
        self.sequence = 0
        self.save()

    def save(self):
        data = json.dumps(self.report, indent=2, ensure_ascii=False) + '\n'
        (self.out / 'report.json').write_text(data)
        latest = self.root / 'output' / 'live-validation.json'
        temporary = latest.with_suffix('.tmp')
        temporary.write_text(data)
        temporary.replace(latest)

    def command(self, *args, timeout=900):
        self.sequence += 1
        result = self.lab.run([sys.executable, str(self.root / 'scripts/manage.py'), *args],
                              capture=True, check=False, timeout=timeout)
        text = self.lab.redact((result.stdout or '') + (result.stderr or ''))
        log = self.out / f'{self.sequence:02d}-{args[0]}.log'
        log.write_text(text)
        if result.returncode:
            raise RuntimeError(f'{args[0]} exited {result.returncode}; see {log.relative_to(self.root)}\n{text[-2000:]}')
        return result.stdout or ''

    def step(self, name, callback):
        entry = {'name': name, 'status': 'RUNNING'}
        self.report['steps'].append(entry)
        self.save()
        print('RUN: ' + name, flush=True)
        start = time.monotonic()
        try:
            callback()
            entry['status'] = 'PASS'
            print('PASS: ' + name, flush=True)
        except BaseException as exc:
            entry.update(status='FAIL', error=self.lab.redact(str(exc) or type(exc).__name__))
            raise
        finally:
            entry['seconds'] = round(time.monotonic() - start, 2)
            self.save()

    @staticmethod
    def poll(predicate, seconds, message):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.5)
        raise RuntimeError(message)

    @staticmethod
    def workload_rows(path):
        rows = []
        if not path.exists():
            return rows
        for line in path.read_text(errors='replace').splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if 'write_ack' in row:
                rows.append(row)
        return rows

    def data(self):
        self.command('demo')
        self.command('seed', '--users', '256', '--payload-bytes', '128')

    def failover_with_client(self):
        run_id = 'probe-' + uuid.uuid4().hex
        log = self.out / 'continuous-workload.log'
        self.lab.inspect('lab-client')
        proc = None
        with log.open('w') as stream:
            try:
                proc = subprocess.Popen(['podman', 'exec', self.lab.cname('lab-client'),
                    'python', '/app/client.py', 'workload', '--seconds', '3600', '--rate', '2',
                    '--wait-replicas', '1', '--run-id', run_id], cwd=self.root,
                    env=self.lab.proc_env, text=True, stdout=stream, stderr=subprocess.STDOUT)
                self.poll(lambda: any(r['write_ack'] for r in self.workload_rows(log)), 30,
                          'No successful warm-up writes. Inspect continuous-workload.log.')
                budget = self.lab.failover_budget() + 2 * self.lab.readiness_budget() + 120
                self.command('test', timeout=budget)
                result = json.loads((self.root / 'output/failover-test.json').read_text())
                if not result.get('passed'):
                    raise RuntimeError('Failover report is not successful')
                new_ip = self.lab.env[result['after'].upper().replace('-', '_') + '_IP']
                self.poll(lambda: any(r['write_ack'] and r.get('node_ip') == new_ip
                                     for r in self.workload_rows(log)), 30,
                          'Sentinel switched but the running client did not write to the new primary.')
                self.report['client_failover'] = {'run_id': run_id, 'new_master': result['after'],
                                                 'new_master_ip': new_ip}
            finally:
                primary_error = sys.exc_info()[1]
                if proc is not None:
                    # Terminating podman exec alone does not reliably terminate its in-container process.
                    # A per-run stop file lets the client finish its JSONL journal cleanly.
                    try:
                        stop = 'from pathlib import Path; import sys; Path("/results",sys.argv[1]+".stop").touch()'
                        self.lab.execute('lab-client', ['python', '-c', stop, run_id], timeout=15)
                        code = proc.wait(timeout=20)
                        if code:
                            raise RuntimeError(f'Workload process exited {code}; inspect {log}')
                    except BaseException as cleanup_error:
                        if proc.poll() is None:
                            proc.terminate()
                            try: proc.wait(timeout=5)
                            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
                        if primary_error is not None:
                            raise RuntimeError(f'Client/failover failed: {primary_error}; stopping workload also failed: {cleanup_error}') from primary_error
                        raise
        self.command('verify', run_id)

    def replica_auth(self):
        master = self.lab.master()
        replica = next(n for n in manage.REDIS if n != master)
        try:
            self.command('fault', 'auth-replica', replica)
            self.poll(lambda: 'master_link_status:down' in self.lab.cli(replica, ['INFO', 'replication']).stdout,
                      30, 'Replica authentication fault did not break the replication link')
        finally:
            self.command('recover', replica)

    def quorum(self):
        master = self.lab.master()
        replicas = [n for n in manage.REDIS if n != master]
        try:
            self.command('fault', 'stop', 'sentinel-2')
            self.command('fault', 'stop', 'sentinel-3')
            def no_quorum():
                result = self.lab.cli('sentinel-1', ['SENTINEL', 'CKQUORUM', self.lab.env['MASTER_NAME']], check=False)
                return 'NOQUORUM' in result.stdout + result.stderr
            self.poll(no_quorum, int(self.lab.env['DOWN_AFTER_MS']) / 1000 + 20, 'Expected NOQUORUM not observed')
            # Sentinel availability and Redis write availability are different conditions.
            if self.lab.cli(master, ['SET', 'lab:acceptance:quorum-service', self.run_id]).stdout.strip() != 'OK':
                raise RuntimeError('Healthy master did not accept writes with one Sentinel')
            self.command('fault', 'kill', master)
            duration = max(12, int(self.lab.env['DOWN_AFTER_MS']) / 1000 + 10)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                for replica in replicas:
                    if not self.lab.cli(replica, ['ROLE']).stdout.startswith('slave\n'):
                        raise RuntimeError('A replica was promoted despite no Sentinel majority')
                time.sleep(1)
            info = self.lab.cli('sentinel-1', ['SENTINEL', 'MASTER', self.lab.env['MASTER_NAME']]).stdout
            if 's_down' not in info:
                raise RuntimeError('Remaining Sentinel did not mark the stopped master down')
            self.report['no_quorum_observation_seconds'] = duration
        finally:
            self.command('recover')

    def sandbox(self, command):
        try:
            self.command(command)
        finally:
            self.command('sandbox-recover')

    def backup_restore(self):
        directory = self.root / 'output' / 'backups'
        before = set(directory.glob('*.rdb'))
        self.command('backup')
        created = set(directory.glob('*.rdb')) - before
        if len(created) != 1:
            raise RuntimeError('Expected exactly one newly created backup')
        source = created.pop()
        self.command('restore', str(source), '--yes')
        self.report['backup_restored'] = str(source.relative_to(self.root))

    def restart(self):
        master = self.lab.master()
        key = 'lab:acceptance:restart:' + uuid.uuid4().hex
        self.lab.cli(master, ['SET', key, self.run_id])
        self.command('down')
        self.command('up', '--no-build')
        if self.lab.master() != master:
            raise RuntimeError('Primary identity changed after clean down/up; inspect runtime configuration persistence')
        if self.lab.cli(master, ['GET', key]).stdout.strip() != self.run_id:
            raise RuntimeError('Restart marker was not persisted')
        self.report['restart_primary'] = master

    def execute(self):
        missing = [n for n in ('podman', 'podman-compose') if not shutil.which(n)]
        if missing:
            self.report.update(status='BLOCKED', error='Missing executable(s): ' + ', '.join(missing))
            self.report['steps'].append({'name': 'runtime availability', 'status': 'BLOCKED'})
            self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            self.save()
            print('BLOCKED: ' + self.report['error'])
            return 2
        try:
            try:
                self.step('preflight', lambda: self.command('doctor'))
            except Exception as exc:
                self.report['status'] = 'BLOCKED'
                self.report['steps'][-1]['status'] = 'BLOCKED'
                raise
            self.step('startup and full readiness', lambda: self.command('up', *(['--no-build'] if self.no_build else [])))
            self.step('basic commands and seed dataset', self.data)
            self.step('primary kill, continuous client reconnection and recovery', self.failover_with_client)
            self.step('replication authentication failure and repair', self.replica_auth)
            self.step('missing Sentinel majority prevents failover', self.quorum)
            self.step('sandbox maxmemory rejection and recovery', lambda: self.sandbox('sandbox-oom'))
            self.step('sandbox RDB failure, MISCONF and recovery', lambda: self.sandbox('sandbox-persistence'))
            self.step('RDB backup, structural validation and isolated restore', self.backup_restore)
            self.step('clean restart preserves data and roles', self.restart)
            self.step('export results and diagnostics', lambda: (self.command('results'), self.command('collect')))
            self.report.update(status='PASS', passed=True)
            return 0
        except BaseException as exc:
            if self.report['status'] != 'BLOCKED':
                self.report['status'] = 'FAIL'
            self.report['error'] = self.lab.redact(str(exc) or type(exc).__name__)
            if self.report['status'] != 'BLOCKED':
                try: self.command('recover')
                except BaseException as recovery: self.report['recovery_error'] = self.lab.redact(str(recovery))
                try: self.command('collect')
                except BaseException as collection: self.report['collection_error'] = self.lab.redact(str(collection))
            print(self.report['status'] + ': ' + self.report['error'], file=sys.stderr)
            return 2 if self.report['status'] == 'BLOCKED' else 1
        finally:
            self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            self.save()
            print('Report: output/live-validation.json', flush=True)


def validate(lab, confirmed=False, no_build=False):
    if not confirmed:
        raise ValueError('validate interrupts THIS lab, kills its master, and overwrites redis-sandbox. Use --yes after exporting needed sandbox data.')
    return Suite(lab, no_build).execute()
