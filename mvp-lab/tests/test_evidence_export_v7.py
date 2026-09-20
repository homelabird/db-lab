"""Public report projection safety; deliberately secret-bearing fixture, no DBs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from tools import acceptance, evidence_export as e, manage

RUN='accept-a123456789ab'

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.directory=self.root/'reports/acceptance'/RUN;self.directory.mkdir(parents=True)
        self.value={'suite':'core','run_id':RUN,'status':'passed',
                    'steps':[{'name':n,'status':'passed','returncode':0,'reason':'PRIVATE-SENTINEL',
                              'payload':{'password':'PRIVATE-SENTINEL'}} for n in
                             ['container-targets','api-runtime-contract','worker-runtime-contract']+[s.name for s in acceptance.steps_for('core')]],
                    'environment':{'password':'PRIVATE-SENTINEL'}, 'recovery_markers':['PRIVATE-SENTINEL']}
        self.save()
    def save(self):manage.atomic_json(self.directory/'summary.json',self.value)
    def test_only_expected_fields_exported(self):
        result=e.project_acceptance(self.root,RUN)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(result['passed_scenarios'],1)
    def test_counts_are_computed_not_copied(self):
        self.value['passed_scenarios']=99999;self.save()
        self.assertEqual(e.project_acceptance(self.root,RUN)['passed_scenarios'],1)
    def test_false_pass_becomes_failure(self):
        self.value['steps'][-1]['status']='not_run';self.save()
        self.assertEqual(e.project_acceptance(self.root,RUN)['status'],'failed')
    def test_missing_step_refused(self):
        self.value['steps'].pop();self.save()
        with self.assertRaises(ValueError):e.project_acceptance(self.root,RUN)
    def test_unknown_step_name_not_copied(self):
        self.value['steps'][0]['name']='PRIVATE-SENTINEL';self.save()
        with self.assertRaises(ValueError):e.project_acceptance(self.root,RUN)
    def test_run_identity_mismatch_refused(self):
        self.value['run_id']='accept-b123456789ab';self.save()
        with self.assertRaises(ValueError):e.project_acceptance(self.root,RUN)
    def test_path_traversal_refused(self):
        with self.assertRaises(ValueError):e.project_acceptance(self.root,'../../.env')
    def test_symlinked_summary_refused(self):
        source=self.directory/'summary.json';target=self.root/'secret.json';source.rename(target);source.symlink_to(target)
        with self.assertRaises(RuntimeError):e.project_acceptance(self.root,RUN)
    def test_symlinked_ancestor_refused(self):
        source=self.directory;target=self.root/'elsewhere';source.rename(target);source.symlink_to(target,target_is_directory=True)
        with self.assertRaises(RuntimeError):e.project_acceptance(self.root,RUN)
    def test_free_text_contract_error_dropped_but_known_code_retained(self):
        data={'result':{'checks':[{'name':'mariadb','status':'failed','code':'unexpected_sql_lock_wait',
                                  'reason':'PRIVATE-SENTINEL','detail':{'sql_password':'PRIVATE-SENTINEL'}}]}}
        manage.atomic_json(self.directory/'api-runtime-contract.json',data)
        result=e.project_acceptance(self.root,RUN)
        self.assertEqual(result['steps'][1]['checks'][0]['code'],'unexpected_sql_lock_wait')
        self.assertNotIn('PRIVATE',json.dumps(result))
    def test_unknown_error_code_dropped(self):
        checks=e.project_checks({'checks':[{'name':'redis','status':'failed','code':'private_secret_token'}]})
        self.assertIsNone(checks[0]['code'])
    def test_unknown_check_not_exported(self):
        self.assertEqual(e.project_checks({'checks':[{'name':'PRIVATE-SENTINEL','status':'failed'}]}),[])
    def test_candidate_input_not_public_exported(self):
        self.value['suite']='candidate';self.save()
        with self.assertRaises(ValueError):e.project_acceptance(self.root,RUN)
    def test_export_has_only_three_fixed_files_and_private_permissions(self):
        path=e.export(self.root,RUN,manage.atomic_json)
        self.assertEqual({p.name for p in path.iterdir()},{'summary.json','report.md','junit.xml'})
        for p in path.iterdir():
            self.assertEqual(p.stat().st_mode&0o777,0o600);self.assertNotIn('PRIVATE',p.read_text())
    def test_no_mixing_previous_export(self):
        e.export(self.root,RUN,manage.atomic_json)
        with self.assertRaises(FileExistsError):e.export(self.root,RUN,manage.atomic_json)
    def test_export_junit_preserves_failure_and_skipped(self):
        self.value['status']='failed';self.value['steps'][1]['status']='failed';self.value['steps'][-1]['status']='not_run';self.save()
        path=e.export(self.root,RUN,manage.atomic_json);xml=ET.parse(path/'junit.xml').getroot()
        self.assertEqual(xml.get('failures'),'1');self.assertEqual(xml.get('skipped'),'1')
    def test_invalid_status_not_leaked(self):
        self.value['status']='PRIVATE-SENTINEL';self.save()
        result=e.project_acceptance(self.root,RUN);self.assertEqual(result['status'],'failed');self.assertNotIn('PRIVATE',str(result))
    def test_extra_file_env_never_read(self):
        (self.directory/'.env').write_text('secret=PRIVATE-SENTINEL')
        path=e.export(self.root,RUN,manage.atomic_json)
        self.assertFalse((path/'.env').exists())
