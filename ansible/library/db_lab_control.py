#!/usr/bin/python
"""Ansible adapter; the existing all.sh remains the sole DB lifecycle entrypoint."""
from __future__ import annotations
DOCUMENTATION = r'''
---
module: db_lab_control
short_description: Run explicitly selected DB Lab commands with native safeguards
version_added: "1.0.0"
description:
  - Uses argument arrays, bounded private output, explicit change/fault/delete consent.
  - Check mode validates inputs and project paths only; it never executes all.sh.
  - This is an imperative adapter, not a declarative HA or desired-state controller.
options:
  project_root:
    description: Existing absolute canonical project path on the managed Linux host.
    type: path
    required: true
  target:
    description: MVP, one standalone lab, four standalone labs, or Helm.
    type: str
    choices: [mvp, all, elasticsearch, kafka, mariadb, redis, k8s]
    default: mvp
  request:
    description: Finite action/verb/name/options mapping documented in ansible/README.md.
    type: dict
    default: {action: status}
  allow_changes:
    description: Explicit consent for lifecycle and synthetic writes.
    type: bool
    default: false
  allow_faults:
    description: Additional consent for fault/recovery lessons.
    type: bool
    default: false
  allow_destroy:
    description: Additional consent for standalone reset or Helm uninstall.
    type: bool
    default: false
  confirmation:
    description: Exact DELETE:target, UNINSTALL:namespace/release or BIND:mvp token.
    type: str
    default: ''
  k8s:
    description: Explicit context/allowed_contexts/namespace/release and optional values_files/kubeconfig.
    type: dict
    default: {}
  timeout:
    description: Per native command timeout in seconds, 30 through 43200; no automatic retry.
    type: int
    default: 3600
  show_output:
    description: Return private native output. May reveal secrets. Leave false in CI.
    type: bool
    default: false
attributes:
  check_mode:
    support: full
    description: Returns plan only. Service readiness is not predicted.
  diff_mode:
    support: none
  platform:
    platforms: posix
author: DB Lab maintainers
'''
EXAMPLES = r'''
- name: Check the MVP
  db_lab_control:
    project_root: /home/lab/db-lab-main
    request: {action: inspect-runtime}
- name: Run a bounded Kafka study
  db_lab_control:
    project_root: /home/lab/db-lab-main
    request:
      action: study
      verb: run
      name: kafka-outage
      options: {seed: 42, workload: write-heavy}
    allow_changes: true
    allow_faults: true
'''
RETURN = r'''
plan:
  description: Validated native argv and required consent. Not DB readiness evidence.
  returned: always
  type: dict
rc:
  description: Original native exit code, or 124 timeout, 130 interruption.
  returned: when executed
  type: int
log_directory:
  description: Private target-side logs, never fetched by the standard collector.
  returned: when execution started
  type: str
share_files:
  description: Exactly three validated export files for the selected acceptance ID.
  returned: after successful verify export
  type: list
'''
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.db_lab_policy import PolicyError, build_plan, validate_project, require
from ansible.module_utils.db_lab_execution import execute_plan


def main():
    module = AnsibleModule(
        argument_spec=dict(
            project_root=dict(type='path', required=True),
            target=dict(type='str', choices=['mvp', 'all', 'elasticsearch', 'kafka', 'mariadb', 'redis', 'k8s'], default='mvp'),
            request=dict(type='dict', default={'action': 'status'}),
            allow_changes=dict(type='bool', default=False),
            allow_faults=dict(type='bool', default=False),
            allow_destroy=dict(type='bool', default=False),
            confirmation=dict(type='str', default=''), k8s=dict(type='dict', default={}),
            timeout=dict(type='int', default=3600), show_output=dict(type='bool', default=False)),
        supports_check_mode=True)
    try:
        require(30 <= module.params['timeout'] <= 43200, 'timeout_out_of_range')
        plan = build_plan(module.params, check_mode=module.check_mode)
        validate_project(module.params['project_root'], module.params['target'])
        if module.check_mode:
            module.exit_json(changed=plan['category'] in ('change', 'fault', 'destroy'),
                             execution='not_run_check_mode', plan=plan,
                             database_health_certified=False)
        result = execute_plan(plan, module.params['timeout'], module.params['show_output'])
        result['plan'] = plan
        if result.pop('failed', False):
            module.fail_json(msg='Native controller did not succeed; inspect private target logs and retained recovery markers.', **result)
        module.exit_json(**result)
    except PolicyError as exc:
        module.fail_json(changed=False, msg=str(exc), error_code=str(exc), execution='refused')
    except (OSError, TypeError, ValueError):
        module.fail_json(changed=False, msg='Adapter validation or filesystem failure; no automatic repair attempted.', execution='refused')


if __name__ == '__main__':
    main()
