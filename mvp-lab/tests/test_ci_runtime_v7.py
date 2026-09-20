"""Fresh-runner pipeline with subprocess/engine doubles; no real image or DB claim."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch
from tools import acceptance, manage

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('ci_mvp',ROOT/'scripts/ci-mvp.py')
ci=importlib.util.module_from_spec(spec);spec.loader.exec_module(ci)
RUN='accept-a123456789ab'

def response(stdout='',code=0):
    return {'stdout':stdout,'returncode':code,'interrupted':False,'timed_out':False,'elapsed_seconds':0,'stderr_bytes':0}

class FreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        (self.root/'mvp-lab').mkdir()
    def test_existing_env_refused_before_engine(self):
        (self.root/'mvp-lab/.env').write_text('PRIVATE')
        with self.assertRaisesRegex(RuntimeError,'fresh_workspace'):ci.fresh_workspace(self.root,{})
    def test_existing_state_refused(self):
        (self.root/'mvp-lab/.state').mkdir()
        with self.assertRaises(RuntimeError):ci.fresh_workspace(self.root,{})
    def test_previous_reports_refused(self):
        (self.root/'mvp-lab/reports').mkdir()
        with self.assertRaises(RuntimeError):ci.fresh_workspace(self.root,{})
    def test_remote_host_refused(self):
        with self.assertRaises(RuntimeError):ci.fresh_workspace(self.root,{'DOCKER_HOST':'tcp://other:2375'})
    def test_self_hosted_runner_refused(self):
        with self.assertRaises(RuntimeError):ci.fresh_workspace(self.root,{'RUNNER_ENVIRONMENT':'self-hosted'})
    def test_missing_engine_is_not_pass(self):
        with patch.object(ci.shutil,'which',return_value=None),self.assertRaises(FileNotFoundError):ci.fresh_workspace(self.root,{})
    def test_fresh_local_accepted_without_creating_state(self):
        with patch.object(ci.shutil,'which',return_value='/fake/docker'):ci.fresh_workspace(self.root,{})
        self.assertFalse((self.root/'mvp-lab/.state').exists())

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.mvp=self.root/'mvp-lab';self.mvp.mkdir()
        shutil.copy2(ROOT/'mvp-lab/.env.example',self.mvp/'.env.example')
        self.calls=[];self.fail=None;self.timed=False;self.pin_local=True;self.existing=False
        self.compose=Mock();self.compose.engine_identity.side_effect=lambda:{'local_docker':self.pin_local}
        patches=[patch.object(ci,'ROOT',self.root),patch.object(manage,'ROOT',self.mvp),
                 patch.object(ci.shutil,'which',return_value='/fake/docker'),
                 patch.object(ci.shutil,'disk_usage',return_value=types.SimpleNamespace(free=9*1024**3)),
                 patch.object(manage,'Compose',return_value=self.compose),
                 patch.object(ci.subprocess,'run',side_effect=self.docker),
                 patch.object(acceptance,'execute',side_effect=self.command),
                 patch.object(ci,'collect',return_value={'ready':True,'containers':[]}),
                 patch.dict(ci.os.environ,{'DOCKER_HOST':'','RUNNER_ENVIRONMENT':'github-hosted'})]
        for p in patches:p.start();self.addCleanup(p.stop)
    def docker(self,argv,**kwargs):
        if argv[:2]==['docker','info']:return types.SimpleNamespace(stdout=json.dumps({'OSType':'linux','MemTotal':7*1024**3}))
        return types.SimpleNamespace(stdout='existing' if self.existing else '')
    def command(self,argv,**kwargs):
        action=argv[2];self.calls.append(action)
        if action==self.fail:
            result=response('PRIVATE-SENTINEL',1);result['timed_out']=self.timed;return result
        if action=='init':
            with redirect_stdout(io.StringIO()):manage.init()
        if action=='verify':
            directory=self.mvp/'reports/acceptance'/RUN;directory.mkdir(parents=True)
            value={'suite':'core','run_id':RUN,'status':'passed','steps':[
                {'name':name,'status':'passed','returncode':0,'reason':'PRIVATE-SENTINEL'} for name in
                ['container-targets','api-runtime-contract','worker-runtime-contract']+[s.name for s in acceptance.steps_for('core')]]}
            manage.atomic_json(directory/'summary.json',value)
            return response(json.dumps({'status':'passed','run_id':RUN}))
        return response('PRIVATE-SENTINEL')
    def run_pipeline(self):
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):return ci.run(['--allow-disposable-runner','--yes'])
    def test_success_is_derived_from_current_acceptance(self):
        self.assertEqual(self.run_pipeline(),0);self.assertEqual(self.calls,['init','doctor','up','verify','down'])
        result=json.loads((self.root/'artifacts/mvp-runtime/summary.json').read_text());self.assertTrue(result['live_acceptance_passed'])
    def test_build_failure_does_not_run_verify(self):
        self.fail='up';self.assertEqual(self.run_pipeline(),1);self.assertNotIn('verify',self.calls)
        value=json.loads((self.root/'artifacts/mvp-runtime/summary.json').read_text());self.assertFalse(value['live_acceptance_passed'])
    def test_doctor_failure_does_not_start_stack(self):
        self.fail='doctor';self.assertEqual(self.run_pipeline(),1);self.assertNotIn('up',self.calls);self.assertNotIn('down',self.calls)
    def test_shutdown_failure_does_not_turn_acceptance_into_pipeline_pass(self):
        self.fail='down';self.assertEqual(self.run_pipeline(),1)
        result=json.loads((self.root/'artifacts/mvp-runtime/summary.json').read_text());self.assertEqual(result['status'],'failed')
    def test_public_bundle_contains_no_raw_stdout_or_arbitrary_reason(self):
        self.run_pipeline()
        for path in (self.root/'artifacts/mvp-runtime').rglob('*'):
            if path.is_file():self.assertNotIn('PRIVATE-SENTINEL',path.read_text())
        self.assertIn('PRIVATE-SENTINEL',(self.root/'artifacts/private/up.stdout.txt').read_text() if (self.root/'artifacts/private/up.stdout.txt').exists() else (self.root/'artifacts/private/build-and-start.stdout.txt').read_text())
    def test_new_project_name_and_password_preserved_by_lifecycle(self):
        self.run_pipeline();config=manage.parse_env(self.mvp/'.env')
        self.assertTrue(config['MVP_PROJECT'].startswith('db-lab-mvp-ci-'));self.assertEqual(config['MVP_ENGINE'],'docker')
        self.assertEqual(len(config['SQL_PASSWORD']),48)
    def test_remote_effective_context_stops_before_start(self):
        self.pin_local=False;self.assertEqual(self.run_pipeline(),1);self.assertNotIn('up',self.calls)
    def test_collision_stops_before_start(self):
        self.existing=True;self.assertEqual(self.run_pipeline(),1);self.assertNotIn('up',self.calls)
    def test_fresh_checkout_required_no_existing_password_changes(self):
        (self.mvp/'.env').write_text('PRIVATE-SENTINEL')
        self.assertEqual(self.run_pipeline(),1);self.assertEqual(self.calls,[]);self.assertEqual((self.mvp/'.env').read_text(),'PRIVATE-SENTINEL')
    def test_runtime_absence_emits_blocked_artifact(self):
        with patch.object(ci.shutil,'which',return_value=None):self.assertEqual(self.run_pipeline(),127)
        result=json.loads((self.root/'artifacts/mvp-runtime/summary.json').read_text());self.assertEqual(result['status'],'blocked')
    def test_build_timeout_is_not_success(self):
        self.fail='up';self.timed=True;self.assertEqual(self.run_pipeline(),1);self.assertNotIn('verify',self.calls)
    def test_no_confirmation_no_changes(self):
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):ci.run([])
        self.assertFalse((self.root/'artifacts').exists())

class WorkflowTests(unittest.TestCase):
    def test_manual_only_and_no_self_hosted_or_pr_target(self):
        import yaml
        value=yaml.load((ROOT/'.github/workflows/mvp-runtime.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(value['on']),{'workflow_dispatch'})
        self.assertEqual(value['permissions'],{'contents':'read'})
        self.assertEqual(value['jobs']['runtime']['runs-on'],'ubuntu-24.04')
        self.assertEqual(value['jobs']['runtime']['needs'],'offline')
    def test_artifact_upload_is_not_recursive_project_upload(self):
        import yaml
        value=yaml.load((ROOT/'.github/workflows/mvp-runtime.yml').read_text(),Loader=yaml.BaseLoader)
        steps=value['jobs']['runtime']['steps'];upload=steps[-1]
        self.assertEqual(upload['with']['path'],'artifacts/mvp-runtime/')
        self.assertEqual(upload['with']['if-no-files-found'],'error')
    def test_actions_do_not_persist_checkout_credentials(self):
        import yaml
        value=yaml.load((ROOT/'.github/workflows/mvp-runtime.yml').read_text(),Loader=yaml.BaseLoader)
        for job in value['jobs'].values():
            checkout=next(step for step in job['steps'] if step.get('uses','').startswith('actions/checkout@'))
            self.assertEqual(checkout['with']['persist-credentials'],'false')
    def test_all_inputs_are_quoted_environment_not_shell_interpolation(self):
        text=(ROOT/'.github/workflows/mvp-runtime.yml').read_text()
        self.assertIn('--suite "$SELECTED_SUITE"',text)
        for line in text.splitlines():
            if '--suite' in line:self.assertNotIn('${{',line)

    def test_missing_consent_is_not_an_all_skipped_green_workflow(self):
        import yaml
        value=yaml.load((ROOT/'.github/workflows/mvp-runtime.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertNotIn('if',value['jobs']['offline'])
        self.assertIn('test "$ALLOW_DISPOSABLE" = true',value['jobs']['offline']['steps'][0]['run'])
