"""Ansible adapter host tests. Ansible/SSH/Docker are NOT simulated as live success.

Actual OS subprocess/timeout/permissions and original CLI parsers are exercised.
Only the AnsibleModule boundary is explicitly doubled in selected tests.
"""
from __future__ import annotations
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'ansible/module_utils'

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

policy = load('ansible.module_utils.db_lab_policy', UTILS / 'db_lab_policy.py')
execution = load('ansible.module_utils.db_lab_execution', UTILS / 'db_lab_execution.py')


def params(action='status', target='mvp', **request):
    return {'project_root': str(ROOT), 'target': target,
            'request': {'action': action, **request},
            'allow_changes': True, 'allow_faults': True, 'allow_destroy': False,
            'confirmation': '', 'k8s': {}}


def argv(p, check=False):
    return policy.build_plan(p, check)['commands'][0][3:]


class PlanTests(unittest.TestCase):
    def test_status_default_does_not_write_database(self):
        p = params(); p['allow_changes'] = False
        self.assertEqual(argv(p), ['mvp', 'status'])
        self.assertEqual(policy.build_plan(p)['category'], 'observe')

    def test_root_common_up_keeps_initialization_and_preflight(self):
        self.assertEqual(argv(params('up', 'kafka')), ['up', 'kafka'])

    def test_es9_is_an_explicit_opt_in_target(self):
        self.assertEqual(argv(params('up', 'elasticsearch9')), ['up', 'elasticsearch9'])
        self.assertEqual(policy.DIRS['elasticsearch9'], 'elasticsearch-9')
        self.assertEqual(argv(params('up', 'all')), ['up', 'all'])

    def test_all_does_not_mix_mvp_and_helm(self):
        self.assertEqual(argv(params('down', 'all')), ['down', 'all'])

    def test_common_health_is_json_not_status_alias(self):
        self.assertEqual(argv(params('health', 'mariadb')), ['health', 'mariadb', '--json'])

    def test_benchmark_routes_only_one_lab_through_root_runner(self):
        cases={'elasticsearch':('es7',{'rate':500,'batch':50}),
               'elasticsearch9':('es9',{'seconds':60,'rate':500,'batch':50}),
               'kafka':('kafka',{'rate':2000,'payload':512}),
               'mariadb':('mariadb',{'workers':4}),
               'redis':('redis',{'rate':300,'workers':8,'keys':2000,'payload':256})}
        for target,(database,options) in cases.items():
            with self.subTest(target=target):
                plan=policy.build_plan(params('benchmark',target,options=options))
                self.assertEqual(plan['commands'][0][3:],
                    ['benchmark',database]+[item for key,value in sorted(options.items())
                                             for item in ('--'+key,str(value))])
                self.assertEqual(plan['category'],'change')
                self.assertEqual(plan['required_consent'],['allow_changes'])

    def test_benchmark_check_mode_plans_without_change_consent(self):
        p=params('benchmark','kafka',options={'seconds':30})
        p['allow_changes']=False
        plan=policy.build_plan(p,check_mode=True)
        self.assertEqual(plan['commands'][0][3:],['benchmark','kafka','--seconds','30'])
        self.assertEqual(plan['required_consent'],['allow_changes'])

    def test_benchmark_rejects_all_target_wrong_options_and_unbounded_values(self):
        invalid=(params('benchmark','all'),
                 params('benchmark','kafka',options={'workers':2}),
                 params('benchmark','redis',options={'rate':10001}),
                 params('benchmark','mariadb',options={'seconds':601}))
        for p in invalid:
            with self.subTest(request=p['request']),self.assertRaises(policy.PolicyError):
                policy.build_plan(p)

    def test_benchmark_requires_explicit_change_consent(self):
        p=params('benchmark','redis');p['allow_changes']=False
        with self.assertRaisesRegex(policy.PolicyError,'allow_changes'):
            policy.build_plan(p)

    def test_restart_is_down_then_up(self):
        cmds = policy.build_plan(params('restart'))['commands']
        self.assertEqual([c[3:] for c in cmds], [['mvp', 'down'], ['mvp', 'up']])

    def test_mutation_requires_change_consent(self):
        p = params('up'); p['allow_changes'] = False
        with self.assertRaisesRegex(policy.PolicyError, 'allow_changes'):
            argv(p)

    def test_fault_requires_separate_consent(self):
        p = params('simulate', verb='run', name='kafka-outage'); p['allow_faults'] = False
        with self.assertRaisesRegex(policy.PolicyError, 'allow_faults'):
            argv(p)

    def test_core_needs_writes_but_not_fault_consent(self):
        p = params('verify', verb='run', name='core'); p['allow_faults'] = False
        self.assertIn('--yes', argv(p))

    def test_baseline_no_fault_consent(self):
        p = params('simulate', verb='run', name='baseline'); p['allow_faults'] = False
        self.assertIn('--yes', argv(p))

    def test_resume_does_not_require_fault_consent(self):
        p = params('resume', name='kafka'); p['allow_faults'] = False
        self.assertEqual(argv(p), ['mvp', 'resume', 'kafka'])

    def test_recovery_requires_writes_not_new_faults(self):
        p = params('simulate', verb='recover'); p['allow_faults'] = False
        self.assertEqual(argv(p), ['mvp', 'simulate', 'recover', '--yes'])

    def test_reset_requires_destroy_flag(self):
        with self.assertRaisesRegex(policy.PolicyError, 'allow_destroy'):
            argv(params('reset', 'redis'))

    def test_reset_requires_exact_confirmation(self):
        p = params('reset', 'redis'); p['allow_destroy'] = True
        with self.assertRaisesRegex(policy.PolicyError, 'confirmation'):
            argv(p)
        p['confirmation'] = 'DELETE:redis'
        self.assertEqual(argv(p), ['reset', 'redis', '--yes'])

    def test_mvp_reset_never_supported(self):
        with self.assertRaises(policy.PolicyError): argv(params('reset'))

    def test_bind_requires_review_token(self):
        with self.assertRaisesRegex(policy.PolicyError, 'confirmation'):
            argv(params('bind-target'))
        p = params('bind-target'); p['confirmation'] = 'BIND:mvp'
        self.assertEqual(argv(p), ['mvp', 'bind-target', '--yes'])

    def test_check_mode_reports_missing_consents_without_call(self):
        p = params('reset', 'redis'); p['allow_changes'] = False
        plan = policy.build_plan(p, check_mode=True)
        self.assertEqual(plan['required_confirmation'], 'DELETE:redis')
        self.assertEqual(plan['required_consent'], ['allow_changes', 'allow_destroy'])

    def test_unused_options_rejected(self):
        with self.assertRaisesRegex(policy.PolicyError, 'unsupported_options'):
            argv(params('up', options={'candidate_image':'mariadb:11.4'}))

    def test_arbitrary_command_rejected(self):
        for command in ('shell', 'exec', 'prune', 'rm -rf /', 'up; touch /tmp/test'):
            with self.subTest(command=command), self.assertRaises(policy.PolicyError):
                argv(params(command))

    def test_unknown_request_key_rejected(self):
        p = params(); p['request']['cmd'] = 'echo unsafe'
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_target_typo_rejected(self):
        with self.assertRaises(policy.PolicyError): argv(params(target='redsi'))

    def test_future_flags_not_passed_through(self):
        with self.assertRaises(policy.PolicyError):
            argv(params('simulate', verb='run', name='baseline', options={'force':True}))

    def test_invalid_target_service_rejected(self):
        with self.assertRaises(policy.PolicyError): argv(params('stop', name='all'))

    def test_no_log_follow_option(self):
        with self.assertRaises(policy.PolicyError): argv(params('logs', options={'follow':True}))

    def test_named_sql_only(self):
        self.assertEqual(argv(params('sql', name='counts')), ['mvp', 'sql', 'counts'])
        with self.assertRaises(policy.PolicyError): argv(params('sql', name='SELECT * FROM orders'))

    def test_snapshot_path_is_one_literal_argument(self):
        path = '/home/lab/backup with $HOME spaces.json'
        p = params('drills', verb='run', name='backup-restore', options={'snapshot':path})
        self.assertEqual(argv(p)[-1], path)

    def test_snapshot_must_be_absolute(self):
        with self.assertRaises(policy.PolicyError):
            argv(params('drills', verb='run', name='backup-restore', options={'snapshot':'../data'}))

    def test_candidate_explicit_tag_only(self):
        with self.assertRaises(policy.PolicyError):
            argv(params('verify', verb='run', name='candidate', options={'candidate_image':'mariadb:latest'}))
        self.assertIn('mariadb:11.4.5', argv(params('verify', verb='run', name='candidate', options={'candidate_image':'mariadb:11.4.5'})))

    def test_candidate_required(self):
        with self.assertRaises(policy.PolicyError): argv(params('verify', verb='run', name='candidate'))

    def test_plan_upgrade_does_not_invent_candidate(self):
        self.assertEqual(argv(params('drills', verb='plan', name='upgrade-restore')), ['mvp','drills','plan','upgrade-restore'])

    def test_invalid_numerics_rejected(self):
        for value in (True, '4', None, 9, -1):
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                argv(params('simulate', verb='plan', name='baseline', options={'workers':value}))

    def test_nonfinite_rate_rejected(self):
        for value in (float('nan'),float('inf'),0,11):
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                argv(params('simulate', verb='plan', name='baseline', options={'rate':value}))

    def test_workflow_limit_checked_before_execution(self):
        with self.assertRaisesRegex(policy.PolicyError,'workflow_budget'):
            argv(params('simulate', verb='run', name='baseline', options={'seconds':180,'rate':10}))

    def test_postfault_window_required(self):
        with self.assertRaisesRegex(policy.PolicyError,'post_fault'):
            argv(params('simulate', verb='plan', name='kafka-outage', options={'seconds':15}))

    def test_version_race_two_workers_required(self):
        with self.assertRaises(policy.PolicyError):
            argv(params('simulate', verb='plan', name='version-race', options={'workers':1}))

    def test_study_only_paired_scenarios(self):
        with self.assertRaises(policy.PolicyError): argv(params('study', verb='run', name='row-lock'))

    def test_compare_requires_two_valid_distinct_ids(self):
        p = params('study', verb='compare', options={'baseline_id':'sim-'+'a'*12,'fault_id':'sim-'+'b'*12})
        self.assertEqual(argv(p)[-2:], ['sim-'+'a'*12,'sim-'+'b'*12])
        p['request']['options']['fault_id'] = 'sim-'+'a'*12
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_runid_traversal_rejected(self):
        with self.assertRaises(policy.PolicyError): argv(params('verify',verb='export',name='../../.env'))

    def test_messages_real_24_character_id(self):
        self.assertIn('msg-'+'a'*24, argv(params('messages',verb='inspect',name='msg-'+'a'*24)))
        with self.assertRaises(policy.PolicyError): argv(params('messages',verb='inspect',name='msg-'+'a'*12))

    def test_keep_boolean_only(self):
        p=params('messages',verb='run',name='poison-schema',options={'keep':False})
        self.assertNotIn('--keep', argv(p))
        p['request']['options']['keep']='false'
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_explicit_bool_required_in_policy(self):
        p=params('up'); p['allow_changes']='false'
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_paths_do_not_allow_parent_or_root(self):
        for path in ('relative', '/', '/a/../b', '//host/data', '/a\n/b'):
            p=params(); p['project_root']=path
            with self.subTest(path=path), self.assertRaises(policy.PolicyError): argv(p)

    def test_k8s_explicit_target_required(self):
        with self.assertRaises(policy.PolicyError): argv(params('status','k8s'))

    def kube(self,action='status'):
        p=params(action,'k8s'); p['k8s']={'context':'kind-lab','allowed_contexts':['kind-lab'],'namespace':'db-lab','release':'db-lab'}
        return p

    def test_k8s_context_and_namespace_passed_via_env(self):
        plan=policy.build_plan(self.kube())
        self.assertEqual(plan['environment']['DB_LAB_CONTEXT'],'kind-lab')
        self.assertEqual(plan['commands'][0][3:],['k8s','status'])

    def test_k8s_context_allowlist_enforced(self):
        p=self.kube(); p['k8s']['context']='prod'
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_k8s_protected_namespace(self):
        p=self.kube(); p['k8s']['namespace']='default'
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_k8s_no_arbitrary_flags(self):
        p=self.kube('up'); p['k8s']['flags']=['--force']
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_k8s_values_paths_preserve_spaces(self):
        p=self.kube('up'); p['k8s']['values_files']=['/home/lab/values with spaces.yml']
        self.assertEqual(argv(p)[-2:],['-f','/home/lab/values with spaces.yml'])

    def test_k8s_down_needs_exact_namespace_release(self):
        p=self.kube('down'); p['allow_destroy']=True;p['confirmation']='UNINSTALL:db-lab/db-lab'
        self.assertIn('--yes',argv(p))
        p['confirmation']='UNINSTALL:prod/db-lab'
        with self.assertRaises(policy.PolicyError): argv(p)

    def test_k8s_vars_not_allowed_for_mvp(self):
        p=params();p['k8s']={'context':'x'}
        with self.assertRaises(policy.PolicyError):argv(p)


class ParserCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(ROOT/'mvp-lab'))
        from tools.manage import parser
        cls.parser=staticmethod(parser)

    def test_all_thirty_run_plans_parse_in_original_cli(self):
        for action, choices in (('simulate',policy.SIMULATIONS),('drills',policy.DRILLS),('messages',policy.MESSAGES)):
            for name in choices:
                opts={'candidate_image':'mariadb:11.4.5'} if name=='upgrade-restore' else {}
                with self.subTest(action=action,name=name):
                    self.parser().parse_args(argv(params(action,verb='run',name=name,options=opts))[1:])

    def test_all_verify_suites_parse(self):
        for name in policy.SUITES:
            opts={'candidate_image':'mariadb:11.4.5'} if name=='candidate' else {'step_timeout':650}
            with self.subTest(name=name): self.parser().parse_args(argv(params('verify',verb='run',name=name,options=opts))[1:])

    def test_all_study_plans_parse(self):
        for name in policy.PAIRED:
            with self.subTest(name=name): self.parser().parse_args(argv(params('study',verb='plan',name=name))[1:])

    def test_catalog_exactly_matches_current_code(self):
        from tools.simulation import SCENARIOS
        from tools.advanced import DRILLS
        from tools.study_analysis import PAIRED
        from mvp_app.message_safety import SCENARIOS as MESSAGES
        self.assertEqual(set(policy.SIMULATIONS),set(SCENARIOS))
        self.assertEqual(set(policy.DRILLS),set(DRILLS))
        self.assertEqual(set(policy.MESSAGES),set(MESSAGES))
        self.assertEqual(set(policy.PAIRED),set(PAIRED))

    def test_examples_build_and_parse(self):
        for path in (ROOT/'ansible/examples').glob('*.yml'):
            data=yaml.safe_load(path.read_text())
            p=params();p.update(target=data['db_lab_target'],request=data['db_lab_request'],k8s=data.get('db_lab_k8s',{}))
            with self.subTest(path=path.name):
                plan=policy.build_plan(p,True)
                if p['target']=='mvp':self.parser().parse_args(plan['commands'][0][4:])


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ansible adapter ');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'project with spaces';self.root.mkdir()
        (self.root/'scripts').mkdir();(self.root/'scripts/control.py').write_text('')
        for dirname in policy.DIRS.values():
            (self.root/dirname).mkdir();(self.root/dirname/'lab.sh').write_text('#!/bin/bash\n')
        self.trace=self.root/'trace.txt'
        self.script='''#!/bin/bash
set -eu
printf '%s\\n' "$*" >> trace.txt
printf 'PRIVATE-NATIVE-OUTPUT'
printf 'PRIVATE-NATIVE-ERROR' >&2
'''
        (self.root/'all.sh').write_text(self.script)

    def plan(self,action='status',**kwargs):
        p=params(action,**kwargs);p['project_root']=str(self.root)
        return policy.build_plan(p)

    def test_all_environment_fingerprint_keeps_es9_opt_in(self):
        for target in policy.DIRS:
            path = self.root / policy.DIRS[target] / '.env'
            path.write_text('LAB_TEST=value\n')
        all_envs = execution.environment_fingerprint(self.root, 'all')
        es9_env = execution.environment_fingerprint(self.root, 'elasticsearch9')
        self.assertNotIn('elasticsearch9', all_envs)
        self.assertIn('elasticsearch9', es9_env)

    def test_project_requires_expected_layout(self):
        policy.validate_project(str(self.root),'mvp')
        (self.root/'mvp-lab/lab.sh').unlink()
        with self.assertRaises(policy.PolicyError):policy.validate_project(str(self.root),'mvp')

    def test_symlink_project_root_rejected(self):
        alias=self.root.parent/'alias';alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(policy.PolicyError):policy.validate_project(str(alias),'mvp')

    def test_symlink_entrypoint_rejected(self):
        (self.root/'all.sh').unlink();(self.root/'all.sh').symlink_to('/bin/true')
        with self.assertRaises(policy.PolicyError):policy.validate_project(str(self.root),'mvp')

    def test_actual_execution_preserves_argument_array_and_cwd(self):
        result=execution.execute_plan(self.plan())
        self.assertEqual(self.trace.read_text().strip(),'--fail-fast mvp status')
        self.assertEqual(result['rc'],0);self.assertFalse(result['changed'])

    def test_default_return_never_contains_native_output(self):
        result=execution.execute_plan(self.plan())
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertIn('PRIVATE',(Path(result['log_directory'])/'00.stdout.log').read_text())

    def test_explicit_output_optin(self):
        result=execution.execute_plan(self.plan(),show_output=True)
        self.assertIn('PRIVATE',result['output'][0]['stdout'])

    def test_logs_and_receipts_are_private(self):
        result=execution.execute_plan(self.plan())
        d=Path(result['log_directory']);self.assertEqual(d.stat().st_mode&0o777,0o700)
        for p in d.iterdir():self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_native_nonzero_is_not_success(self):
        (self.root/'all.sh').write_text(self.script+'\nexit 2\n')
        result=execution.execute_plan(self.plan('up'))
        self.assertEqual(result['rc'],2);self.assertTrue(result['failed']);self.assertTrue(result['changed'])

    def test_restart_stops_after_failed_down(self):
        (self.root/'all.sh').write_text(self.script+'\nexit 7\n')
        result=execution.execute_plan(self.plan('restart'))
        self.assertEqual(len(result['children']),1)
        self.assertNotIn('up',self.trace.read_text())

    def test_second_restart_stage_executes_only_after_success(self):
        result=execution.execute_plan(self.plan('restart'))
        self.assertEqual(len(result['children']),2)
        self.assertEqual(self.trace.read_text().splitlines(),['--fail-fast mvp down','--fail-fast mvp up'])

    def test_native_timeout_is_failure_even_when_term_returns_zero(self):
        d=execution.private_directory(self.root/'timed')
        result=execution.execute(['bash','-c','trap "exit 0" TERM; while :; do sleep .05; done'],self.root,{},d,0,.2,grace=.3)
        self.assertEqual(result['returncode'],124);self.assertTrue(result['timed_out'])

    def test_bounded_logs_are_drained(self):
        d=execution.private_directory(self.root/'bounded')
        result=execution.execute([sys.executable,'-S','-c','print("x"*100000)'],self.root,{},d,0,10,limit=1024)
        self.assertEqual(result['returncode'],0);self.assertTrue(result['output_truncated'])
        self.assertEqual((d/'00.stdout.log').stat().st_size,1024)

    def test_env_not_expanded_as_shell(self):
        d=execution.private_directory(self.root/'literal')
        token='$(touch ESCAPED); $HOME spaces'
        result=execution.execute([sys.executable,'-S','-c','import sys; print(sys.argv[1])',token],self.root,{},d,0,10)
        self.assertEqual((d/'00.stdout.log').read_text().strip(),token)
        self.assertFalse((self.root/'ESCAPED').exists())

    def test_init_changed_tracks_env_not_receipt(self):
        (self.root/'all.sh').write_text(self.script+'\n[ -f mvp-lab/.env ] || printf TEST=value > mvp-lab/.env\n')
        self.assertTrue(execution.execute_plan(self.plan('init'))['changed'])
        self.assertFalse(execution.execute_plan(self.plan('init'))['changed'])

    def test_existing_ansible_lock_prevents_execution(self):
        state=self.root/'.state';state.mkdir()
        with (state/'ansible-control.lock').open('w') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(policy.PolicyError,'another_ansible'):
                execution.execute_plan(self.plan('up'))
        self.assertFalse(self.trace.exists())

    def test_fault_marker_never_deleted_by_adapter(self):
        state=self.root/'mvp-lab/.state';state.mkdir();marker=state/'simulation-active.json';marker.write_text('{"test":true}')
        (self.root/'all.sh').write_text(self.script+'\nexit 1\n')
        execution.execute_plan(self.plan('up'))
        self.assertEqual(marker.read_text(),'{"test":true}')

    def test_symlink_reports_refused(self):
        (self.root/'reports').symlink_to(self.root.parent,target_is_directory=True)
        with self.assertRaises(policy.PolicyError):execution.execute_plan(self.plan())
        self.assertFalse(self.trace.exists())

    def test_symlink_state_refused(self):
        (self.root/'.state').symlink_to(self.root.parent,target_is_directory=True)
        with self.assertRaises(policy.PolicyError):execution.execute_plan(self.plan())

    def test_share_export_requires_all_three_safe_files(self):
        name='accept-'+'a'*12;d=self.root/'mvp-lab/reports/share'/name;d.mkdir(parents=True)
        for n in ('summary.json','report.md','junit.xml'):(d/n).write_text('test')
        self.assertEqual(len(execution.share_paths(self.root,name)),3)
        (d/'report.md').unlink();(d/'report.md').symlink_to(self.root/'all.sh')
        with self.assertRaises(policy.PolicyError):execution.share_paths(self.root,name)

    def test_invalid_timeout_cannot_start_child(self):
        with self.assertRaises(policy.PolicyError):execution.execute_plan(self.plan(),timeout=0)
        self.assertFalse(self.trace.exists())


class ModuleBoundaryTests(ExecutionTests):
    """Only extra tests are counted; inherited OS tests disabled below in loader."""
    def invoke(self,p,check=False):
        class Return(Exception):
            def __init__(self,value):self.value=value
        class FakeModule:
            def __init__(self,**kwargs):
                self.params={key:spec.get('default') for key,spec in kwargs['argument_spec'].items()}
                self.params.update(p);self.check_mode=check
                if not kwargs.get('supports_check_mode'):raise AssertionError('check mode not supported')
            def exit_json(self,**value):raise Return(value)
            def fail_json(self,**value):raise Return(dict(value,failed=True))
        basic=types.ModuleType('ansible.module_utils.basic');basic.AnsibleModule=FakeModule
        with patch.dict(sys.modules,{'ansible.module_utils.basic':basic}):
            mod=load('_db_lab_module_test',ROOT/'ansible/library/db_lab_control.py')
            try:mod.main()
            except Return as exc:return exc.value
        self.fail('module returned no response')

    def test_module_check_mode_runs_no_controller_and_writes_nothing(self):
        p=params('up');p['project_root']=str(self.root);p['allow_changes']=False
        result=self.invoke(p,check=True)
        self.assertEqual(result['execution'],'not_run_check_mode')
        self.assertFalse(self.trace.exists());self.assertFalse((self.root/'.state').exists());self.assertFalse((self.root/'reports').exists())

    def test_module_refuses_unconfirmed_changes_before_files(self):
        p=params('up');p['project_root']=str(self.root);p['allow_changes']=False
        result=self.invoke(p)
        self.assertTrue(result['failed']);self.assertFalse(self.trace.exists());self.assertFalse((self.root/'.state').exists())

    def test_module_propagates_native_failure(self):
        (self.root/'all.sh').write_text(self.script+'\nexit 127\n')
        p=params();p['project_root']=str(self.root)
        result=self.invoke(p)
        self.assertTrue(result['failed']);self.assertEqual(result['rc'],127);self.assertNotIn('PRIVATE',json.dumps(result))

    def test_module_validates_path_even_in_check(self):
        p=params();p['project_root']=str(self.root/'missing')
        result=self.invoke(p,check=True)
        self.assertTrue(result['failed'])

    def test_module_returns_provenance_not_db_certification(self):
        p=params();p['project_root']=str(self.root)
        result=self.invoke(p)
        self.assertFalse(result['database_health_certified']);self.assertIn('plan',result)


# Do not re-count the inherited OS tests for the AnsibleModule double class.
for _name in list(ExecutionTests.__dict__):
    if _name.startswith('test_') and _name not in ModuleBoundaryTests.__dict__:
        setattr(ModuleBoundaryTests,_name,None)


class PlaybookTests(unittest.TestCase):
    def read(self,name):return yaml.safe_load((ROOT/'ansible'/name).read_text())
    def test_serial_failure_contract(self):
        p=self.read('control.yml')[0]
        self.assertEqual(p['serial'],1);self.assertIs(p['any_errors_fatal'],True)
        self.assertIs(p['become'],False);self.assertIs(p['gather_facts'],False)
    def test_control_never_invokes_unrestricted_shell(self):
        s=(ROOT/'ansible/control.yml').read_text()
        self.assertNotIn('ansible.builtin.shell:',s);self.assertNotIn('ignore_errors:',s);self.assertNotIn('async:',s)
    def test_multi_host_writes_must_select_one_host(self):
        rules=self.read('control.yml')[0]['pre_tasks'][0]['ansible.builtin.assert']['that']
        self.assertTrue(any('ansible_play_hosts_all' in x for x in rules))
    def test_inventory_stays_example_only_and_no_secrets(self):
        remote=self.read('inventory/remote.example.yml')
        hosts=remote['all']['children']['db_lab']['hosts']
        self.assertEqual(hosts['lab1']['ansible_host'],'192.0.2.10')
        self.assertNotIn('password',json.dumps(remote))
    def test_collector_is_allowlist_not_recursive(self):
        s=(ROOT/'ansible/collect.yml').read_text()
        self.assertIn('db_lab_export.share_files',s);self.assertIn('not ansible_check_mode',s)
        self.assertNotIn('synchronize:',s);self.assertNotIn('slurp:',s)
    def test_cfg_keeps_host_key_checking(self):
        import configparser
        cfg=configparser.ConfigParser();cfg.read(ROOT/'ansible/ansible.cfg')
        self.assertTrue(cfg['defaults'].getboolean('host_key_checking'))
    def test_module_docs_parse(self):
        tree=__import__('ast').parse((ROOT/'ansible/library/db_lab_control.py').read_text())
        doc=next(n.value.value for n in tree.body if isinstance(n,__import__('ast').Assign) and getattr(n.targets[0],'id','')=='DOCUMENTATION')
        self.assertEqual(yaml.safe_load(doc)['module'],'db_lab_control')


if __name__=='__main__':unittest.main()
