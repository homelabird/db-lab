"""Controller tests use explicit subprocess/engine doubles, not Docker execution."""
from contextlib import nullcontext,redirect_stdout,redirect_stderr
import io,json,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from tools import manage
from tools.study import Study,study_plan
from tools.study_analysis import PAIRED,EvidenceError
from tools.simulation import Plan
from study_fixtures import fixture,save

class StudyPlanTests(unittest.TestCase):
    def test_nine_pairs_have_identical_operations(self):
        self.assertEqual(len(PAIRED),9)
        for name in PAIRED:
            p=study_plan(Plan(scenario=name));self.assertEqual(p['baseline']['operations'],p['fault']['operations'])
            self.assertEqual([s['name'] for s in p['steps']],['core-gate','baseline','fault'])
    def test_specialized_workflows_refused(self):
        for name in ('baseline','version-race','duplicate-retry','row-lock'):
            with self.subTest(name=name),self.assertRaises(ValueError):study_plan(Plan(scenario=name))
    def test_all_commands_require_consent(self):self.assertTrue(all('--yes' in x['command'] for x in study_plan(Plan(scenario='kafka-outage'))['steps']))
    def test_plan_deterministic(self):self.assertEqual(study_plan(Plan(scenario='redis-outage',seed=1)),study_plan(Plan(scenario='redis-outage',seed=1)))
    def test_cli_plan_needs_no_engine(self):
        with patch.object(manage,'Compose',side_effect=AssertionError()),redirect_stdout(io.StringIO()):self.assertEqual(manage.main(['study','plan','kafka-outage']),0)
    def test_run_without_yes_refused(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):manage.parser().parse_args(['study','run','kafka-outage'])
    def test_bounds_not_bypassed(self):
        with self.assertRaises(ValueError):study_plan(Plan(scenario='kafka-outage',workers=9))
    def test_plan_warns_data_not_reset(self):self.assertIn('source data/cache/host load',str(study_plan(Plan(scenario='kafka-outage'))))

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.m=types.SimpleNamespace(ROOT=self.root,atomic_json=manage.atomic_json,require_no_active_fault=Mock(),lock=lambda:nullcontext())
        self.calls=[];self.behavior=None
    def result(self,detail,code=0,**changes):return {'returncode':code,'stdout':json.dumps(detail),'stderr_bytes':0,'elapsed_seconds':.01,'timed_out':False,'interrupted':False,**changes}
    def executor(self,argv,**kwargs):
        action=argv[2:];self.calls.append(action)
        if self.behavior:
            result=self.behavior(action)
            if result is not None:return result
        if action[0]=='verify':
            rid='accept-'+'c'*12;path=self.root/'reports/acceptance'/rid;path.mkdir(parents=True,exist_ok=True)
            value={'run_id':rid,'suite':'core','status':'passed','evidence_kind':'LIVE-ACCEPTANCE-ATTEMPT','passed_scenarios':1,'planned_scenarios':1,'recovery_markers':[],
                   'steps':[{'name':n,'status':'passed'} for n in ('container-targets','api-runtime-contract','worker-runtime-contract','smoke','simulate-baseline')]}
            (path/'summary.json').write_text(json.dumps(value));detail={'run_id':rid,'report_directory':str(path),'status':'passed'}
        else:
            run=fixture(action[2],live=True);path=save(self.root,run);detail={'run_id':run.run_id,'report':str(path/'report.md'),'status':'passed'}
        return self.result(detail)
    def run_study(self,check=None):
        s=Study(self.m,Plan(scenario='redis-outage'),executor=self.executor)
        with patch('tools.study.shutil.which',return_value='/test-double/docker'),patch.object(s,'check_reference',side_effect=check),redirect_stdout(io.StringIO()):code=s.run()
        return s,code
    def test_success_core_then_baseline_then_fault(self):
        s,c=self.run_study();self.assertEqual(c,0);self.assertEqual(s.summary['status'],'passed')
        self.assertEqual([a[:3] for a in self.calls],[['verify','run','core'],['simulate','run','baseline'],['simulate','run','redis-outage']]);self.assertTrue((s.directory/'comparison/report.html').is_file())
    def test_missing_docker_blocks_without_subprocess(self):
        s=Study(self.m,Plan(scenario='redis-outage'),executor=self.executor)
        with patch('tools.study.shutil.which',return_value=None),redirect_stdout(io.StringIO()):self.assertEqual(s.run(),127)
        self.assertEqual(self.calls,[]);self.assertEqual(s.summary['paired_runs_passed'],0);self.assertEqual(s.summary['steps'][1]['status'],'not_run')
    def test_failed_core_stops(self):
        self.behavior=lambda a:self.result({'status':'failed'},1) if a[0]=='verify' else None
        s,c=self.run_study();self.assertEqual(c,1);self.assertEqual(len(self.calls),1);self.assertEqual(s.summary['steps'][1]['status'],'not_run')
    def test_failed_baseline_never_injects_fault(self):
        self.behavior=lambda a:self.result({'status':'failed','run_id':'sim-'+'a'*12},1) if a[:3]==['simulate','run','baseline'] else None
        s,c=self.run_study();self.assertEqual(c,1);self.assertEqual(len(self.calls),2);self.assertEqual(s.summary['steps'][2]['status'],'not_run')
    def test_inconclusive_baseline_stops(self):
        self.behavior=lambda a:self.result({'status':'inconclusive'},2) if a[0]=='simulate' else None
        s,c=self.run_study();self.assertEqual(c,2);self.assertEqual(len(self.calls),2)
    def test_timeout_rc_zero_not_passed(self):
        self.behavior=lambda a:self.result({'status':'passed'},timed_out=True)
        s,c=self.run_study();self.assertEqual(c,1);self.assertEqual(len(self.calls),1)
    def test_context_change_blocks_before_fault(self):
        s,c=self.run_study(check=EvidenceError('context_changed_before_fault_no_fault_was_started'))
        self.assertEqual(c,1);self.assertEqual(len(self.calls),2);self.assertEqual(s.summary['steps'][2]['reason'],'context_changed_before_fault_no_fault_was_started')
    def test_old_core_report_not_reused(self):
        (self.root/'reports/acceptance'/('accept-'+'c'*12)).mkdir(parents=True)
        s,c=self.run_study();self.assertEqual(c,1);self.assertEqual(len(self.calls),1);self.assertIn('old_core',s.summary['steps'][0]['reason'])
    def test_old_simulation_not_reused(self):
        save(self.root,fixture(live=True));s,c=self.run_study();self.assertEqual(c,1);self.assertIn('old_simulation',s.summary['steps'][1]['reason']);self.assertEqual(len(self.calls),2)
    def test_forged_pass_missing_report_refused(self):
        self.behavior=lambda a:self.result({'status':'passed','run_id':'accept-'+'c'*12,'report_directory':'/tmp/not-here'})
        s,c=self.run_study();self.assertEqual(c,1);self.assertEqual(len(self.calls),1)
    def test_test_double_evidence_not_live_pass(self):
        def behavior(a):
            if a[0]=='simulate':
                r=fixture();p=save(self.root,r);return self.result({'status':'passed','run_id':r.run_id,'report':str(p/'report.md')})
        self.behavior=behavior;s,c=self.run_study();self.assertEqual(c,1);self.assertEqual(len(self.calls),2)
    def test_keyboard_interrupt_stops(self):
        def interrupt(a):raise KeyboardInterrupt()
        self.behavior=interrupt;s,c=self.run_study();self.assertEqual(c,130);self.assertEqual(len(self.calls),1)
    def test_active_fault_blocks_before_child(self):
        self.m.require_no_active_fault.side_effect=RuntimeError('pending fault')
        s,c=self.run_study();self.assertEqual(c,1);self.assertEqual(len(self.calls),0)
    def test_invalid_budget_before_directory_creation(self):
        with self.assertRaises(ValueError):Study(self.m,Plan(scenario='redis-outage'),step_timeout=1)
        self.assertFalse((self.root/'reports').exists())
    def test_raw_child_output_not_copied(self):
        s,c=self.run_study();data=json.loads((s.directory/'core-gate-execution.json').read_text());self.assertNotIn('stdout',data);self.assertNotIn('stderr',data)
    def test_compare_cli_no_compose_or_repair(self):
        l,r=fixture(),fixture('redis-outage');save(self.root,l);save(self.root,r)
        with patch.object(manage,'ROOT',self.root),patch.object(manage,'Compose',side_effect=AssertionError()),redirect_stdout(io.StringIO()):c=manage.main(['study','compare',l.run_id,r.run_id])
        self.assertEqual(c,2);self.assertEqual(len(list((self.root/'reports/comparisons').glob('*/report.html'))),1)
