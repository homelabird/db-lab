"""Honest same-workload comparison for exported Redis load reports."""
from __future__ import annotations

import json
from pathlib import Path


def _load(path: str | Path) -> tuple[dict, str | None]:
    report_path = Path(path)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('status') != 'PASS' or report.get('scenario') != 'load':
        raise ValueError(f'{report_path}: expected a completed PASS load report')
    result = report.get('findings', {}).get('load')
    if not isinstance(result, dict):
        raise ValueError(f'{report_path}: missing load findings')
    versions = set()
    metrics = report_path.parent / 'metrics.jsonl'
    if metrics.is_file():
        for line in metrics.read_text(encoding='utf-8').splitlines():
            try:
                version = json.loads(line).get('info', {}).get('redis_version')
            except (ValueError, AttributeError):
                continue
            if version:
                versions.add(str(version))
    return report, ','.join(sorted(versions)) if versions else None


def compare_reports(baseline: str | Path, candidate: str | Path) -> dict:
    a, av = _load(baseline)
    b, bv = _load(candidate)
    differences = []
    if a.get('parameters') != b.get('parameters'):
        differences.append('workload parameters differ')
    if not av or not bv or av != bv:
        differences.append('Redis version differs or was not observed in both runs')
    x = a['findings']['load']; y = b['findings']['load']
    xc = x.get('counts', {}); yc = y.get('counts', {})
    def delta(old, new):
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            return None
        return {'absolute': round(new-old, 4),
                'percent': round((new-old)*100/old, 2) if old else None}
    old_p95 = (x.get('rtt_ms') or {}).get('p95_upper')
    new_p95 = (y.get('rtt_ms') or {}).get('p95_upper')
    return {
        'comparable': not differences,
        'differences': differences,
        'baseline': {'run_id': a.get('run_id'), 'redis_version': av,
                     'success': xc.get('success', 0), 'errors': xc.get('error', 0),
                     'success_rps': x.get('achieved_success_rps'),
                     'rtt_ms': x.get('rtt_ms')},
        'candidate': {'run_id': b.get('run_id'), 'redis_version': bv,
                      'success': yc.get('success', 0), 'errors': yc.get('error', 0),
                      'success_rps': y.get('achieved_success_rps'),
                      'rtt_ms': y.get('rtt_ms')},
        'candidate_minus_baseline': {
            'success_rps': delta(x.get('achieved_success_rps'), y.get('achieved_success_rps')),
            'errors': delta(xc.get('error', 0), yc.get('error', 0)),
            'rtt_p95_upper_ms': delta(old_p95, new_p95),
        },
        'interpretation': 'Observed run results only. Keep host, container limits, dataset state, and competing load constant; this tool cannot verify them or infer production capacity.',
    }
