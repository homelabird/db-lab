"""Bounded message exercises inside the existing API image, on isolated resources.

Fault checkpoints raise explicit application exceptions. They do NOT claim to kill
the host/container, emulate Kafka rebalancing, or reproduce a network outage.
"""
from __future__ import annotations
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
from .adapters import SQL, Settings
from .core import envelope
from .message_runtime import MessageRuntime
from .message_safety import (Scope, SCENARIOS, PoisonBlocked, InjectedBoundary, canonical,
                             process_record, repair_event, checked_quarantine)
from .observability import safe_error
from .worker import acknowledge


def note(kind, **data):
    print(json.dumps({'kind': kind, **data}, ensure_ascii=False), flush=True)


class Runner:
    def __init__(self, runtime, repo, scope, resources, log=note):
        self.rt, self.repo, self.scope, self.resources, self.log = runtime, repo, scope, resources, log
        self.fixtures, self.versions, self.observations = {}, {}, []
        self.topic_id = resources['topics'][scope.topic]
        self.started = time.monotonic()

    def record(self, stage, **fields):
        event = {'stage': stage, 'elapsed_seconds': round(time.monotonic() - self.started, 4), **fields}
        self.observations.append(event)
        self.log('observation', **event)

    def require(self, truth, reason):
        if not truth:
            raise RuntimeError(reason)

    def prepare(self):
        self.rt.verify_resources(self.resources)
        for topic in (self.scope.topic, self.scope.dlq):
            watermark = self.rt.watermark(topic)
            self.require(watermark['low'] == 0 and watermark['high'] == 0 and watermark['committed'] is None,
                         'New message run must start with empty topics and no checkpoint')
        for name in ('a', 'b', 'c'):
            key = self.scope.run_id + '-' + name
            order, created = self.repo.create({'item': key, 'quantity': 2, 'unit_price': 17}, key)
            self.require(created, 'Message fixture key already existed; run cannot be adopted')
            self.fixtures[order['id']] = key
            self.versions[name] = [order]
            self.rt.cache.put(order)  # only isolated keys; final audits never fill/repair
        self.record('fixtures_created', fixtures=self.fixtures, original_topic_modified_by_drill=False,
                    note='SQL outbox from normal fixture creation still follows the normal worker')

    def send(self, event):
        receipt = self.rt.publish(self.scope.topic, event['order']['id'].encode(), canonical(event))
        self.record('source_ack', **receipt, event_id=event.get('event_id'), version=event['order'].get('version'))
        return receipt

    def consume(self, count, policy='strict', inject=None):
        reader = self.rt.reader()
        results = []
        def quarantine(record):
            receipt = self.rt.publish(self.scope.dlq, record['dlq_id'].encode(), canonical(record))
            self.record('dlq_ack', dlq_id=record['dlq_id'], receipt=receipt, source=record['source'])
            return receipt
        def checkpoint(name):
            if inject == name:
                self.record('injected_boundary', checkpoint=name, type='application_exception_not_process_kill')
                raise InjectedBoundary(name)
        try:
            for _ in range(count):
                message = self.rt.next_message(reader)
                position = {'topic': message.topic(), 'partition': message.partition(), 'offset': message.offset()}
                self.record('received', **position, policy=policy)
                result = process_record(scope=self.scope, topic_id=self.topic_id, position=position,
                    key=message.key(), raw=message.value(), search=self.rt.search, cache=self.rt.cache,
                    commit=lambda: acknowledge(reader, message), quarantine=quarantine,
                    policy=policy, checkpoint=checkpoint)
                self.record('processed', **position, **result)
                results.append(result)
        finally:
            reader.close()
        return results

    def repair(self, expected_unique=1):
        records = self.rt.read_dlq()
        unique = {}
        for record in records:
            checked_quarantine(record, self.scope, self.topic_id)
            if record['dlq_id'] in unique and canonical(unique[record['dlq_id']]) != canonical(record):
                raise RuntimeError('Conflicting copies of the same quarantine record')
            unique[record['dlq_id']] = record
        self.require(len(unique) == expected_unique, 'Unexpected number of distinct poison records')
        repairs = []
        for record in unique.values():
            event = repair_event(record, self.scope, self.topic_id, self.fixtures, self.repo)
            receipt = self.send(event)
            repairs.append({'dlq_id': record['dlq_id'], 'event_id': event['event_id'],
                            'order_id': event['order']['id'], 'version': event['order']['version'], 'receipt': receipt})
        self.record('repaired_from_current_sql', dlq_records=len(records), unique_records=len(unique),
                    duplicate_quarantines=len(records) - len(unique), repairs=repairs)
        self.consume(len(repairs))
        return {'records': records, 'repair_receipts': repairs, 'unique_records': len(unique)}

    def poison(self, scenario):
        a, b, c = (self.versions[n][0] for n in ('a', 'b', 'c'))
        first = envelope(a)
        bad = envelope(copy.deepcopy(a if scenario == 'version-collision' else c))
        if scenario == 'mapping-reject':
            bad['order']['unexpected_study_field'] = 'mapping-reject'
        elif scenario == 'version-collision':
            bad['order']['item'] = 'CORRUPTED-SAME-VERSION'
        else:
            bad['schema_version'] = 99
        events = [first, bad, envelope(b)]
        if scenario == 'version-collision':
            events.append(envelope(c))
        for event in events:
            self.send(event)
        try:
            self.consume(len(events))
            raise RuntimeError('Strict consumer unexpectedly crossed the poison record')
        except PoisonBlocked as exc:
            self.record('strict_blocked', reason=exc.reason, position=exc.position)
            expected = ('event_version_payload_conflict' if scenario == 'version-collision' else
                        'strict_dynamic_mapping_exception' if scenario == 'mapping-reject' else 'unsupported_event_schema')
            self.require(exc.position['offset'] == 1, 'Poison block at an unexpected source offset')
            self.require(exc.reason == expected or (scenario == 'mapping-reject' and exc.reason == 'mapper_parsing_exception'),
                         'Observed failure is not the scenario requested')
        before = self.rt.watermark()
        self.require(before['committed'] == 1, 'Source offset advanced past the failed event')
        self.require(self.rt.exact(b['id']) is None, 'Later normal event was processed past the poison event')
        if scenario == 'version-collision':
            self.require(canonical(self.rt.exact(a['id'])) == canonical(a), 'Same-version conflict overwrote the document')
        self.record('head_of_line_block_observed', checkpoint=before, later_order_absent=True)
        if scenario == 'dlq-commit-gap':
            try:
                self.consume(1, policy='quarantine', inject='after_dlq_ack')
                raise RuntimeError('DLQ boundary injection did not execute')
            except InjectedBoundary:
                pass
            self.require(self.rt.watermark()['committed'] == 1, 'Source committed during DLQ ACK gap')
            self.require(len(self.rt.read_dlq()) == 1, 'DLQ ACK did not produce a readable record')
        self.consume(len(events) - 1, policy='quarantine')
        self.require(self.rt.watermark()['committed'] == len(events), 'Source did not progress after durable quarantine')
        self.require(self.rt.exact(b['id']) is not None, 'Later healthy order still blocked')
        repair = self.repair()
        if scenario == 'dlq-commit-gap':
            self.require(len(repair['records']) == 2 and repair['unique_records'] == 1,
                         'Expected repeated quarantine of the same source position')
        return repair

    def projection_gap(self):
        for name in ('a', 'b', 'c'):
            self.send(envelope(self.versions[name][0]))
        try:
            self.consume(1, inject='after_projection')
            raise RuntimeError('Projection boundary injection did not execute')
        except InjectedBoundary:
            pass
        self.require(self.rt.watermark()['committed'] in (None, 0), 'Checkpoint advanced during projection gap')
        self.require(self.rt.exact(self.versions['a'][0]['id']) is not None, 'No actual projection before injected gap')
        result = self.consume(3)
        self.require(result[0]['outcome'] == 'duplicate_ignored', 'Redelivered projection was not an identical duplicate')
        received = [r['offset'] for r in self.observations if r['stage'] == 'received']
        self.require(received[:2] == [0, 0], 'The uncommitted source offset was not redelivered')
        return {'redelivery_offsets': received, 'dlq_records': self.rt.read_dlq()}

    def ordering(self):
        order = self.versions['a'][0]
        for state in ('paid', 'shipped'):
            order = self.repo.update(order['id'], {'expected_version': order['version'], 'status': state})
            self.versions['a'].append(order)
        versions = self.versions['a']
        for i in (1, 0, 2, 1, 2, 0):
            self.send(envelope(versions[i]))
        for name in ('b', 'c'):
            self.send(envelope(self.versions[name][0]))
        result = self.consume(8)
        outcomes = [r['outcome'] for r in result]
        self.require(outcomes.count('older_version_ignored') == 3 and outcomes.count('duplicate_ignored') == 1,
                     'Old/duplicate records not classified as expected')
        return {'outcomes': outcomes, 'source_versions': [v['version'] for v in versions],
                'dlq_records': self.rt.read_dlq()}

    def run(self, scenario):
        if scenario not in SCENARIOS:
            raise ValueError('Unsupported message scenario')
        self.prepare()
        if scenario == 'projection-commit-gap':
            observation = self.projection_gap()
        elif scenario == 'replay-ordering':
            observation = self.ordering()
        else:
            observation = self.poison(scenario)
        consecutive = 0
        deadline = time.monotonic() + 20
        audits = []
        while time.monotonic() < deadline:
            result = self.rt.audit(self.repo, self.fixtures)
            audits.append(result)
            consecutive = consecutive + 1 if result['matched'] else 0
            if consecutive == 2:
                break
            time.sleep(.5)
        self.require(consecutive == 2, 'Source/search/cache did not match on two final observations')
        checkpoint = self.rt.watermark()
        self.require(checkpoint['committed'] == checkpoint['high'], 'Study consumer has uncommitted events')
        self.record('final_audit', matched=True, checkpoint=checkpoint)
        return {'status': 'passed', 'scenario': scenario, 'observation': observation,
                'audits': audits[-2:], 'fixtures': self.fixtures, 'checkpoint': checkpoint,
                'normal_worker_policy_changed': False, 'normal_offsets_reset': False,
                'evidence_kind': self.rt.evidence_kind,
                'fault_model': 'explicit application checkpoints; no process kill or broker rebalance'}


def runtime_settings(scope):
    settings = Settings.load()
    expected = {'sql_host': 'mariadb', 'sql_database': 'mvp', 'sql_user': 'mvp',
                'kafka_bootstrap': 'kafka:9092', 'kafka_topic': 'mvp.orders.v1',
                'kafka_group': 'mvp.search.v1', 'es_url': 'http://elasticsearch:9200',
                'es_index': 'mvp-orders-v1', 'redis_host': 'redis'}
    if os.environ.get('STUDY_PROJECT') != scope.project or any(getattr(settings, k) != v for k, v in expected.items()):
        raise ValueError('API environment is not the selected normal MVP; no message resource touched')
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create-topic', 'create-index', 'run', 'inspect', 'cleanup'))
    args = parser.parse_args()
    runtime = None
    def expire(*_):
        raise TimeoutError('Message helper hard deadline expired')
    old = signal.signal(signal.SIGALRM, expire)
    signal.alarm(150)
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('Message control input too large')
        request = json.loads(raw)
        scope = Scope(**request['scope'])
        settings = runtime_settings(scope)
        # Shared by every RPC, including cleanup. Killing just the host controller
        # cannot make cleanup race an in-container helper still processing events.
        path = Path('/tmp') / ('mvp-' + scope.run_id + '-' + scope.token + '.lock')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('Study helper is still running; recovery refused') from exc
            runtime = MessageRuntime(settings, scope)
            resources = request.get('resources', {})
            if args.action == 'create-topic':
                result = runtime.create_topic(request['topic'])
            elif args.action == 'create-index':
                result = runtime.create_index()
            elif args.action == 'cleanup':
                result = runtime.cleanup(resources)
            elif args.action == 'inspect':
                runtime.verify_resources(resources)
                result = {'scope': scope.public(), 'checkpoint': runtime.watermark(),
                          'dlq': runtime.read_dlq(), 'projection': runtime.search.find('')}
            else:
                result = Runner(runtime, SQL(settings), scope, resources).run(request['scenario'])
            note('result', result=result)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # No DSN, raw subprocess stderr or arbitrary driver exception text.
        note('failure', **safe_error(exc), reason=str(exc) if type(exc) in (RuntimeError, ValueError, TimeoutError) else None)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
        if runtime is not None:
            runtime.close()


if __name__ == '__main__':
    raise SystemExit(main())
