"""All engine calls are mocked. These tests assert scope/rollback contracts, not Docker support."""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from tools import manage
from tools.sim_engine import DockerLab, BASE_SERVICES

RUN='sim-0123456789ab'
PIN={'fingerprint':'a'*64, 'engine':'docker', 'local_docker':True}


class EnginePinTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'.state').mkdir()
        source=Path(manage.__file__).resolve().parents[1]
        shutil.copy2(source/'.env.example',self.root/'.env.example')
        p=patch.object(manage,'ROOT',self.root);p.start();self.addCleanup(p.stop)
        manage.init();self.config=manage.validate(manage.parse_env(self.root/'.env'))

    def compose(self):
        with patch.object(manage,'provider',return_value=(['docker','compose'],['docker'])):
            return manage.Compose(self.config)

    def test_selector_change_rejected_before_mutation(self):
        with patch.dict(os.environ,{'DOCKER_CONTEXT':'lab-a'}):a=self.compose();a.guard_identity()
        with patch.dict(os.environ,{'DOCKER_CONTEXT':'lab-b'}):b=self.compose()
        with self.assertRaises(RuntimeError):b.guard_target()

    def test_same_context_changed_endpoint_or_daemon_is_blocked(self):
        c=self.compose()
        with patch.object(c,'engine_identity',return_value=PIN):c.guard_engine(bind=True)
        before=(self.root/'.state/engine-target.json').read_bytes()
        with patch.object(c,'engine_identity',return_value={**PIN,'fingerprint':'b'*64}),self.assertRaises(RuntimeError):
            c.guard_engine()
        self.assertEqual((self.root/'.state/engine-target.json').read_bytes(),before)

    def test_missing_pin_is_not_silently_created_for_shutdown(self):
        c=self.compose()
        with patch.object(c,'engine_identity',return_value=PIN),self.assertRaises(RuntimeError):c.guard_engine()
        self.assertFalse((self.root/'.state/engine-target.json').exists())

    def test_rebind_flag_never_overwrites_an_existing_pin(self):
        c=self.compose()
        with patch.object(c,'engine_identity',return_value=PIN):c.guard_engine(bind=True)
        with patch.object(c,'engine_identity',return_value={**PIN,'fingerprint':'b'*64}),self.assertRaises(RuntimeError):
            c.guard_engine(bind=True)

    def test_pin_does_not_store_endpoint_or_password(self):
        c=self.compose()
        with patch.object(c,'engine_identity',return_value=PIN):c.guard_engine(bind=True)
        content=(self.root/'.state/engine-target.json').read_text()
        self.assertNotIn(self.config['SQL_PASSWORD'],content)
        self.assertNotIn('ssh://',content)

    def test_docker_identity_resolves_actual_context_endpoint(self):
        c=self.compose();c.inherited={}
        answers=[types.SimpleNamespace(stdout=json.dumps({'ID':'daemon-a'})),
                 types.SimpleNamespace(stdout=json.dumps([{'Endpoints':{'docker':{'Host':'unix:///var/run/docker.sock'}}}]))]
        with patch.object(manage.subprocess,'run',side_effect=answers):identity=c.engine_identity()
        self.assertTrue(identity['local_docker']);self.assertEqual(len(identity['fingerprint']),64)

    def test_remote_context_is_not_local_even_if_called_default(self):
        c=self.compose();c.inherited={'DOCKER_CONTEXT':'default'}
        answers=[types.SimpleNamespace(stdout=json.dumps({'ID':'daemon-a'})),
                 types.SimpleNamespace(stdout=json.dumps([{'Endpoints':{'docker':{'Host':'ssh://private-host'}}}]))]
        with patch.object(manage.subprocess,'run',side_effect=answers):identity=c.engine_identity()
        self.assertFalse(identity['local_docker']);self.assertNotIn('private-host',json.dumps(identity))

    def test_missing_daemon_id_fails_closed(self):
        c=self.compose();c.inherited={'DOCKER_HOST':'unix:///socket'}
        with patch.object(manage.subprocess,'run',return_value=types.SimpleNamespace(stdout='{}')),self.assertRaises(RuntimeError):
            c.engine_identity()

    def test_invalid_migration_credentials_do_not_pin_target(self):
        c=self.compose(); c.guard_identity()
        saved=json.loads((self.root/'.state/identity.json').read_text())
        saved['SQL_PASSWORD']='invalid'
        (self.root/'.state/identity.json').write_text(json.dumps(saved))
        with patch.object(manage,'Compose',return_value=c),patch.object(c,'engine_identity',return_value=PIN):
            self.assertEqual(manage.main(['bind-target','--yes']),1)
        self.assertFalse((self.root/'.state/engine-target.json').exists())

    def test_ready_info_uses_captured_environment(self):
        with patch.dict(os.environ,{'DOCKER_CONTEXT':'lab-a'}):c=self.compose()
        with patch.dict(os.environ,{'DOCKER_CONTEXT':'lab-b'}),patch.object(manage.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)) as run:
            c.ready()
        self.assertTrue(all(x.kwargs['env']['DOCKER_CONTEXT']=='lab-a' for x in run.call_args_list))

    def test_compose_calls_keep_captured_engine_environment(self):
        with patch.dict(os.environ,{'DOCKER_CONTEXT':'lab-a'}):c=self.compose()
        with patch.dict(os.environ,{'DOCKER_CONTEXT':'lab-b'}),patch.object(manage.subprocess,'run') as run:
            c.run('ps')
        self.assertEqual(run.call_args.kwargs['env']['DOCKER_CONTEXT'],'lab-a')


class FaultSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'.state').mkdir()
        self.c=Mock();self.c.config={'MVP_PROJECT':'db-lab-mvp','API_PORT':'18090'}
        self.c.engine=['docker'];self.c.inherited={};self.c.guard_engine.return_value=PIN
        self.m=types.SimpleNamespace(ROOT=self.root,atomic_json=manage.atomic_json,mode=Mock(return_value='normal'))
        self.lab=DockerLab(self.c,self.m)
        self.state={name:{'id':format(i+1,'064x'),'service':name,'running':True,'paused':False,
                  'health':'healthy','image_id':'sha256:'+format(i+1,'064x'),'image_reference':'test:only',
                  'volumes':{'/data':name+'-volume'},'published_ports':{'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18090'}]} }
                    for i,name in enumerate(BASE_SERVICES)}
        self.lab.service=Mock(side_effect=lambda name,required=True:copy.deepcopy(self.state.get(name)))
        def inspect(cid,service):
            if self.state[service]['id']!=cid:raise RuntimeError('identity changed')
            return copy.deepcopy(self.state[service])
        self.lab.inspect=Mock(side_effect=inspect)
        def docker(*args,**kwargs):
            cid=args[-1];name=next(n for n,v in self.state.items() if v['id']==cid)
            if args[0]=='pause':self.state[name]['paused']=True
            elif args[0]=='unpause':self.state[name]['paused']=False
            elif args[0]=='stop':self.state[name]['running']=False
            elif args[0]=='start':self.state[name]['running']=True
            else:raise AssertionError(args)
            return ''
        self.lab.docker=Mock(side_effect=docker)

    def test_preflight_requires_all_existing_ready_services(self):
        self.assertEqual(len(self.lab.preflight()),6)
        self.state['mariadb']['health']='unhealthy'
        with self.assertRaises(RuntimeError):self.lab.preflight()
        self.lab.docker.assert_not_called()

    def test_wrong_api_port_refuses_any_fault(self):
        self.state['api']['published_ports']['8080/tcp'][0]['HostPort']='18091'
        with self.assertRaises(RuntimeError):self.lab.preflight()

    def test_remote_automation_is_refused(self):
        self.c.guard_engine.return_value={**PIN,'local_docker':False}
        with self.assertRaises(RuntimeError):self.lab.preflight()
        self.lab.docker.assert_not_called()

    def test_existing_fault_marker_blocks_new_run(self):
        self.lab.active_path.write_text('{}')
        with self.assertRaises(RuntimeError):self.lab.preflight()

    def test_non_normal_mode_blocks_new_run(self):
        self.m.mode.return_value='fresh-search'
        with self.assertRaises(RuntimeError):self.lab.preflight()

    def test_pause_restores_same_id_not_recreated_container(self):
        original=self.state['mariadb']['id']
        self.lab.apply('pause','mariadb',RUN,2)
        self.assertTrue(self.state['mariadb']['paused']);self.assertTrue(self.lab.active_path.exists())
        self.lab.restore()
        self.assertFalse(self.state['mariadb']['paused']);self.assertEqual(self.state['mariadb']['id'],original)
        self.assertFalse(self.lab.active_path.exists())
        self.assertEqual([c.args[0] for c in self.lab.docker.call_args_list],['pause','unpause'])

    def test_stop_starts_same_container_and_keeps_volume(self):
        before=copy.deepcopy(self.state['kafka'])
        self.lab.apply('stop','kafka',RUN,2);self.assertFalse(self.state['kafka']['running'])
        self.lab.restore()
        self.assertEqual(self.state['kafka'],before)
        self.assertEqual([c.args[0] for c in self.lab.docker.call_args_list],['stop','start'])

    def test_invalid_fault_scope_never_calls_docker(self):
        for action,service in [('stop','api'),('pause','redis'),('delete','mariadb')]:
            with self.assertRaises(ValueError):self.lab.apply(action,service,RUN,2)
        self.lab.docker.assert_not_called();self.assertFalse(self.lab.active_path.exists())

    def test_invalid_run_identifier_cannot_create_recovery_state(self):
        with self.assertRaises(ValueError):self.lab.apply('pause','mariadb','other-run',2)
        self.lab.docker.assert_not_called(); self.assertFalse(self.lab.active_path.exists())

    def test_invalid_order_identifier_is_rejected_before_row_lock_intent(self):
        with self.assertRaises(ValueError):self.lab.apply('row-lock','api',RUN,2,'-'*36)
        self.lab.docker.assert_not_called(); self.assertFalse(self.lab.active_path.exists())

    def test_preexisting_pause_is_not_claimed_as_own_fault(self):
        self.state['mariadb']['paused']=True
        with self.assertRaises(RuntimeError):self.lab.apply('pause','mariadb',RUN,2)
        self.lab.docker.assert_not_called()

    def test_intent_is_saved_before_partial_mutation_failure(self):
        original=self.lab.docker.side_effect
        def fail(*args,**kwargs):
            self.assertTrue(self.lab.active_path.exists())
            original(*args,**kwargs)
            raise RuntimeError('CLI disconnected after applying pause')
        self.lab.docker.side_effect=fail
        with self.assertRaises(RuntimeError):self.lab.apply('pause','mariadb',RUN,2)
        self.assertTrue(self.state['mariadb']['paused'])
        self.lab.docker.side_effect=original;self.lab.restore()
        self.assertFalse(self.state['mariadb']['paused'])

    def test_target_change_during_restore_preserves_marker(self):
        self.lab.apply('stop','kafka',RUN,2)
        self.c.guard_engine.return_value={**PIN,'fingerprint':'b'*64}
        with self.assertRaises(RuntimeError):self.lab.restore()
        self.assertTrue(self.lab.active_path.exists());self.assertFalse(self.state['kafka']['running'])

    def test_external_container_replacement_is_not_started(self):
        self.lab.apply('stop','kafka',RUN,2)
        self.state['kafka']['id']='f'*64
        with self.assertRaises(RuntimeError):self.lab.restore()
        self.assertTrue(self.lab.active_path.exists())
        self.assertEqual(self.lab.docker.call_count,1)

    def test_unsupported_recovery_payload_not_executed(self):
        self.lab.active_path.write_text(json.dumps({'action':'shell','service':'api','command':'anything'}))
        with self.assertRaises(RuntimeError):self.lab.restore()
        self.lab.docker.assert_not_called()

    def test_changed_image_or_volume_is_not_safe_recreation(self):
        original=self.state['redis']
        for changed in ({**original,'image_id':'new'}, {**original,'volumes':{'/data':'other'}}):
            with self.assertRaises(RuntimeError):self.lab._same_storage_image(original,changed)

    def test_recover_with_no_marker_is_noop(self):
        self.assertEqual(self.lab.restore(),{'restored':True,'action':'none'})
        self.lab.docker.assert_not_called()

    def test_recovery_failure_keeps_marker_and_never_deletes_volumes(self):
        self.lab.apply('stop','kafka',RUN,2)
        self.lab.docker.side_effect=RuntimeError('start failed')
        with self.assertRaises(RuntimeError):self.lab.restore()
        self.assertTrue(self.lab.active_path.exists())
        self.assertFalse(any(c.args[0] in {'rm','prune','volume'} for c in self.lab.docker.call_args_list))

    def test_external_stop_of_paused_container_not_automatically_restarted(self):
        self.lab.apply('pause','worker',RUN,2)
        self.state['worker']['running']=False;self.state['worker']['paused']=False
        with self.assertRaises(RuntimeError):self.lab.restore()
        self.assertFalse(any(c.args[0]=='start' for c in self.lab.docker.call_args_list))
