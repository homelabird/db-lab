"""Commands and cleanup are mocks. No Docker service, DB or resource pressure runs."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace
from tools import manage, advanced
from mvp_app.transaction_model import SCENARIOS, verify_result
from mvp_app.transaction_drill import run
from mvp_app.transaction_sql import Repository
from mvp_app.adapters import Settings
from transaction_sqlite import DeferredWriteConnection

RUN='drill-0123456789ab'
CID='c'*64
IID='sha256:'+'a'*64


def evidence(directory, scenario='stock-race'):
    with patch.dict('os.environ', {'MVP_DISPOSABLE':RUN}):
        path=str(directory/'test.db')
        repo=Repository(Settings(sql_host='127.0.0.1',sql_database='txlab',sql_user='txlab'),
                        connect=lambda: DeferredWriteConnection(path))
        return run(repo, scenario)


class ParserTests(TestCase):
    def parse(self, words): return manage.parser().parse_args(['drills', *words])

    def test_all_existing_and_six_new_names_preserved(self):
        self.assertEqual(set(advanced.DRILLS), set(SCENARIOS)|{'backup-restore','upgrade-restore','redis-disk-full','redis-oom','deadlock'})

    def test_plan_needs_no_config_runtime_or_consent(self):
        m=Mock()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(advanced.cli(self.parse(['plan','stock-race']),m),0)
        value=json.loads(output.getvalue())
        self.assertEqual(value['clients'],4)
        m.lock.assert_not_called()

    def test_list_needs_no_runtime(self):
        m=Mock()
        with redirect_stdout(io.StringIO()) as output: advanced.cli(self.parse(['list']),m)
        self.assertEqual(len(json.loads(output.getvalue())),11)
        m.lock.assert_not_called()

    def test_consented_run_parses_bounds(self):
        args=self.parse(['run','stock-race','--clients','8','--seed','42','--yes'])
        self.assertEqual(args.clients,8); self.assertTrue(args.yes)

    def test_no_consent_cannot_run(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            self.parse(['run','stock-race'])
        self.assertEqual(exc.exception.code,2)

    def test_unknown_scenario_cannot_run(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): self.parse(['run','unknown','--yes'])

    def test_bad_clients_rejected_before_engine(self):
        m=Mock()
        for n in (0,1,9):
            with self.assertRaises(ValueError): advanced.cli(self.parse(['run','stock-race','--clients',str(n),'--yes']),m)
        m.lock.assert_not_called()

    def test_bad_seed_rejected_before_engine(self):
        m=Mock()
        with self.assertRaises(ValueError): advanced.cli(self.parse(['plan','stock-race','--seed','-1']),m)
        m.lock.assert_not_called()

    def test_old_drill_cannot_silently_ignore_tx_options(self):
        m=Mock()
        with self.assertRaises(ValueError): advanced.cli(self.parse(['run','redis-oom','--clients','4','--yes']),m)
        m.lock.assert_not_called()

    def test_old_drill_plan_is_read_only(self):
        m=Mock()
        with redirect_stdout(io.StringIO()) as out: advanced.cli(self.parse(['plan','backup-restore']),m)
        self.assertFalse(json.loads(out.getvalue())['runtime_executed'])
        m.lock.assert_not_called()


class ControllerTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'.state').mkdir()
        self.m=Mock(ROOT=self.root); self.m.atomic_json=manage.atomic_json
        self.lab=Mock(); self.lab.active_path=self.root/'.state/simulation-active.json'
        self.lab.binding.return_value={'local_docker':True,'fingerprint':'a'*64}
        self.lab.c.config={'MVP_PROJECT':'db-lab-mvp'}
        self.store=advanced.Disposable(self.lab,self.m)
        self.store.begin(RUN,'stock-race')
        self.store.create=Mock(return_value=CID)
        self.store.call=Mock(); self.store.wait_db=Mock()
        self.states=[{'service':'mariadb','id':'d'*64,'image_id':IID},{'service':'api','id':'e'*64,'image_id':'sha256:'+'b'*64}]
        self.directory=self.root/'report'; self.directory.mkdir()

    def test_only_isolated_txlab_and_client_share_loopback(self):
        value=evidence(self.root)
        self.store.client=Mock(return_value=value)
        result=self.store.transaction_trial(self.states,self.directory,'stock-race')
        options=self.store.create.call_args.kwargs
        self.assertEqual(options['environment']['MARIADB_DATABASE'],'txlab')
        self.assertEqual(options['environment']['MARIADB_USER'],'txlab')
        self.assertEqual(options['memory'],768)
        self.assertEqual(options['tmpfs'],{'/var/lib/mysql':512})
        self.assertNotIn('network',options) # create's default is none
        self.assertEqual(self.store.client.call_args.args[4]['SQL_HOST'],'127.0.0.1')
        self.assertEqual(self.store.client.call_args.args[4]['SQL_DATABASE'],'txlab')
        self.assertFalse(result['source_database_modified'])
        self.lab.docker.assert_not_called() # no SQL exec on original API/database
        for name in ('transaction.json','report.md','trace.jsonl'):
            self.assertTrue((self.directory/name).exists())
            self.assertEqual((self.directory/name).stat().st_mode & 0o777,0o600)

    def test_malformed_passed_boolean_without_rows_rejected(self):
        self.store.client=Mock(return_value={'status':'passed','observed':True,'scenario':'stock-race'})
        with self.assertRaises(ValueError): self.store.transaction_trial(self.states,self.directory,'stock-race')
        self.assertFalse((self.directory/'transaction.json').exists())

    def test_failed_helper_not_relabelled_inconclusive(self):
        self.store.client=Mock(return_value={'status':'failed','observed':False,'scenario':'stock-race','error':'TimeoutError','trace':[]})
        result=self.store.transaction_trial(self.states,self.directory,'stock-race')
        self.assertEqual(result['status'],'failed')
        self.assertTrue((self.directory/'transaction.json').exists())

    def test_client_json_array_rejected(self):
        self.store.call.return_value=SimpleNamespace(stdout='[]',returncode=0)
        with self.assertRaisesRegex(RuntimeError,'evidence object'):
            self.store.client('image',CID,'module',[],{})

    def test_client_status_exit_disagreement_rejected(self):
        self.store.call.return_value=SimpleNamespace(stdout='{"status":"passed"}',returncode=2)
        with self.assertRaisesRegex(RuntimeError,'disagree'):
            self.store.client('image',CID,'mvp_app.transaction_drill',[],{},accepted_codes=(0,1,2))

    def test_old_client_still_refuses_nonzero_exit(self):
        self.store.call.return_value=SimpleNamespace(stdout='{"matched":false}',returncode=1)
        with self.assertRaisesRegex(RuntimeError,'reported failure'):
            self.store.client('image',CID,'mvp_app.snapshot',[],{})

    def test_returned_success_cannot_hide_corrupt_rows(self):
        value=evidence(self.root)
        value['cases'][1]['snapshot']['wallets'][0]['balance']+=1
        self.store.client=Mock(return_value=value)
        with self.assertRaisesRegex(ValueError,'audit does not match'):
            self.store.transaction_trial(self.states,self.directory,'stock-race')

    def test_output_bound(self):
        self.store.call.return_value=SimpleNamespace(stdout='x'*(8*1024*1024+1),returncode=0)
        with self.assertRaisesRegex(RuntimeError,'budget'):
            self.store.client('image',CID,'module',[],{})

    def test_new_scenario_recovery_uses_existing_owned_cleanup(self):
        self.store.call.return_value=SimpleNamespace(stdout='')
        result=self.store.cleanup()
        self.assertTrue(result['cleaned']); self.assertFalse(self.store.path.exists())


class ReportVerifierTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.value=evidence(Path(self.temp.name))

    def test_original_rows_are_reaudited(self): verify_result(self.value,'stock-race',4,42)

    def test_extra_or_missing_cases_refused(self):
        for rows in ([],self.value['cases']*2):
            value=copy.deepcopy(self.value); value['cases']=rows
            with self.assertRaises(ValueError): verify_result(value,'stock-race',4,42)

    def test_wrong_plan_refused(self):
        with self.assertRaises(ValueError): verify_result(self.value,'stock-race',8,42)

    def test_no_trace_refused(self):
        value=copy.deepcopy(self.value); value['trace']=[]
        with self.assertRaises(ValueError): verify_result(value,'stock-race',4,42)

    def test_wrong_flags_refused(self):
        for field in ('observed','negative_control_observed','protected_passed','unexpected_errors_absent'):
            value=copy.deepcopy(self.value); value[field]=False
            with self.subTest(field=field), self.assertRaises(ValueError): verify_result(value,'stock-race',4,42)
