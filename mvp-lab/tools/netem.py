"""Optional netem helper lifecycle. All mutations go through the already pinned DockerLab."""
import json
import re
import time


def image(lab):
    tag = lab.c.config['MVP_PROJECT'] + '-drills:local'
    try:
        rows = json.loads(lab.docker('image', 'inspect', tag))
        iid = rows[0]['Id']
    except Exception as exc:
        raise RuntimeError('Build the optional helper first: mvp drills prepare --yes') from exc
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', iid):
        raise RuntimeError('No immutable helper image ID')
    return iid


def check_target(lab, original):
    row = json.loads(lab.docker('inspect', original['id']))[0]
    if row['HostConfig'].get('NetworkMode') in {'host', 'none'} or str(row['HostConfig'].get('NetworkMode', '')).startswith('container:'):
        raise RuntimeError('Netem requires a private bridge network namespace')
    networks = row.get('NetworkSettings', {}).get('Networks', {})
    if len(networks) != 1:
        raise RuntimeError('Netem only supports this MVP single-network topology')


def helper(lab, record):
    name = record['helper_name']
    expected = lab.c.config['MVP_PROJECT'] + '-netem-' + record['run_id']
    if name != expected:
        raise RuntimeError('Invalid helper identity')
    ids = lab.docker('ps', '-a', '-q', '--filter', 'name=^/' + name + '$').split()
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError('Ambiguous helper identity')
    row = json.loads(lab.docker('inspect', ids[0]))[0]
    labels = row['Config'].get('Labels', {})
    if (labels.get('io.db-lab.run') != record['run_id']
            or labels.get('io.db-lab.project') != record['project']
            or labels.get('io.db-lab.kind') != 'netem'
            or row['HostConfig'].get('NetworkMode') != 'container:' + record['original']['id']
            or row['Image'] != record['helper_image'] or row.get('Mounts')
            or (record.get('helper_id') and row['Id'] != record['helper_id'])):
        raise RuntimeError('Helper ownership/storage/namespace changed; recovery refused')
    return row


def options(lab, record):
    return ['--pull', 'never', '--restart', 'no', '--network', 'container:' + record['original']['id'],
            '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN', '--security-opt', 'no-new-privileges:true',
            '--read-only', '--memory', '64m', '--memory-swap', '64m', '--pids-limit', '24', '--cpus', '0.25',
            '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1',
            '--label', 'io.db-lab.project=' + record['project'], '--label', 'io.db-lab.run=' + record['run_id'],
            '--label', 'io.db-lab.kind=netem']


def start(lab, record, seconds):
    check_target(lab, record['original'])
    record['helper_name'] = record['project'] + '-netem-' + record['run_id']
    record['helper_image'] = image(lab)
    lab._save(record)
    cid = lab.docker('create', '--name', record['helper_name'], *options(lab, record),
                     record['helper_image'], 'delay' if record['action'] == 'netem-delay' else 'loss',
                     '--seconds', str(seconds)).strip()
    record['helper_id'] = cid
    lab._save(record)
    helper(lab, record)
    lab.docker('start', cid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = logs(lab, record)
        ready = next((e for e in events if e.get('stage') == 'netem_applied'), None)
        if ready:
            return ready
        failure = next((e for e in events if e.get('stage') == 'netem_failed'), None)
        if failure:
            allowed = {'capability_denied', 'qdisc_conflict', 'interface_missing', 'netem_kernel_unavailable', 'tc_command_failed'}
            reason = failure.get('reason') if failure.get('reason') in allowed else 'helper_failure'
            raise RuntimeError('Netem helper: ' + reason + '; no automatic host changes')
        time.sleep(0.15)
    raise RuntimeError('Netem application was not observed; inspect recover marker')


def logs(lab, record):
    row = helper(lab, record)
    if not row:
        return []
    events = []
    for line in lab.docker('logs', '--tail', '20', row['Id']).splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except ValueError:
            pass
    return events


def restore(lab, record):
    # If setup failed BEFORE a helper identity was written, no netem command could run.
    if 'helper_name' not in record:
        return {'statistics': [], 'helper_created': False}
    lab.inspect(record['original']['id'], record['service'])
    check_target(lab, record['original'])
    row = helper(lab, record)
    if row and row['State']['Running']:
        lab.docker('stop', '--time', '8', row['Id'])
    events = logs(lab, record)
    # Explicit cleanup verifies the reserved handle, including a helper SIGKILL.
    raw = lab.docker('run', '--rm', *options(lab, record), record['helper_image'], 'cleanup', timeout=20)
    cleanup = [json.loads(line) for line in raw.splitlines() if line.startswith('{')]
    if not any(e.get('stage') == 'netem_cleared' for e in cleanup):
        raise RuntimeError('Netem cleanup was not confirmed')
    if row:
        helper(lab, record)  # recheck immediately before removing only our disposable helper
        lab.docker('rm', row['Id'])
    stats = [r for e in events + cleanup if e.get('stage') == 'netem_cleared' for r in e.get('statistics', [])]
    return {'statistics': stats, 'events': events, 'qdisc_removed': True,
            'watchdog': 'helper lease survives controller death; not helper SIGKILL or host failure'}
