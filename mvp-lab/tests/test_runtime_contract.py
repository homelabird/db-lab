"""Read-only protocol/source/metadata contracts with doubles; not live engine evidence."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from mvp_app import runtime_contract as rc
from mvp_app.adapters import Settings


def source(root):
    (root/'mvp_app').mkdir()
    (root/'mvp_app/runtime_contract.py').write_text('# fixture source\n')
    (root/'mvp_app/index.html').write_text('<p>fixture</p>')
    (root/'Containerfile').write_text('FROM test-only\n')
    (root/'requirements.txt').write_text('PyMySQL==1.1.2\nconfluent-kafka==2.8.2\nredis==5.2.1\nrequests==2.32.5\n')

class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);source(self.root)

    def test_same_files_produce_same_fingerprint(self):
        self.assertEqual(rc.source_manifest(self.root),rc.source_manifest(self.root))

    def test_sealed_image_matches_host_source(self):
        value=rc.seal(self.root)
        self.assertTrue(rc.check_source(self.root,value['sha256'])['build_manifest_matches'])

    def test_no_build_manifest_is_not_fresh(self):
        with self.assertRaisesRegex(rc.ContractError,'missing_rebuild'):rc.check_source(self.root,'a'*64)

    def test_source_edit_since_build_is_detected(self):
        value=rc.seal(self.root);(self.root/'mvp_app/runtime_contract.py').write_text('changed')
        with self.assertRaisesRegex(rc.ContractError,'differ_from_build'):rc.check_source(self.root,value['sha256'])

    def test_old_but_self_consistent_image_is_rejected(self):
        rc.seal(self.root)
        with self.assertRaisesRegex(rc.ContractError,'differs_from_host'):rc.check_source(self.root,'f'*64)

    def test_ui_is_part_of_source_contract(self):
        before=rc.source_manifest(self.root)
        (self.root/'mvp_app/index.html').write_text('another UI')
        self.assertNotEqual(before['sha256'],rc.source_manifest(self.root)['sha256'])

    def test_dependency_file_is_part_of_source_contract(self):
        before=rc.source_manifest(self.root);(self.root/'requirements.txt').write_text('changed')
        self.assertNotEqual(before['sha256'],rc.source_manifest(self.root)['sha256'])

    def test_containerfile_is_part_of_source_contract(self):
        before=rc.source_manifest(self.root);(self.root/'Containerfile').write_text('changed')
        self.assertNotEqual(before['sha256'],rc.source_manifest(self.root)['sha256'])

    def test_pycache_and_runtime_state_do_not_change_source_hash(self):
        before=rc.source_manifest(self.root)
        (self.root/'mvp_app/__pycache__').mkdir();(self.root/'mvp_app/__pycache__/x.pyc').write_bytes(b'x')
        (self.root/'.env').write_text('SECRET=not-read')
        self.assertEqual(before,rc.source_manifest(self.root))

    def test_symlinked_source_refused(self):
        p=self.root/'mvp_app/index.html';p.unlink();p.symlink_to(self.root/'requirements.txt')
        with self.assertRaises(rc.ContractError):rc.source_manifest(self.root)

    def test_missing_source_fails_without_generated_manifest(self):
        (self.root/'mvp_app/runtime_contract.py').unlink()
        with self.assertRaises(rc.ContractError):rc.seal(self.root)
        self.assertFalse((self.root/rc.BUILD_FILE).exists())

    def test_credentials_hashed_and_not_reported_as_plaintext(self):
        s=Settings(sql_password='SQL-SECRET',redis_password='REDIS-SECRET')
        result=rc.target_manifest(s,'db-lab-mvp')
        self.assertNotIn('SECRET',json.dumps(result));self.assertEqual(len(result['sql_password']),64)

    def test_packages_use_distribution_versions(self):
        pins={'PyMySQL':'1.1.2','confluent-kafka':'2.8.2','redis':'5.2.1','requests':'2.32.5'}
        module=types.SimpleNamespace(TopicCollection=object,AdminClient=types.SimpleNamespace(describe_topics=lambda:None),libversion=lambda:('2.8.0',0))
        with patch.object(rc.metadata,'version',side_effect=pins.get),patch.object(rc.importlib,'import_module',return_value=module),patch.object(rc.sys,'version_info',(3,12,0)):
            self.assertEqual(rc.check_packages(self.root)['distributions'],pins)

    def test_mismatched_installed_pin_is_failure(self):
        with patch.object(rc.metadata,'version',return_value='0.0.0'),patch.object(rc.importlib,'import_module'):
            with self.assertRaisesRegex(rc.ContractError,'differs_from_pin'):rc.check_packages(self.root)

    def test_wildcard_dependency_not_silently_accepted(self):
        (self.root/'requirements.txt').write_text('redis>=5')
        with self.assertRaises(rc.ContractError):rc.check_packages(self.root)

class SQLReadOnlyTests(unittest.TestCase):
    def fixture(self):
        info={'db':'mvp','version':'11.4.5-MariaDB','datadir':'/var/lib/mysql/','isolation_level':'REPEATABLE-READ','lock_wait':2}
        tables=[{'name':t,'engine':'InnoDB'} for t in rc.TABLE_COLUMNS]
        columns=[{'table_name':t,'column_name':k,'data_type':v} for t, cols in rc.TABLE_COLUMNS.items() for k,v in cols.items()]
        indexes=[{'table_name':t,'index_name':key,'column_name':key,'non_unique':0,'seq_in_index':1}
                 for t,keys in {'orders':['id','idempotency_key'],'outbox':['seq','event_id']}.items() for key in keys]
        cur=Mock();cur.__enter__=Mock(return_value=cur);cur.__exit__=Mock(return_value=False)
        cur.fetchone.return_value=info;cur.fetchall.side_effect=[tables,columns,indexes]
        conn=Mock();conn.cursor.return_value=cur;repo=Mock();repo.connect.return_value=conn
        return repo,conn,cur,info,tables,columns,indexes

    def test_real_query_sequence_is_read_only_and_closes(self):
        repo,conn,cur,*_=self.fixture();self.assertTrue(rc.check_sql(repo)['unique_keys_checked'])
        queries=[c.args[0] for c in cur.execute.call_args_list]
        self.assertTrue(all(q.startswith(('START TRANSACTION READ ONLY','SELECT ')) for q in queries))
        conn.rollback.assert_called_once();conn.close.assert_called_once();conn.commit.assert_not_called()

    def test_missing_idempotency_constraint_fails(self):
        repo,conn,cur,info,tables,columns,indexes=self.fixture()
        indexes[:]=[i for i in indexes if i['column_name']!='idempotency_key']
        with self.assertRaisesRegex(rc.ContractError,'unique_key'):rc.check_sql(repo)
        conn.close.assert_called_once()

    def test_partial_schema_is_not_initialized(self):
        repo,conn,cur,info,tables,columns,indexes=self.fixture();columns.pop()
        with self.assertRaisesRegex(rc.ContractError,'column_contract'):rc.check_sql(repo)
        repo.initialize.assert_not_called();conn.rollback.assert_called_once()

    def test_extra_table_is_not_ignored(self):
        repo,conn,cur,info,tables,*_=self.fixture();tables.append({'name':'payments','engine':'InnoDB'})
        with self.assertRaisesRegex(rc.ContractError,'tables_or_engine'):rc.check_sql(repo)

    def test_misplaced_datadir_refused(self):
        repo,conn,cur,info,*_=self.fixture();info['datadir']='/wrong/path'
        with self.assertRaisesRegex(rc.ContractError,'data_directory'):rc.check_sql(repo)

    def test_wrong_engine_refused(self):
        repo,conn,cur,info,*_=self.fixture();info['version']='8.4.0-MySQL'
        with self.assertRaisesRegex(rc.ContractError,'database_or_engine'):rc.check_sql(repo)

    def test_query_failure_closes_connection_without_commit(self):
        repo,conn,cur,*_=self.fixture();cur.execute.side_effect=OSError('private connection info')
        with self.assertRaises(OSError):rc.check_sql(repo)
        conn.close.assert_called_once();conn.commit.assert_not_called()

class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);source(self.root);self.manifest=rc.seal(self.root)
        self.request={'schema':1,'service':'api','source_sha256':self.manifest['sha256'],'target':rc.target_manifest(Settings(),'db-lab-mvp')}
        p=patch.dict(os.environ,{'STUDY_PROJECT':'db-lab-mvp'},clear=True);p.start();self.addCleanup(p.stop)

    def test_missing_driver_skips_db_connections(self):
        with patch.object(rc,'check_packages',side_effect=ImportError('password=secret')),patch.object(rc,'check_sql') as sql:
            value=rc.run_probe(self.request,self.root)
        self.assertEqual(value['status'],'failed');sql.assert_not_called()
        self.assertNotIn('password=secret',json.dumps(value));self.assertEqual(value['checks'][-1]['status'],'not_run')

    def test_target_mismatch_blocks_all_database_probes(self):
        self.request['target']['sql_host']='different'
        with patch.object(rc,'check_packages',return_value={}),patch.object(rc,'check_sql') as sql:
            value=rc.run_probe(self.request,self.root)
        sql.assert_not_called();self.assertEqual(value['checks'][2]['code'],'effective_container_configuration_mismatch')

    def test_every_dependency_is_checked_independently(self):
        with patch.object(rc,'check_packages',return_value={}),patch.object(rc,'check_sql',side_effect=OSError('private')),patch.object(rc,'check_redis',return_value={}),patch.object(rc,'check_search',return_value={}),patch.object(rc,'check_kafka',return_value={}):
            value=rc.run_probe(self.request,self.root)
        self.assertEqual(len(value['checks']),7);self.assertEqual(value['checks'][-1]['status'],'passed');self.assertEqual(value['status'],'failed')

    def test_all_protocol_contracts_required_for_pass(self):
        with patch.object(rc,'check_packages',return_value={}),patch.object(rc,'check_sql',return_value={}),patch.object(rc,'check_redis',return_value={}),patch.object(rc,'check_search',return_value={}),patch.object(rc,'check_kafka',return_value={}):
            value=rc.run_probe(self.request,self.root)
        self.assertEqual(value['status'],'passed');self.assertIs(value['data_writes'],False)

    def test_bad_input_is_rejected(self):
        for request in ({}, {'schema':1}, {**self.request,'service':'mariadb'}, {**self.request,'extra':1}):
            with self.subTest(request=request),self.assertRaises(rc.ContractError):rc.run_probe(request,self.root)

    def test_redis_probe_cannot_fill_or_clear_cache(self):
        cache=Mock();cache.client.info.side_effect=[{'redis_version':'7.2.7'},{'role':'master'},{'aof_enabled':1}]
        rc.check_redis(cache);cache.put.assert_not_called();cache.delete.assert_not_called();cache.close.assert_called_once()

    def test_nonpersistent_redis_is_not_expected_baseline(self):
        cache=Mock();cache.client.info.side_effect=[{'redis_version':'7.2.7'},{'role':'master'},{'aof_enabled':0}]
        with self.assertRaises(rc.ContractError):rc.check_redis(cache)
        cache.close.assert_called_once()

    def test_search_probe_only_uses_get_and_closes(self):
        search=Mock();search.s.es_url='http://elasticsearch:9200';search.s.es_index='mvp-orders-v1'
        search.session.get.return_value.json.return_value={'version':{'number':'7.17.29'}}
        mapping=Mock();mapping.json.return_value={'mvp-orders-v1':{'mappings':{'dynamic':'strict','properties':{k:{'type':v} for k,v in rc.ES_FIELDS.items()}}}}
        settings=Mock();settings.json.return_value={'mvp-orders-v1':{'settings':{'index':{'uuid':'uuid','number_of_shards':'1','number_of_replicas':'0'}}}}
        search.request.side_effect=[mapping,settings];value=rc.check_search(search)
        self.assertTrue(value['mapping_checked']);self.assertTrue(all(c.args[0]=='GET' for c in search.request.call_args_list));search.close.assert_called_once()

class ImageBuildContractTests(unittest.TestCase):
    def test_manifest_inputs_are_in_the_actual_build_context(self):
        root=Path(__file__).resolve().parents[1]
        rules=(root/'.dockerignore').read_text().splitlines()
        self.assertEqual(rules[0],'*')
        for rule in ('!Containerfile','!requirements.txt','!mvp_app/','!mvp_app/**'):
            self.assertIn(rule,rules)
        text=(root/'Containerfile').read_text()
        self.assertIn('COPY Containerfile /app/Containerfile',text)
        self.assertLess(text.index('COPY mvp_app'),text.index('runtime_contract seal'))
        self.assertLess(text.index('runtime_contract seal'),text.index('USER 10001'))
