#!/usr/bin/env python3
"""Opt-in fresh-runner build + live acceptance. Never use an existing lab directory.

The operator opts into image downloads/builds and synthetic writes by passing
--allow-disposable-runner --yes. No reset/prune/volume deletion is performed.
Only allowlisted projections are copied to the public artifact directory.
"""
from __future__ import annotations
import argparse
import json
import os
import signal
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'mvp-lab'))
from tools import manage, acceptance
from tools.evidence_export import project_acceptance, write_projection
from tools.startup import collect

SUITES = ('core', 'transactions', 'messages', 'recovery')
FAILURE_CODES = {'fresh_workspace_required', 'local_docker_required', 'self_hosted_runner_refused',
                 'insufficient_disk_budget', 'linux_and_memory_budget_required', 'project_resources_already_exist',
                 'phase_timeout', 'phase_failed', 'acceptance_not_passed'}
PHASES = ('fresh-runner', 'init', 'engine-target', 'doctor', 'build-and-start', 'acceptance', 'shutdown')


def fresh_workspace(root: Path, env: dict) -> None:
    for name in ('.env', '.state', 'reports'):
        if (root/'mvp-lab'/name).exists() or (root/'mvp-lab'/name).is_symlink():
            raise RuntimeError('fresh_workspace_required')
    for key in ('DOCKER_HOST', 'CONTAINER_HOST'):
        if env.get(key) and not env[key].startswith('unix://'):
            raise RuntimeError('local_docker_required')
    if env.get('RUNNER_ENVIRONMENT') == 'self-hosted':
        raise RuntimeError('self_hosted_runner_refused')
    if not shutil.which('docker'):
        raise FileNotFoundError('docker')
    if (root/'mvp-lab').is_symlink():
        raise RuntimeError('fresh_workspace_required')


def run(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--suite', choices=SUITES, default='core')
    p.add_argument('--allow-disposable-runner', action='store_true', required=True)
    p.add_argument('--yes', action='store_true', required=True)
    args = p.parse_args(argv)
    public = ROOT/'artifacts/mvp-runtime'
    if public.exists() or public.is_symlink():
        print('Refusing an existing artifact directory; use a new checkout.', file=sys.stderr)
        return 1
    rows = [{'name': name, 'status': 'not_run', 'returncode': None} for name in PHASES]
    summary = {'schema': 1, 'suite': args.suite, 'status': 'blocked', 'steps': rows,
               'evidence_kind': 'FRESH-RUNNER-PIPELINE-ATTEMPT', 'live_acceptance_passed': False,
               'raw_logs_included': False, 'credentials_included': False}
    index = 0; compose = None; started = False; readiness = None; projected = None
    journal = ROOT/'artifacts/private'
    # Raw phase output is private and never part of upload-artifact paths.
    journal.mkdir(parents=True, exist_ok=False, mode=0o700)
    previous_signal = signal.getsignal(signal.SIGTERM)
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    def save():
        manage.atomic_json(journal/'pipeline.json', summary)
    def phase(name, command, timeout):
        nonlocal index
        index = PHASES.index(name); row = rows[index]
        row['status'] = 'running'; save()
        result = acceptance.execute(command, cwd=manage.ROOT, env=dict(os.environ), timeout=timeout)
        # Retain private stdout for local debugging. stderr is byte-count only in execute().
        path = journal/(name+'.stdout.txt'); path.write_text(result['stdout']); path.chmod(0o600)
        row['returncode'] = result['returncode']
        if result['interrupted']:
            row['status'] = 'aborted'; raise KeyboardInterrupt()
        if result['timed_out']:
            row['status'] = 'failed'; raise RuntimeError('phase_timeout')
        if result['returncode'] != 0:
            row['status'] = 'blocked' if result['returncode'] == 127 else 'failed'
            raise RuntimeError('phase_failed')
        row['status'] = 'passed'; save()
        return result
    command = [sys.executable, str(manage.ROOT/'tools/manage.py')]
    try:
        rows[0]['status'] = 'running'; save()
        fresh_workspace(ROOT, dict(os.environ))
        if shutil.disk_usage(ROOT).free < 8 * 1024**3:
            raise RuntimeError('insufficient_disk_budget')
        rows[0].update(status='passed', returncode=0)
        phase('init', command+['init'], 30)
        config = manage.parse_env(manage.ROOT/'.env')
        config['MVP_ENGINE'] = 'docker'
        config['MVP_PROJECT'] = 'db-lab-mvp-ci-' + uuid.uuid4().hex[:10]
        path = manage.ROOT/'.env'
        path.write_text('\n'.join(k+'='+v for k, v in manage.validate(config).items())+'\n'); path.chmod(0o600)
        index = 2; rows[index]['status'] = 'running'; save()
        compose = manage.Compose(config)
        identity = compose.engine_identity()
        if not identity.get('local_docker'):
            raise RuntimeError('local_docker_required')
        info = subprocess.run(['docker','info','--format','{{json .}}'], capture_output=True,
                              check=True, text=True, timeout=15)
        info = json.loads(info.stdout)
        if info.get('OSType') != 'linux' or type(info.get('MemTotal')) is not int or info['MemTotal'] < 6*1024**3:
            raise RuntimeError('linux_and_memory_budget_required')
        # Random naming is not enough: prove no existing resources match that project.
        for kind, command_name in (('container','ps'), ('volume','volume'), ('network','network')):
            cmd = (['docker','ps','-aq'] if kind == 'container' else ['docker',command_name,'ls','-q'])
            result = subprocess.run(cmd+['--filter','label=com.docker.compose.project='+config['MVP_PROJECT']],
                                    capture_output=True, text=True, timeout=15, check=True)
            if result.stdout.strip(): raise RuntimeError('project_resources_already_exist')
        rows[index].update(status='passed', returncode=0)
        phase('doctor', command+['doctor'], 60)
        started = True  # up may partially create resources before failing.
        phase('build-and-start', command+['up'], 900)
        phase('acceptance', command+['verify','run',args.suite,'--yes'], 2700)
        raw = json.loads((journal/'acceptance.stdout.txt').read_text())
        projected = project_acceptance(manage.ROOT, raw['run_id'])
        if raw.get('status') != 'passed' or projected['status'] != 'passed': raise RuntimeError('acceptance_not_passed')
        summary['live_acceptance_passed'] = True
        summary['status'] = 'passed'
    except FileNotFoundError:
        rows[index].update(status='blocked', returncode=127)
        summary['status'] = 'blocked'
        summary['failure_code'] = 'required_runtime_missing'
    except KeyboardInterrupt:
        rows[index]['status'] = 'aborted'; summary['status'] = 'aborted'
        summary['failure_code'] = 'interrupted'
    except Exception as exc:
        if rows[index]['status'] in {'running','passed','not_run'}: rows[index]['status'] = 'failed'
        summary['status'] = rows[index]['status']
        summary['failure_code'] = str(exc) if type(exc) is RuntimeError and str(exc) in FAILURE_CODES else 'unclassified_failure'
    finally:
        # Public artifact remains available even when build/preflight never reached verify.
        if projected is None and (journal/'acceptance.stdout.txt').is_file():
            try:
                raw = json.loads((journal/'acceptance.stdout.txt').read_text())
                projected = project_acceptance(manage.ROOT, raw['run_id'])
            except Exception:
                pass
        if compose is not None and started:
            try: readiness = collect(compose, include_logs=True, budget=30)
            except Exception: readiness = {'ready': False, 'diagnostic_status': 'unavailable', 'containers': []}
            prior_status = summary['status']
            try:
                # Existing guards refuse shutdown with pending faults; never delete the marker.
                phase('shutdown', command+['down'], 120)
            except BaseException:
                rows[-1]['status'] = 'failed'
                if prior_status == 'passed': summary['status'] = 'failed'
            else:
                summary['status'] = prior_status
        signal.signal(signal.SIGTERM, previous_signal)
        save()
        write_projection(summary, public, manage.atomic_json)
        if projected is not None:
            write_projection(projected, public/'acceptance', manage.atomic_json)
        if readiness is not None:
            manage.atomic_json(public/'containers.json', readiness)
        print(json.dumps({'status': summary['status'], 'live_acceptance_passed': summary['live_acceptance_passed'],
                          'public_artifacts': 'artifacts/mvp-runtime', 'private_logs_not_uploaded': True}))
    if summary['status'] == 'blocked': return 127
    if summary['status'] == 'aborted': return 130
    return 0 if summary['status'] == 'passed' else 1

if __name__ == '__main__':
    raise SystemExit(run())
