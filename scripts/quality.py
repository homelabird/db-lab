#!/usr/bin/env python3
"""Small, offline-first quality entrypoint. Never starts DBs or repairs failures.

static: syntax, duplicate test names/YAML keys, local documentation links.
offline: static + all six host suites (their doubles are not live DB evidence).
tools: actual Ansible fixture and Helm renderer, missing tools => blocked/127.
release: compare the supplied release manifest; not a signature/attestation.
all: static + host suites + real tool checks. Runtime remains `mvp verify`.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUITES = (('root', '.'), ('mvp', 'mvp-lab'), ('elasticsearch', 'elasticsearch'),
          ('kafka', 'kafka-lab'), ('mariadb', 'mariadb-ha-lab'), ('redis', 'redis-lab'))
IGNORED_DIRS = {'.git', '__pycache__', '.state', 'reports', 'artifacts', '.quality',
                'node_modules', 'htmlcov', '.pytest_cache'}
MANIFEST = 'RELEASE-MANIFEST.json'


def source_files(root):
    """Exclude local environments/generated evidence, never follow symlinks."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith('.venv'))
        for dirname in list(dirs):
            if (Path(directory) / dirname).is_symlink():
                yield Path(directory) / dirname
                dirs.remove(dirname)
        for filename in sorted(files):
            if filename == '.env' or filename.startswith('.coverage') or filename.endswith(('.pyc', '.pyo')):
                continue
            yield Path(directory) / filename


def no_links(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink_path_refused')


def duplicate_tests(tree):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            seen = set()
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith('test_'):
                    if member.name in seen:
                        found.append({'class': node.name, 'method': member.name, 'line': member.lineno})
                    seen.add(member.name)
    return found


def yaml_documents(text):
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            seen = set()
            # Check explicit keys before YAML merge-key expansion; overrides of
            # merged defaults are legitimate, duplicate explicit keys are not.
            for key_node, _ in node.value:
                if key_node.tag == 'tag:yaml.org,2002:merge':
                    continue
                key = self.construct_object(key_node, deep=False)
                if key in seen:
                    raise ValueError('duplicate_yaml_key')
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    return list(yaml.load_all(text, Loader=UniqueLoader))


def local_links(path, text):
    errors = []
    # This intentionally checks file targets only, not remote URLs or anchors.
    for target in re.findall(r'(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)', text):
        target = target.split(' "', 1)[0].strip('<>')
        split = urlsplit(target)
        if split.scheme or target.startswith(('#', '/', '$')) or not split.path:
            continue
        if not (path.parent / unquote(split.path)).exists():
            errors.append(target)
    return errors


def static_check(root):
    counts = Counter()
    issues, long_functions, blocked = [], [], []
    try:
        import yaml  # noqa: F401 - prerequisite, no fake YAML parser fallback
    except ImportError:
        blocked.append('PyYAML missing: install scripts/requirements-checks.txt')
    for path in source_files(root):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            issues.append({'file': name, 'error': 'source_symlink_refused'})
            continue
        suffix = path.suffix
        if suffix not in {'.py', '.sh', '.yaml', '.yml', '.md'}:
            continue
        try:
            text = path.read_text(encoding='utf-8')
            if suffix == '.py':
                tree = ast.parse(text, filename=name)
                counts['python'] += 1
                for dup in duplicate_tests(tree):
                    issues.append({'file': name, 'error': 'overwritten_test_method', **dup})
                if 'tests' not in path.parts:
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno - node.lineno + 1 >= 100:
                            long_functions.append({'file': name, 'function': node.name,
                                                   'line': node.lineno, 'lines': node.end_lineno-node.lineno+1})
            elif suffix == '.sh':
                if not shutil.which('bash'):
                    if 'bash missing' not in blocked: blocked.append('bash missing')
                    continue
                p = subprocess.run(['bash', '-n', str(path)], capture_output=True, timeout=15)
                counts['bash'] += 1
                if p.returncode: issues.append({'file': name, 'error': 'bash_syntax'})
            elif suffix in {'.yaml', '.yml'}:
                if 'templates' in path.relative_to(root).parts and ('{{' in text or '{%' in text):
                    counts['helm_templates_not_parsed'] += 1
                    continue
                if any(x.startswith('PyYAML') for x in blocked): continue
                yaml_documents(text)
                counts['yaml'] += 1
            elif suffix == '.md':
                counts['markdown'] += 1
                for target in local_links(path, text):
                    issues.append({'file': name, 'error': 'missing_local_document', 'target': target})
        except (ValueError, TypeError, SyntaxError, OSError, subprocess.TimeoutExpired) as exc:
            issues.append({'file': name, 'error': type(exc).__name__})
        except Exception as exc:
            # Includes YAML parser exceptions; never silently skip an input.
            issues.append({'file': name, 'error': type(exc).__name__})
    return {'name': 'static', 'status': 'failed' if issues else ('blocked' if blocked else 'passed'),
            'counts': dict(counts), 'issues': issues, 'blocked': blocked,
            'maintainability_warnings': long_functions,
            'scope': 'syntax/duplicate names/local links, NOT typecheck/security scan/Helm rendering'}


def release_check(root):
    """Checks every manifest-listed file, including retained historical reports."""
    path = root / MANIFEST
    no_links(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not isinstance(data.get('files'), dict):
        raise ValueError('invalid_release_manifest')
    issues = []
    for name, expected in data['files'].items():
        pure = PurePosixPath(name)
        if pure.is_absolute() or '..' in pure.parts or str(pure) != name or '\\' in name or name == MANIFEST:
            raise ValueError('invalid_manifest_path')
        item = root / name
        no_links(item)
        if not item.is_file():
            issues.append({'file': name, 'error': 'missing'})
            continue
        if hashlib.sha256(item.read_bytes()).hexdigest() != expected['sha256']:
            issues.append({'file': name, 'error': 'sha256_mismatch'})
        if item.stat().st_mode & 0o777 != expected['mode']:
            issues.append({'file': name, 'error': 'mode_mismatch'})
    for item in source_files(root):
        name = item.relative_to(root).as_posix()
        if name not in data['files'] and name != MANIFEST:
            issues.append({'file': name, 'error': 'unlisted_source_file'})
    return {'name': 'release', 'status': 'failed' if issues else 'passed',
            'files': len(data['files']), 'issues': issues,
            'scope': 'local integrity, not authenticity; runtime-generated paths excluded'}


def command_check(name, argv, cwd, output, timeout=600):
    """Run real tools once, preserve rc. No retries or mocked fallback."""
    log = output / (name + '.log')
    started = time.monotonic()
    with log.open('xb') as stream:
        os.chmod(log, 0o600)
        try:
            process = subprocess.Popen(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                       stdin=subprocess.DEVNULL, start_new_session=True)
        except FileNotFoundError:
            return {'name': name, 'status': 'blocked', 'returncode': 127, 'tests': 0}
        try:
            rc = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(timeout=5)
            rc = 124 if isinstance(exc, subprocess.TimeoutExpired) else 130
    text = log.read_text(encoding='utf-8', errors='replace')
    matches = re.findall(r'^Ran (\d+) tests? in ', text, re.MULTILINE)
    tests = sum(map(int, matches))
    skipped = sum(map(int, re.findall(r'skipped=(\d+)', text)))
    status = 'passed' if rc == 0 else ('blocked' if rc == 127 else 'failed')
    if name.startswith('host-') and rc == 0 and skipped:
        status, rc = 'blocked', 127
    if name.startswith('host-') and rc == 0 and tests == 0:
        status, rc = 'failed', 1
    return {'name': name, 'status': status, 'returncode': rc, 'tests': tests,
            'tests_skipped': skipped, 'log': log.name, 'seconds': round(time.monotonic()-started, 3),
            'scope': 'host test doubles/HTTP or actual tool fixture; NOT live DB acceptance'}


def overall(results):
    states = {r['status'] for r in results}
    if 'failed' in states: return 'failed', 1
    if 'blocked' in states: return 'blocked', 127
    if not states: return 'failed', 1
    return 'passed', 0


def write_report(output, mode, results):
    status, code = overall(results)
    data = {'schema': 1, 'command': mode, 'status': status, 'returncode': code,
            'created_at': datetime.now(timezone.utc).isoformat(), 'python': sys.version,
            'tests_executed': sum(r.get('tests', 0) for r in results),
            'live_db_acceptance_executed': False, 'checks': results}
    (output/'summary.json').write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    lines = ['# Project quality checks', '', f'**{status}** · exit {code}', '',
             'This run is NOT a real DB/HA acceptance test.', '', '| Check | Status | Tests |', '|---|---|---:|']
    lines += [f"| {r['name']} | {r['status']} | {r.get('tests', '—')} |" for r in results]
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    suite=ET.Element('testsuite', name='db-lab-quality', tests=str(len(results)),
                     failures=str(sum(r['status']=='failed' for r in results)),
                     errors=str(sum(r['status']=='blocked' for r in results)))
    for r in results:
        case=ET.SubElement(suite,'testcase',name=r['name'])
        if r['status']!='passed':
            ET.SubElement(case,'error' if r['status']=='blocked' else 'failure',message=r['status'])
    ET.ElementTree(suite).write(output/'junit.xml',encoding='utf-8',xml_declaration=True)
    for path in (output/'summary.json',output/'report.md',output/'junit.xml'):os.chmod(path,0o600)
    return code


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('static','offline','tools','release','all'))
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,help='New private directory; never overwrite old evidence')
    args=parser.parse_args(argv)
    root=args.root.absolute();no_links(root)
    output=(args.output or root/'.quality'/('quality-'+uuid.uuid4().hex[:12])).absolute()
    no_links(output);output.mkdir(mode=0o700,parents=True,exist_ok=False)
    results=[]
    try:
        if args.mode in ('static','offline','all'):results.append(static_check(root))
        if args.mode in ('offline','all'):
            for name, directory in SUITES:
                results.append(command_check('host-'+name,[sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_*.py','-v'],root/directory,output))
        if args.mode in ('tools','all'):
            for name in ('ansible','helm'):
                results.append(command_check(name,['bash',str(root/'scripts'/('test-'+name+'.sh'))],root,output))
        if args.mode=='release':results.append(release_check(root))
    except (OSError,ValueError,TypeError,KeyError) as exc:
        results.append({'name':'quality-runner','status':'failed','error':type(exc).__name__})
    code=write_report(output,args.mode,results)
    print(json.dumps({'status':overall(results)[0],'report':str(output/'summary.json'),
                      'live_db_acceptance_executed':False}))
    return code


if __name__=='__main__':
    sys.exit(main())
