"""Operations extension unit tests. No actual Redis is used here."""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from ops.resp import encode,read_reply,ServerError,ProtocolError,parse_info,pairs,endpoints
from ops.common import Histogram,Guard,Report,Blocked,cleanup,owned_prefix,recover_journal,atomic_json,render
from ops.load import validate,Stats,save_benchmark
from ops.compare import compare_reports
from ops.proxy import Rules
from ops.cli import parser as runner_parser
from ops_manage import Ops,parser as host_parser
import ops_manage
import manage

class RespCodecTests(unittest.TestCase):
    def test_encode_binary(self):self.assertEqual(encode(('SET','k',b'\0\xff')),b'*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$2\r\n\0\xff\r\n')
    def test_host_runner_passes_benchmark_environment_without_shell(self):
        ops=Ops(manage.parse_env(ROOT/'.env.example'))
        with patch.object(ops,'inspect'),patch.object(ops.base,'run') as run:
            ops.runner('load',child_env={'DB_LAB_BENCHMARK_ENV':'{"container_images":{}}'},capture=True)
        command=run.call_args.args[0]
        self.assertIn('--env',command)
        self.assertIn('DB_LAB_BENCHMARK_ENV={"container_images":{}}',command)
        self.assertEqual(command[-4:],['python','-m','ops.cli','load'])

    def test_host_environment_inspects_owned_redis_and_runner_containers(self):
        ops=Ops(manage.parse_env(ROOT/'.env.example'))
        ops.base.engine='podman'
        with patch.object(ops.base,'run',return_value=Mock(stdout='podman 5.4')) as version, \
             patch.object(ops_manage,'host_benchmark_environment',return_value={'container_images':{'rslab-redis-1':{}}}) as inspect:
            result=ops.benchmark_environment()
        self.assertEqual(result['container_engine_version'],'podman 5.4')
        version.assert_called_once()
        inspect.assert_called_once_with('podman',[
            'rslab-redis-1','rslab-redis-2','rslab-redis-3','rslab-redis-perf','rslab-ops-runner'])
    def test_runtime_observation_is_encoded_as_data_for_runner(self):
        ops=Ops(manage.parse_env(ROOT/'.env.example'))
        observation={'scope':'host','sample_count':4,'host_load_1m_peak':1.2}
        with patch.object(ops,'execute') as execute:
            ops.attach_runtime_observation(observation)
        argv=execute.call_args.args[1]
        self.assertEqual(json.loads(argv[-1]),observation)
        self.assertIn('benchmark.json',argv[2])
    def test_empty_command(self):
        with self.assertRaises(ValueError):encode(())
    def test_none_argument(self):
        with self.assertRaises(TypeError):encode(('SET','k',None))
    def test_simple(self):self.assertEqual(read_reply(io.BytesIO(b'+PONG\r\n')),b'PONG')
    def test_integer(self):self.assertEqual(read_reply(io.BytesIO(b':-3\r\n')),-3)
    def test_bulk_binary(self):self.assertEqual(read_reply(io.BytesIO(b'$3\r\n\0\xffx\r\n')),b'\0\xffx')
    def test_nil_bulk(self):self.assertIsNone(read_reply(io.BytesIO(b'$-1\r\n')))
    def test_nil_array(self):self.assertIsNone(read_reply(io.BytesIO(b'*-1\r\n')))
    def test_nested_array(self):self.assertEqual(read_reply(io.BytesIO(b'*2\r\n:1\r\n*1\r\n+OK\r\n')),[1,[b'OK']])
    def test_error_is_value_for_pipeline_drain(self):self.assertIsInstance(read_reply(io.BytesIO(b'-OOM no memory\r\n')),ServerError)
    def test_invalid_framing(self):
        with self.assertRaises(ProtocolError):read_reply(io.BytesIO(b'$3\r\nab'))
    def test_huge_reply(self):
        with self.assertRaises(ProtocolError):read_reply(io.BytesIO(b'$999999999\r\n'))
    def test_invalid_type(self):
        with self.assertRaises(ProtocolError):read_reply(io.BytesIO(b'%0\r\n'))
    def test_closed_socket(self):
        with self.assertRaises(EOFError):read_reply(io.BytesIO(b''))
    def test_info_nested(self):
        d=parse_info('# Memory\r\nused_memory:42\r\nratio:1.5\r\nrole:master\r\ncmdstat_get:calls=10,usec=25,usec_per_call=2.5\r\n')
        self.assertEqual(d['used_memory'],42);self.assertEqual(d['cmdstat_get']['usec_per_call'],2.5)
    def test_pairs(self):self.assertEqual(pairs([b'k',b'v']),{'k':'v'})
    def test_pairs_odd(self):
        with self.assertRaises(ProtocolError):pairs([b'a'])
    def test_endpoints(self):self.assertEqual(endpoints('a:6379,b:26379'),[('a',6379),('b',26379)])

class HistAndValidationTests(unittest.TestCase):
    def test_empty_hist(self):self.assertIsNone(Histogram().summary()['p99_upper'])
    def test_quantile_bounds(self):
        h=Histogram()
        for i in range(1,101):h.add(i)
        p=h.percentile(.99);self.assertGreaterEqual(p,99);self.assertLess(p,103)
    def test_zero(self):
        h=Histogram();h.add(0);self.assertEqual(h.percentile(.5),0)
    def test_invalid_latency(self):
        for n in [-1,float('nan'),float('inf')]:
            with self.subTest(n=n),self.assertRaises(ValueError):Histogram().add(n)
    def test_load_memory_limit(self):
        with self.assertRaises(ValueError):validate(10,100,2,50000,8192)
    def test_load_rate_limit(self):
        with self.assertRaises(ValueError):validate(10,10001,2,100,128)
    def test_load_good(self):validate(30,300,8,2000,256)
    def test_shared_benchmark_report_excludes_secrets_and_observes_version(self):
        runtime={'host_fingerprint':'test-host','host_cpu_count':8,'host_memory_bytes':1000,
                 'container_limits':{'rslab-redis-1':{'memory_bytes':512,'cpu_cores':1.0}},
                 'container_images':{'rslab-redis-1':{'image_id':'sha256:redis','repo_digests':[]}},
                 'container_engine':'podman','container_engine_version':'5.4'}
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'OPS_RESULTS':temp,'REDIS_PASSWORD':'secret-value',
                'DB_LAB_BENCHMARK_ENV':json.dumps(runtime)}):
            report=Report('load',{'cmd':'load','target':'perf','rate':300,'workers':8,'keys':100})
            (report.path/'metrics.jsonl').write_text('{"info":{"redis_version":"7.4.1"}}\n')
            normalized=save_benchmark(report,{'target':'perf','wall_s':2.0,
                'counts':{'attempted':20,'success':19,'error':1},'rtt_ms':{'p50_upper':1.0,'p95_upper':2.0,'p99_upper':3.0},
                'achieved_success_rps':9.5,'offered_target_rps':10,'cleanup_keys':100},'PASS')
            saved=json.loads((report.path/'benchmark.json').read_text())
            self.assertEqual(saved['database']['version'],'7.4.1')
            self.assertEqual(saved['metrics']['requests_succeeded'],19)
            self.assertEqual(saved['verification']['owned_keys_cleanup_succeeded'],True)
            self.assertTrue(saved['verification']['dataset_state_verified'])
            self.assertEqual(saved['environment']['container_images'],runtime['container_images'])
            self.assertNotIn('secret-value',(report.path/'benchmark.json').read_text())
    def test_compare_requires_matching_parameters_and_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            paths=[]
            for i,rate in enumerate((100,100)):
                folder=Path(temp)/str(i);folder.mkdir();paths.append(folder/'report.json')
                data={'status':'PASS','scenario':'load','run_id':str(i),
                    'parameters':{'target':'perf','rate':rate},
                    'findings':{'load':{'counts':{'success':10+i,'error':0},
                        'achieved_success_rps':10+i,'rtt_ms':{'p95_upper':2+i}}}}
                paths[-1].write_text(json.dumps(data))
                (folder/'metrics.jsonl').write_text('{"info":{"redis_version":"7.4.1"}}\n')
            result=compare_reports(*paths)
            self.assertTrue(result['comparable']);self.assertEqual(result['candidate']['success'],11)
            self.assertEqual(result['candidate_minus_baseline']['success_rps']['absolute'],1)
            data=json.loads(paths[1].read_text());data['parameters']['rate']=200
            paths[1].write_text(json.dumps(data))
            self.assertFalse(compare_reports(*paths)['comparable'])
            data['parameters']['rate']=100;paths[1].write_text(json.dumps(data))
            (Path(temp)/'1'/'metrics.jsonl').unlink()
            self.assertFalse(compare_reports(*paths)['comparable'])
    def test_compare_rejects_failed_report(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.json';path.write_text(json.dumps({'status':'FAIL'}))
            with self.assertRaises(ValueError):compare_reports(path,path)
    def test_stats_uncertain_vs_rejected(self):
        s=Stats();s.result(False,'SET',1,2,'transport:TransportError');s.result(False,'SET',1,2,'server:OOM')
        self.assertEqual(s.snapshot()['counts']['writes_uncertain'],1);self.assertEqual(s.snapshot()['counts']['writes_rejected'],1)
    def test_write_not_sent_is_not_uncertain(self):
        s=Stats();s.result(False,'SET',1,2,'transport:TransportError',send_attempted=False)
        self.assertEqual(s.snapshot()['counts']['writes_not_sent'],1)
        self.assertEqual(s.snapshot()['counts'].get('writes_uncertain',0),0)
    def test_owned_prefix(self):self.assertEqual(owned_prefix('ops:abcdefgh:'),'ops:abcdefgh:')
    def test_foreign_prefix_rejected(self):
        for p in ('*','production:','ops:*:','ops:../secret:'):
            with self.subTest(p=p),self.assertRaises(ValueError):owned_prefix(p)
    def test_cleanup_never_flushes(self):
        c=Mock();c.execute.side_effect=[(0,[b'ops:abcdefgh:k']),1,(0,[])]
        self.assertEqual(cleanup(c,'ops:abcdefgh:'),1)
        self.assertTrue(all(call.args[0] in ('SCAN','UNLINK') for call in c.execute.call_args_list))
    def test_cleanup_rejects_foreign_key_even_from_server(self):
        c=Mock();c.execute.return_value=(0,[b'production:key'])
        with self.assertRaises(RuntimeError):cleanup(c,'ops:abcdefgh:')

class RulesTests(unittest.TestCase):
    def test_set_and_expire(self):
        r=Rules()
        with patch('ops.proxy.time.monotonic',return_value=1):r.set({'delay_ms':40,'ttl_s':2})
        with patch('ops.proxy.time.monotonic',return_value=4):self.assertEqual(r.get()['delay_ms'],0)
    def test_unknown_rule(self):
        with self.assertRaises(ValueError):Rules().set({'upstream':'prod'})
    def test_bounds(self):
        for obj in ({'delay_ms':1001},{'ttl_s':121},{'kib_per_s':-1},{'disconnect':'yes'},{'delay_ms':True}):
            with self.subTest(obj=obj),self.assertRaises(ValueError):Rules().set(obj)
    def test_rejected_update_preserves_old_rule(self):
        r=Rules();r.set({'delay_ms':40})
        with self.assertRaises(ValueError):r.set({'delay_ms':2000})
        self.assertEqual(r.get()['delay_ms'],40)

class FakeConfig:
    def __init__(self):self.settings={'maxclients':'512','activedefrag':'no'};self.calls=[];self.sid='server1';self.fail_restore=False
    def info(self,*args):return {'run_id':self.sid,'role':'master','connected_slaves':0,'used_memory':1000000}
    def config(self,*names):return {k:self.settings[k] for k in names if k in self.settings}
    def execute(self,*args):
        self.calls.append(args)
        if args[:2]==('CONFIG','SET'):
            d=dict(zip(args[2::2],map(str,args[3::2])))
            if self.fail_restore and d.get('maxclients')=='512':raise ServerError('restore failed')
            self.settings.update(d);return b'OK'
        if args[0]=='SCAN':return (0,[])
        if args[0]=='PING':return b'PONG'
        raise AssertionError(args)

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{'OPS_RESULTS':self.temp.name});self.env.start();self.addCleanup(self.env.stop)
        self.c=FakeConfig()
    def test_restore_on_exception(self):
        report=Report('example')
        with self.assertRaisesRegex(RuntimeError,'injected'):
            with Guard(self.c,report) as g:g.configure(maxclients=24);raise RuntimeError('injected')
        self.assertEqual(self.c.settings['maxclients'],'512');self.assertTrue(report.data['recovery']['ok'])
        self.assertFalse((Path(self.temp.name)/'recovery.json').exists())
    def test_journal_precedes_mutation(self):
        report=Report('example')
        with Guard(self.c,report) as g:
            original=self.c.execute
            def execute(*args):
                if args[:2]==('CONFIG','SET'):
                    data=json.loads((Path(self.temp.name)/'recovery.json').read_text());self.assertEqual(data['config']['maxclients'],'512')
                return original(*args)
            self.c.execute=execute;g.configure(maxclients=24)
    def test_restore_error_retains_journal(self):
        report=Report('example')
        with self.assertRaises(ServerError):
            with Guard(self.c,report) as g:g.configure(maxclients=24);self.c.fail_restore=True
        self.assertTrue((Path(self.temp.name)/'recovery.json').exists());self.assertFalse(report.data['recovery']['ok'])
    def test_recover_does_not_reapply_to_restarted_server(self):
        journal=Path(self.temp.name)/'recovery.json'
        atomic_json(journal,{'server_id':'old','prefix':'ops:abcdefgh:','config':{'maxclients':'999'}})
        result=recover_journal(self.c)
        self.assertTrue(result['server_restarted']);self.assertEqual(self.c.settings['maxclients'],'512')
    def test_new_scenario_rejects_stale_journal(self):
        (Path(self.temp.name)/'recovery.json').write_text('{}')
        with self.assertRaises(Blocked):
            with Guard(self.c,Report('example')):pass
    def test_scenario_lock_rejects_concurrency(self):
        with Guard(self.c,Report('first')):
            with self.assertRaises(Blocked):
                with Guard(self.c,Report('second')):pass
    def test_auth_config_refused(self):
        with Guard(self.c,Report('example')) as g:
            with self.assertRaises(ValueError):g.configure(requirepass='newpass')
    def test_guard_unknown_setting_blocked(self):
        with Guard(self.c,Report('example')) as g:
            with self.assertRaises(Blocked):g.configure(hz=50)
    def test_failed_report_is_not_previous_pass(self):
        first=Report('first');first.finish('PASS');second=Report('second');second.finish('FAIL')
        self.assertEqual(json.loads((Path(self.temp.name)/'latest.json').read_text())['status'],'FAIL')
    def test_new_run_replaces_latest_with_running(self):
        first=Report('first');first.finish('PASS');second=Report('second')
        latest=json.loads((Path(self.temp.name)/'latest.json').read_text())
        self.assertEqual(latest['status'],'RUNNING');self.assertEqual(latest['run_id'],second.id)
    def test_html_escapes_values(self):
        report=Report('example');report.data['error']='<script>alert(1)</script>';report.finish('FAIL')
        data=(report.path/'report.html').read_text();self.assertNotIn('<script>',data);self.assertIn('&lt;script&gt;',data)

class HostAndComposeTests(unittest.TestCase):
    def test_extra_container_allowlist(self):
        ops=Ops(manage.parse_env(ROOT/'.env.example'))
        with self.assertRaises(ValueError):ops.name_of('production')
    def test_extra_volume_allowlist(self):
        ops=Ops(manage.parse_env(ROOT/'.env.example'))
        with self.assertRaises(ValueError):ops.volume('other')
    def test_main_defaults(self):
        a=host_parser().parse_args(['run','fragmentation','--yes']);self.assertEqual(a.mib,64)
    def test_compare_cli_takes_two_report_paths(self):
        a=host_parser().parse_args(['compare','baseline/report.json','candidate/report.json'])
        self.assertEqual((str(a.baseline),str(a.candidate)),('baseline/report.json','candidate/report.json'))
    def test_runner_rejects_unbounded_time(self):
        with self.assertRaises(SystemExit):runner_parser().parse_args(['run','fragmentation','--seconds','999'])
    def test_no_host_ports_privileged_or_binds(self):
        import yaml
        d=yaml.safe_load((ROOT/'compose.ops.yaml').read_text())
        for service in d['services'].values():
            self.assertNotIn('ports',service);self.assertNotIn('privileged',service);self.assertNotIn('network_mode',service)
            for volume in service.get('volumes',[]):self.assertFalse(volume.startswith(('/','.')))
    def test_same_external_network(self):
        import yaml
        d=yaml.safe_load((ROOT/'compose.ops.yaml').read_text());self.assertTrue(d['networks']['labnet']['external'])
    def test_perf_has_memory_limit(self):
        import yaml
        d=yaml.safe_load((ROOT/'compose.ops.yaml').read_text());self.assertEqual(d['services']['redis-perf']['mem_limit'],'640m')
