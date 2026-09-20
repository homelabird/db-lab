"""Typed, finite CLI plan for the existing DB Lab controllers; no shell fragments.

The policy is deliberately independent of Ansible and the DB drivers so it can be
unit tested without either. Consent is an accident guard, not a security boundary
against an operator who can edit playbooks or execute Docker directly.
"""
from __future__ import annotations
import math
import os
from pathlib import Path
import re

TARGETS = ('mvp', 'all', 'elasticsearch', 'kafka', 'mariadb', 'redis', 'k8s')
SIMULATIONS = ('baseline', 'kafka-outage', 'es-outage', 'redis-outage', 'db-freeze',
               'worker-freeze', 'redis-recreate', 'redis-switch', 'row-lock',
               'duplicate-retry', 'version-race', 'db-network-delay', 'db-network-loss')
TRANSACTIONS = ('stock-race', 'duplicate-checkout', 'checkout-rollback',
                'commit-ambiguity', 'idempotency-conflict', 'refund-race')
DRILLS = ('backup-restore', 'upgrade-restore', 'redis-disk-full', 'redis-oom', 'deadlock') + TRANSACTIONS
MESSAGES = ('poison-schema', 'mapping-reject', 'projection-commit-gap', 'dlq-commit-gap',
            'replay-ordering', 'version-collision')
PAIRED = tuple(s for s in SIMULATIONS if s not in ('baseline', 'row-lock', 'duplicate-retry', 'version-race'))
SUITES = ('core', 'simulations', 'transactions', 'messages', 'network', 'resources', 'recovery', 'all', 'candidate')
SERVICES = ('mariadb', 'kafka', 'elasticsearch', 'redis', 'api', 'worker', 'redis-spare')
WORKLOADS = ('mixed', 'read-heavy', 'write-heavy', 'hot-key')
IMAGE_RE = r'(?:(?:docker\.io/)?library/)?mariadb:(?:[0-9]+\.[0-9]+(?:\.[0-9]+)?)(?:-[a-z0-9.-]+)?(?:@sha256:[a-f0-9]{64})?'
RUN_IDS = {'accept': r'accept-[a-f0-9]{12}', 'msg': r'msg-[a-f0-9]{24}', 'sim': r'sim-[a-f0-9]{12}'}
REQUEST_KEYS = {'action', 'verb', 'name', 'options'}
NUMERIC = {'seed': (0, 2**32 - 1), 'seconds': (15, 180), 'workers': (1, 8),
           'fault_at': (1, 179), 'fault_for': (1, 30), 'recovery_timeout': (5, 300),
           'clients': (2, 8), 'step_timeout': (120, 1200)}
SIM_KEYS = {'seed', 'seconds', 'rate', 'workers', 'fault_at', 'fault_for', 'recovery_timeout', 'workload'}
DIRS = {'elasticsearch': 'elasticsearch', 'kafka': 'kafka-lab',
        'mariadb': 'mariadb-ha-lab', 'redis': 'redis-lab', 'mvp': 'mvp-lab'}


class PolicyError(ValueError):
    """Safe error text; caller-provided strings are never echoed."""


def require(condition, code):
    if not condition:
        raise PolicyError(code)


def text(value, limit=512):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def absolute(value):
    require(text(value, 2048) and value.startswith('/') and not value.startswith('//'), 'absolute_posix_path_required')
    require('..' not in Path(value).parts and value != '/', 'unsafe_path')
    return value


def regular_path(path, *, directory=False):
    """Reject symlink components rather than silently changing project identity."""
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_path_refused')
    require(path.is_dir() if directory else path.is_file(), 'required_path_missing')
    return path


def validate_project(value, target):
    root = regular_path(absolute(value), directory=True)
    require(root.resolve() == root, 'canonical_project_path_required')
    required = ['all.sh', 'scripts/control.py']
    if target in DIRS:
        required.append(DIRS[target] + '/lab.sh')
    elif target == 'all':
        required.extend(DIRS[t] + '/lab.sh' for t in DIRS if t != 'mvp')
    elif target == 'k8s':
        required.extend(('helmchart/Chart.yaml', 'scripts/k8s_guard.py'))
    for name in required:
        path = regular_path(root / name)
        require(os.access(path, os.R_OK), 'project_file_unreadable')
    return root


def run_id(value, kind):
    require(isinstance(value, str) and re.fullmatch(RUN_IDS[kind], value), 'invalid_run_id')
    return value


def encode_options(options, allowed):
    require(isinstance(options, dict) and set(options) <= allowed, 'unsupported_options')
    args = []
    for key in sorted(options):
        val = options[key]
        if key in NUMERIC:
            lo, hi = NUMERIC[key]
            require(type(val) is int and lo <= val <= hi, 'option_out_of_range_' + key)
        elif key == 'rate':
            require(type(val) in (int, float) and math.isfinite(val) and .2 <= val <= 10, 'option_out_of_range_rate')
        elif key == 'workload':
            require(val in WORKLOADS, 'invalid_workload')
        elif key == 'keep':
            require(type(val) is bool, 'keep_requires_boolean')
            if val:
                args.append('--keep')
            continue
        elif key == 'candidate_image':
            require(isinstance(val, str) and re.fullmatch(IMAGE_RE, val), 'explicit_mariadb_image_required')
        elif key == 'snapshot':
            absolute(val)
        else:
            raise PolicyError('unsupported_options')
        args.extend(['--' + key.replace('_', '-'), str(val)])
    return args


def bounded_plan(name, options):
    seconds = options.get('seconds', 40)
    workers = options.get('workers', 4)
    rate = options.get('rate', 2)
    require(seconds * rate <= 400, 'workflow_budget_exceeded')
    fault_at, fault_for = options.get('fault_at', 8), options.get('fault_for', 10)
    require(fault_at < seconds, 'fault_at_outside_run')
    if name not in ('baseline', 'duplicate-retry', 'version-race'):
        require(fault_at + fault_for + 5 <= seconds, 'post_fault_observation_required')
    require(name != 'version-race' or workers >= 2, 'version_race_requires_two_workers')


def kube_plan(action, config):
    require(isinstance(config, dict), 'k8s_configuration_required')
    allowed = {'context', 'allowed_contexts', 'namespace', 'release', 'kubeconfig', 'values_files'}
    require(not set(config) - allowed, 'unsupported_k8s_option')
    ctx, allowed_ctx = config.get('context'), config.get('allowed_contexts')
    require(text(ctx, 200) and not ctx.startswith('-'), 'explicit_k8s_context_required')
    require(isinstance(allowed_ctx, list) and 0 < len(allowed_ctx) <= 20 and
            all(text(c, 200) and ',' not in c for c in allowed_ctx) and ctx in allowed_ctx, 'k8s_context_not_allowed')
    namespace, release = config.get('namespace'), config.get('release', 'db-lab')
    for value, limit in ((namespace, 63), (release, 53)):
        require(isinstance(value, str) and len(value) <= limit and re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', value), 'invalid_k8s_name')
    require(namespace not in ('default', 'kube-system', 'kube-public', 'kube-node-lease'), 'protected_namespace')
    env = {'DB_LAB_CONTEXT': ctx, 'DB_LAB_ALLOWED_CONTEXTS': ','.join(allowed_ctx),
           'DB_LAB_NAMESPACE': namespace, 'DB_LAB_RELEASE': release}
    if config.get('kubeconfig'):
        env['DB_LAB_KUBECONFIG'] = absolute(config['kubeconfig'])
    values = config.get('values_files', [])
    require(isinstance(values, list) and len(values) <= 8, 'invalid_values_files')
    require(not values or action in ('up', 'preflight', 'lint', 'template'), 'values_not_used_by_action')
    args = []
    for path in values:
        args += ['-f', absolute(path)]
    return env, args, namespace + '/' + release


def build_plan(params, check_mode=False):
    """Return only argv lists; runtime execution cannot add arbitrary CLI flags."""
    root = absolute(params.get('project_root'))
    target = params.get('target', 'mvp')
    require(target in TARGETS, 'unknown_target')
    req = params.get('request', {'action': 'status'})
    require(isinstance(req, dict) and set(req) <= REQUEST_KEYS, 'invalid_request')
    action = req.get('action', 'status')
    verb, name, opts = req.get('verb'), req.get('name'), req.get('options', {})
    require(text(action, 40) and (verb is None or text(verb, 40)) and (name is None or text(name)), 'invalid_command_tokens')
    require(isinstance(opts, dict), 'options_must_be_mapping')
    for flag in ('allow_changes', 'allow_faults', 'allow_destroy'):
        require(type(params.get(flag, False)) is bool, 'consent_must_be_boolean')
    confirmation = params.get('confirmation', '')
    require(isinstance(confirmation, str), 'confirmation_must_be_string')
    allowed_options, env, category = set(), {}, 'observe'
    destructive_phrase = None
    commands = []
    prefix = ['bash', str(Path(root) / 'all.sh'), '--fail-fast']

    if target not in ('mvp', 'k8s'):
        require(verb is None and name is None, 'unused_verb_or_name')
        require(action in ('help', 'list', 'init', 'doctor', 'preflight', 'health', 'status', 'up', 'down', 'restart', 'test', 'reset'), 'unsupported_lab_action')
        args = [action] if action in ('help', 'list') else [action, target]
        if action in ('health', 'preflight'):
            args.append('--json')
        if action in ('init', 'up', 'down', 'restart'):
            category = 'change'
        if action == 'reset':
            category, destructive_phrase = 'destroy', 'DELETE:' + target
            args.append('--yes')
        commands.append(prefix + args)
    elif target == 'k8s':
        require(verb is None and name is None, 'unused_verb_or_name')
        require(action in ('init', 'preflight', 'lint', 'template', 'up', 'status', 'history', 'values', 'down'), 'unsupported_k8s_action')
        env, values, kname = kube_plan(action, params.get('k8s', {}))
        args = ['k8s', action] + values
        if action in ('init', 'up'):
            category = 'change'
        if action == 'down':
            category, destructive_phrase = 'destroy', 'UNINSTALL:' + kname
            args.append('--yes')
        commands.append(prefix + args)
    else:
        args = ['mvp', action]
        if action in ('init', 'up', 'down', 'status', 'doctor', 'diagnose', 'smoke', 'test', 'inspect-runtime', 'help', 'bind-target', 'rebuild-search', 'restart'):
            require(verb is None and name is None, 'unused_verb_or_name')
            if action in ('init', 'up', 'down', 'smoke', 'restart', 'rebuild-search', 'bind-target'):
                category = 'change'
            if action == 'bind-target':
                destructive_phrase = 'BIND:mvp'
            if action in ('bind-target', 'rebuild-search'):
                args.append('--yes')
            if action == 'restart':
                commands = [prefix + ['mvp', 'down'], prefix + ['mvp', 'up']]
        elif action in ('stop', 'resume', 'recreate', 'logs', 'experiment', 'sql'):
            require(verb is None, 'unused_verb')
            choices = ('normal', 'redis-spare', 'bad-db-password', 'fresh-search') if action == 'experiment' else (
                ('orders', 'outbox', 'counts', 'ping') if action == 'sql' else SERVICES)
            require(name in choices or (action == 'logs' and name is None), 'invalid_service_mode_or_query')
            if name is not None:
                args.append(name)
            if action in ('stop', 'recreate', 'experiment'):
                category = 'change' if action == 'experiment' and name == 'normal' else 'fault'
                args.append('--yes')
            elif action == 'resume':
                category = 'change'
        elif action in ('simulate', 'drills', 'messages', 'verify', 'study'):
            choices = {'simulate': SIMULATIONS, 'drills': DRILLS, 'messages': MESSAGES, 'verify': SUITES, 'study': PAIRED}[action]
            verbs = {'list', 'plan', 'run'}
            if action in ('simulate', 'drills', 'messages'):
                verbs.add('recover')
            if action == 'drills':
                verbs.add('prepare')
            if action == 'messages':
                verbs |= {'inspect', 'cleanup'}
            if action == 'verify':
                verbs |= {'report', 'export'}
            if action == 'study':
                verbs.add('compare')
            require(verb in verbs, 'unsupported_subcommand')
            args.append(verb)
            if verb in ('list', 'recover', 'prepare'):
                require(name is None, 'unused_name')
                if verb != 'list':
                    category = 'change'
                    args.append('--yes')
            elif verb in ('report', 'export', 'inspect', 'cleanup'):
                args.append(run_id(name, 'accept' if action == 'verify' else 'msg'))
                if verb == 'cleanup':
                    category = 'change'; args.append('--yes')
            elif verb == 'compare':
                require(name is None and set(opts) == {'baseline_id', 'fault_id'}, 'compare_requires_two_run_ids')
                args += [run_id(opts['baseline_id'], 'sim'), run_id(opts['fault_id'], 'sim')]
                require(opts['baseline_id'] != opts['fault_id'], 'compare_requires_distinct_runs')
                opts = {}
            else:
                if action == 'verify' and name is None:
                    name = 'core'
                require(name in choices, 'unknown_scenario_or_suite')
                args.append(name)
                if action in ('simulate', 'study'):
                    allowed_options = set(SIM_KEYS)
                    if action == 'study' and verb == 'run':
                        allowed_options.add('step_timeout')
                elif action == 'drills':
                    if name in TRANSACTIONS:
                        allowed_options = {'clients', 'seed'}
                    elif name == 'backup-restore' and verb == 'run':
                        allowed_options = {'snapshot'}
                    elif name == 'upgrade-restore' and verb == 'run':
                        allowed_options = {'snapshot', 'candidate_image'}
                        require('candidate_image' in opts, 'candidate_image_required')
                elif action == 'messages' and verb == 'run':
                    allowed_options = {'keep'}
                elif action == 'verify':
                    allowed_options = {'step_timeout'} if verb == 'run' else set()
                    if name == 'candidate':
                        allowed_options.add('candidate_image')
                        require('candidate_image' in opts, 'candidate_image_required')
                if verb == 'run':
                    category = 'change' if ((action == 'simulate' and name in ('baseline', 'duplicate-retry', 'version-race')) or (action == 'verify' and name == 'core')) else 'fault'
                    args.append('--yes')
        else:
            raise PolicyError('unsupported_mvp_action')
        args += encode_options(opts, allowed_options)
        if action in ('simulate', 'study') and verb in ('plan', 'run'):
            bounded_plan(name, opts)
        if not commands:
            commands.append(prefix + args)
    if target != 'mvp':
        require(not opts, 'unused_options')
    if target != 'k8s':
        require(not params.get('k8s'), 'k8s_options_for_non_k8s_target')
    consent = []
    if category in ('change', 'fault', 'destroy'):
        consent.append('allow_changes')
    if category == 'fault':
        consent.append('allow_faults')
    if category == 'destroy':
        consent.append('allow_destroy')
    if not check_mode:
        for flag in consent:
            require(params.get(flag) is True, 'explicit_' + flag + '_required')
        if destructive_phrase:
            require(confirmation == destructive_phrase, 'explicit_confirmation_required')
    return {'target': target, 'action': action, 'verb': verb, 'name': name,
            'commands': commands, 'environment': env, 'category': category,
            'required_consent': consent, 'required_confirmation': destructive_phrase,
            'project_root': root, 'check_mode': bool(check_mode)}
