"""Regression tests. Bash/subprocess tests execute real processes with EXPLICIT DB/runtime doubles.
These tests do NOT start MariaDB, Docker, Podman, or a Galera cluster.
LAB_TEST_PROJECT_ROOT allows the exact same regression to run against an older project copy.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(os.environ.get('LAB_TEST_PROJECT_ROOT', Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
import health
stub = types.ModuleType('pymysql')
stub.MySQLError = type('ExplicitFakeMySQLError', (Exception,), {})
with patch.dict(sys.modules, {'pymysql':stub}):
    spec = importlib.util.spec_from_file_location('workload_regression', ROOT/'scripts/workload.py')
    workload = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workload)


def done(code=0, stdout='', stderr=''):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class RegressionEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mariadb env regression ')
        self.root = Path(self.tmp.name)
        self.example = (ROOT/'.env.example').read_text()
        (self.root/'.env.example').write_text(self.example)
        self.valid = self.example.replace('GENERATE_WITH_LAB_INIT', 'testPassword123456')
        self.root_patch = patch.object(lab,'ROOT',self.root); self.root_patch.start()
        self.out = contextlib.redirect_stdout(io.StringIO()); self.out.__enter__()
    def tearDown(self):
        self.out.__exit__(None,None,None);self.root_patch.stop();self.tmp.cleanup()
    def test_copied_example_is_repaired(self):
        (self.root/'.env').write_text(self.example)
        lab.initialize()
        self.assertNotIn('GENERATE_WITH_LAB_INIT',(self.root/'.env').read_text())
        lab.load_env(self.root/'.env')
    def test_init_does_not_rotate_valid_passwords(self):
        (self.root/'.env').write_text(self.valid)
        lab.initialize()
        self.assertEqual((self.root/'.env').read_text(),self.valid)
    def test_init_fills_missing_password_only(self):
        text=self.valid.replace('SST_PASSWORD=testPassword123456\n','')
        (self.root/'.env').write_text(text)
        lab.initialize(); cfg=lab.load_env(self.root/'.env')
        self.assertEqual(cfg['ROOT_PASSWORD'],'testPassword123456')
        self.assertNotEqual(cfg['SST_PASSWORD'],'testPassword123456')
    def test_init_refuses_rotating_existing_identity(self):
        (self.root/'.env').write_text(self.example)
        (self.root/'.state').mkdir();(self.root/'.state/identity.json').write_text('{}')
        with self.assertRaises(lab.LabError):lab.initialize()
    def test_duplicate_configuration_fails(self):
        (self.root/'.env').write_text(self.valid+'\nNODE1_PORT=15000\n')
        with self.assertRaises(lab.LabError):lab.load_env(self.root/'.env')
    def test_missing_port_is_friendly_error(self):
        (self.root/'.env').write_text(self.valid.replace('NODE1_PORT=13301\n',''))
        with self.assertRaises(lab.LabError):lab.load_env(self.root/'.env')
    def test_init_validates_before_replacing_file(self):
        text=self.example.replace('WRITER_PORT=13306','WRITER_PORT=13301')
        (self.root/'.env').write_text(text)
        with self.assertRaises(lab.LabError):lab.initialize()
        self.assertEqual((self.root/'.env').read_text(),text)
    def test_existing_env_is_private(self):
        (self.root/'.env').write_text(self.valid);(self.root/'.env').chmod(0o644)
        lab.initialize();self.assertEqual((self.root/'.env').stat().st_mode & 0o777,0o600)


class RegressionControllerTests(unittest.TestCase):
    def setUp(self):
        self.obj=object.__new__(lab.Lab)
        self.obj.project='mariadb-ha';self.obj.engine='podman';self.obj.compose_base=['podman-compose']
    def test_compose_run_unique_name(self):
        with patch.object(self.obj,'run',return_value=done()) as run:
            self.obj.comp('run','--rm','--no-deps','galera1','/opt/lab/state.py','inspect')
            a=run.call_args.args[0]
            self.obj.comp('run','--rm','--no-deps','galera1','/opt/lab/state.py','inspect')
            b=run.call_args.args[0]
        self.assertIn('--name',a)
        self.assertNotEqual(a[a.index('--name')+1],b[b.index('--name')+1])
    def test_compose_run_no_tty(self):
        with patch.object(self.obj,'run',return_value=done()) as run:
            self.obj.comp('run','--rm','tools','check')
        self.assertIn('-T',run.call_args.args[0])
    def test_compose_respects_explicit_oneoff_name(self):
        with patch.object(self.obj,'run',return_value=done()) as run:
            self.obj.comp('run','--rm','--name','specified-test-name','tools','check')
        args=run.call_args.args[0]
        self.assertEqual(args.count('--name'),1)
        self.assertEqual(args[args.index('--name')+1],'specified-test-name')
    def test_no_oneoff_flags_on_up(self):
        with patch.object(self.obj,'run',return_value=done()) as run:self.obj.comp('up','-d','galera1')
        self.assertNotIn('--name',run.call_args.args[0]);self.assertNotIn('-T',run.call_args.args[0])
    def test_engine_failure_is_not_absent(self):
        with patch.object(self.obj,'run',return_value=done(125,stderr='Cannot connect to Podman socket: permission denied')):
            with self.assertRaises(lab.LabError):self.obj.state('galera1')
    def test_real_missing_object_is_absent(self):
        with patch.object(self.obj,'run',return_value=done(1,stderr='Error: no such object: mariadb-ha-galera1')):
            self.assertEqual(self.obj.state('galera1'),'absent')
    def test_stopped_container_recreated_without_deleting_volume(self):
        with patch.object(self.obj,'state',return_value='stopped'),patch.object(self.obj,'comp') as comp:
            self.obj.start_service('galera1')
        args=comp.call_args.args
        self.assertIn('--force-recreate',args);self.assertNotIn('-v',args)
    def test_running_node_never_recreated(self):
        with patch.object(self.obj,'state',return_value='running'),patch.object(self.obj,'comp') as comp:
            self.obj.start_service('galera1')
        comp.assert_not_called()
    def test_paused_node_start_refused(self):
        with patch.object(self.obj,'state',return_value='paused'),patch.object(self.obj,'comp') as comp:
            with self.assertRaises(lab.LabError):self.obj.start_service('galera1')
        comp.assert_not_called()
    def test_seed_marker_precedes_schema_replace(self):
        commands=[]
        def sql(node,query,**kwargs):
            commands.append(query)
            if 'information_schema.SCHEMATA' in query:return done(stdout='1')
            if 'DROP DATABASE' in query:raise lab.LabError('injected DDL failure')
            return done()
        with patch.object(self.obj,'healthy_node',return_value='galera1'),patch.object(self.obj,'health',return_value={'ready':True}),patch.object(self.obj,'sql',side_effect=sql):
            with self.assertRaises(lab.LabError):self.obj.seed('tiny',replace=True)
        marker=next(i for i,q in enumerate(commands) if "'loading'" in q)
        ddl=next(i for i,q in enumerate(commands) if 'DROP DATABASE' in q)
        self.assertLess(marker,ddl)
    def test_unknown_mode_is_not_ready(self):
        self.assertFalse(health.is_ready({'sql_alive':True},'galerra'))
    def test_standalone_ready_allowed(self):
        self.assertTrue(health.is_ready({'sql_alive':True},'standalone'))
    def test_offline_runs_real_subprocess_with_explicit_runtime_double(self):
        # Only the provider is simulated. Lab.offline -> Lab.comp -> subprocess.run is real.
        with tempfile.TemporaryDirectory(prefix='compose process test ') as directory:
            fake=Path(directory)/'explicit_fake_compose.py'
            fake.write_text('''import json,sys
args=sys.argv[1:]
# Model the upstream container_name collision and TTY corruption conditions.
if '--name' not in args:
 print('EXPLICIT TEST DOUBLE: container name mariadb-ha-galera1 already in use',file=sys.stderr);sys.exit(125)
if '-T' not in args:
 print('EXPLICIT TEST DOUBLE: TTY must be disabled',file=sys.stderr);sys.exit(126)
print('provider setup log before JSON')
print(json.dumps(dict(initialized=True,init_complete=True,uuid='test',seqno=5,safe_to_bootstrap=1)))
''')
            self.obj.compose_base=[sys.executable,str(fake)];self.obj.env=dict(os.environ)
            self.obj.settings={key:'testPassword123456' for key in lab.PASSWORDS}
            with patch.object(self.obj,'state',return_value='stopped'):
                result=self.obj.offline('galera1')
            self.assertEqual(result['seqno'],5)


class RegressionBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for dirname in ('images/node','images/proxy','scripts','datasets','.state'):(self.root/dirname).mkdir(parents=True,exist_ok=True)
        (self.root/'scripts/sample.py').write_text('first revision')
        self.root_patch=patch.object(lab,'ROOT',self.root);self.root_patch.start()
        self.obj=object.__new__(lab.Lab);self.obj.settings={};self.obj.engine='podman'
        self.obj.node_image='node';self.obj.proxy_image='proxy'
    def tearDown(self):self.root_patch.stop();self.tmp.cleanup()
    def test_changed_source_rebuilds_existing_tag(self):
        with patch.object(self.obj,'run',return_value=done(stdout='[{"Id":"sha256:test"}]')),patch.object(self.obj,'state',return_value='stopped'),patch.object(self.obj,'comp') as comp:
            self.obj.build();comp.reset_mock()
            self.obj.build();comp.assert_not_called()
            (self.root/'scripts/sample.py').write_text('fixed revision')
            self.obj.build();comp.assert_called_once_with('build','galera1')
    def test_stale_running_nodes_require_safe_shutdown(self):
        with patch.object(self.obj,'run',return_value=done(stdout='[{"Id":"sha256:test"}]')),patch.object(self.obj,'state',return_value='running'),patch.object(self.obj,'comp') as comp:
            with self.assertRaises(lab.LabError):self.obj.build()
        comp.assert_not_called()
    def test_explicit_build_allowed_without_automatic_restart(self):
        with patch.object(self.obj,'run',return_value=done(stdout='[{"Id":"sha256:test"}]')),patch.object(self.obj,'state',return_value='running'),patch.object(self.obj,'comp') as comp:
            self.obj.build(force=True)
        self.assertEqual([c.args[0] for c in comp.call_args_list],['build','build'])
    def test_pycache_does_not_force_rebuild(self):
        before=self.obj.build_fingerprint('galera1')
        (self.root/'scripts/__pycache__').mkdir();(self.root/'scripts/__pycache__/test.pyc').write_bytes(b'abc')
        self.assertEqual(before,self.obj.build_fingerprint('galera1'))


class RegressionWorkloadTests(unittest.TestCase):
    def test_failed_sql_probe_closes_connection(self):
        a,b=MagicMock(),MagicMock()
        a.cursor.return_value.__enter__.return_value.execute.side_effect=stub.MySQLError(2013,'fake failure')
        with patch.object(workload,'connect',side_effect=[a,b]),patch.object(workload.time,'sleep'):
            self.assertIs(workload.checked_connect(),b)
        a.close.assert_called_once()
    def run_conflict(self, reject, delta):
        a,b=MagicMock(),MagicMock()
        if reject:b.commit.side_effect=stub.MySQLError(reject,'explicit test error')
        def query(conn,sql,*args):
            if sql.startswith('SELECT value'):
                query.count+=1;return [(10 if query.count==1 else 10+delta,)]
            return []
        query.count=0
        with patch.object(workload,'checked_connect',side_effect=[a,b]),patch.object(workload,'query',side_effect=query),contextlib.redirect_stdout(io.StringIO()):
            workload.conflict()
        a.close.assert_called_once();b.close.assert_called_once()
    def test_conflict_rejects_both_committed(self):
        with self.assertRaises(RuntimeError):self.run_conflict(None,2)
    def test_conflict_rejects_wrong_sql_error(self):
        with self.assertRaises(RuntimeError):self.run_conflict(1045,1)
    def test_conflict_rejects_wrong_counter_delta(self):
        with self.assertRaises(RuntimeError):self.run_conflict(1213,0)
    def test_conflict_accepts_exact_evidence(self):self.run_conflict(1213,1)
    def test_second_connection_failure_closes_first(self):
        a=MagicMock()
        with patch.object(workload,'checked_connect',side_effect=[a,stub.MySQLError(2003,'offline')]),self.assertRaises(stub.MySQLError):workload.conflict()
        a.close.assert_called_once()


class RegressionBashProcessTests(unittest.TestCase):
    """Execute the Bash entrypoint. Absolute paths are sandbox-mapped; database commands are stubs."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='bash entrypoint regression ')
        # Bash contains unquoted fixed production paths, so use an internal no-space symlink.
        self.link=Path('/tmp')/('maria-shell-'+os.urandom(8).hex());self.link.symlink_to(self.tmp.name)
        self.root=self.link
        for part in ('bin','var/lib/mysql','var/lib/labctl','run/mysqld','opt/lab','usr/local/bin'):(self.root/part).mkdir(parents=True,exist_ok=True)
        def executable(path,text):
            path.write_text(text);path.chmod(0o755)
        executable(self.root/'bin/chown','#!/bin/sh\nexit 0\n')
        executable(self.root/'bin/mariadb','#!/bin/sh\n[ "${LAB_TEST_INIT_MODE:-success}" = success ]\n')
        executable(self.root/'bin/mariadb-admin','#!/usr/bin/env bash\nkill -TERM "$(cat "$LAB_TEST_ROOT/run/mysqld/lab-init.pid")"\n')
        executable(self.root/'usr/local/bin/docker-entrypoint.sh','''#!/usr/bin/env bash
set -eu
if [[ ${LAB_TEST_INIT_MODE:-success} == fail ]];then exit 23;fi
mkdir -p "$LAB_TEST_ROOT/var/lib/mysql/mysql"
trap 'exit 0' TERM INT
echo $$ > "$LAB_TEST_ROOT/run/mysqld/lab-init.pid"
while :;do sleep 0.05;done
''')
        executable(self.root/'usr/local/bin/gosu','''#!/usr/bin/env python3
import json,os,sys
open(os.environ['LAB_TEST_ROOT']+'/gosu-args.json','w').write(json.dumps(sys.argv[1:]))
''')
        (self.root/'opt/lab/health.py').write_text('# explicit health-server test double\n')
        text=(ROOT/'images/node/entrypoint.sh').read_text()
        for absolute in ('/var/lib/mysql','/var/lib/labctl','/run/mysqld','/opt/lab','/usr/local/bin'):
            text=text.replace(absolute,str(self.root)+absolute)
        self.script=self.root/'entrypoint-under-test.sh';self.script.write_text(text)
        self.env=dict(os.environ,PATH=str(self.root/'bin')+':'+os.environ['PATH'],LAB_TEST_ROOT=str(self.root),
                      LAB_MODE='galera',SST_PASSWORD='testPassword123456',MARIADB_ROOT_PASSWORD='testPassword123456')
    def tearDown(self):self.link.unlink(missing_ok=True);self.tmp.cleanup()
    def run_script(self,*args,**env):
        # Leave headroom for loaded CI hosts; kill only our process group on any exit.
        process=subprocess.Popen(['bash',str(self.script),*args],env=dict(self.env,**env),
                                 text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            out,err=process.communicate(timeout=20)
            return subprocess.CompletedProcess(process.args,process.returncode,out,err)
        finally:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            if process.poll() is None:process.wait()
    def initialized(self):
        (self.root/'var/lib/mysql/mysql').mkdir();(self.root/'var/lib/labctl/init-complete').touch()
    def test_unknown_mode_exits_64(self):
        self.initialized();result=self.run_script('mariadbd',LAB_MODE='galerra')
        self.assertEqual(result.returncode,64,result.stderr)
        self.assertFalse((self.root/'gosu-args.json').exists())
    def test_additional_daemon_args_are_not_silently_ignored(self):
        self.initialized();result=self.run_script('mariadbd','--port=3309')
        self.assertEqual(result.returncode,64,result.stderr)
    def test_non_daemon_command_passes_through(self):
        result=self.run_script('printf','test-command')
        self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,'test-command')
    def test_partial_init_refused(self):
        (self.root/'var/lib/mysql/mysql').mkdir();result=self.run_script('mariadbd')
        self.assertNotEqual(result.returncode,0);self.assertIn('partial init',result.stderr)
    def test_successful_stub_init_creates_marker_and_consumes_token(self):
        (self.root/'var/lib/labctl/bootstrap-once').write_text('authorize-new\n')
        result=self.run_script('mariadbd')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.root/'var/lib/labctl/init-complete').exists())
        self.assertFalse((self.root/'var/lib/labctl/bootstrap-once').exists())
        args=json.loads((self.root/'gosu-args.json').read_text());self.assertIn('--wsrep-new-cluster',args)
    def test_failed_stub_init_never_sets_marker(self):
        result=self.run_script('mariadbd',LAB_TEST_INIT_MODE='fail')
        self.assertNotEqual(result.returncode,0);self.assertFalse((self.root/'var/lib/labctl/init-complete').exists())
        self.assertFalse((self.root/'gosu-args.json').exists())
    def test_signal_during_init_exits_143_and_no_final_daemon(self):
        process=subprocess.Popen(['bash',str(self.script),'mariadbd'],env=dict(self.env,LAB_TEST_INIT_MODE='hang'),
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        try:
            deadline=time.monotonic()+5
            while not (self.root/'run/mysqld/lab-init.pid').exists() and time.monotonic()<deadline:time.sleep(0.02)
            self.assertTrue((self.root/'run/mysqld/lab-init.pid').exists())
            process.send_signal(signal.SIGTERM);out,err=process.communicate(timeout=10)
            self.assertEqual(process.returncode,143,(out,err))
            self.assertFalse((self.root/'var/lib/labctl/init-complete').exists())
            self.assertFalse((self.root/'gosu-args.json').exists())
        finally:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            if process.poll() is None:process.wait()
    def test_standalone_rejects_bootstrap_token(self):
        self.initialized();(self.root/'var/lib/labctl/bootstrap-once').write_text('test')
        result=self.run_script('mariadbd',LAB_MODE='standalone')
        self.assertNotEqual(result.returncode,0);self.assertFalse((self.root/'gosu-args.json').exists())

class AcceptanceGuardProcessTests(unittest.TestCase):
    def invoke(self, approved):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tests').mkdir()
            script=root/'tests/real_acceptance.py'
            script.write_bytes((ROOT/'tests/real_acceptance.py').read_bytes())
            env=dict(os.environ,PATH=directory,RUN_DISRUPTIVE_TESTS='1' if approved else '0')
            result=subprocess.run([sys.executable,str(script)],env=env,capture_output=True,text=True,timeout=5)
            report=json.loads((root/'reports/real-acceptance/latest.json').read_text())
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertEqual(report['status'],'BLOCKED')
            self.assertFalse(report['real_database_tests_executed'])
            self.assertEqual(report['steps'],[])
            self.assertFalse((root/'.env').exists())
            return report
    def test_runtime_missing_is_blocked_not_pass(self):
        report=self.invoke(True)
        self.assertIn('Neither Podman nor Docker',report['reason'])
    def test_disruptive_approval_missing_does_not_execute(self):
        report=self.invoke(False)
        self.assertIn('Explicit approval required',report['reason'])

if __name__=='__main__':unittest.main(verbosity=2)
