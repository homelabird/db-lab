"""Offline numeric/provenance/path controls, NOT live database experiments."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
import unittest
from tools.measurement import strict_json,digest
from tools.simulation import Plan
from tools.study_analysis import (compare,validate_run,load_run,render,aligned_windows,workload_metrics,PAIRED,EvidenceError, classify)
from study_fixtures import fixture,refresh,save

class StrictInputTests(unittest.TestCase):
    def test_empty_workload_cannot_pass(self):
        r=fixture();r.requests=[];r.timeline=[x for x in r.timeline if not x.get('stage','').startswith('workflow_')]
        r.summary.update(admitted_workflows=0,skipped_workflows=len(r.plan['operations']));refresh(r)
        with self.assertRaisesRegex(EvidenceError,'without_workload'):validate_run(r)
    def test_pass_requires_completed_workflows(self):
        r=fixture();r.timeline=[x for x in r.timeline if not(x.get('stage')=='workflow_finished' and x['operation_number']==0)]
        with self.assertRaisesRegex(EvidenceError,'unfinished_workflow'):validate_run(r)
    def test_completion_must_be_admitted(self):
        r=fixture();next(x for x in r.timeline if x.get('stage')=='workflow_finished')['operation_number']=9999
        with self.assertRaisesRegex(EvidenceError,'invalid_workflow_completion'):validate_run(r)
    def test_failed_workflow_does_not_pass(self):
        r=fixture();next(x for x in r.timeline if x.get('stage')=='workflow_finished')['outcome']='failed'
        with self.assertRaisesRegex(EvidenceError,'unfinished_workflow'):validate_run(r)
    def test_invalid_convergence_rejected(self):
        r=fixture();r.summary['observed_convergence_seconds_after_restore']='secret-url'
        with self.assertRaisesRegex(EvidenceError,'convergence'):validate_run(r)
    def test_non_object_final_rows_rejected(self):
        r=fixture();r.summary['final_consistency']['rows']=['bad']
        with self.assertRaisesRegex(EvidenceError,'invalid_final'):validate_run(r)
    def test_non_string_error_is_not_dropped(self):
        r=fixture('kafka-outage');r.requests[0].update(status=503,outcome='error',error={'sensitive':'do-not-print'});refresh(r)
        validate_run(r);metrics=workload_metrics(r,aligned_windows(r))
        self.assertEqual(metrics['error_codes'],{'unclassified':1})
    def test_html_delta_rows_keep_each_distinct_value(self):
        result=compare(fixture(),fixture('kafka-outage'))
        result['deltas']=[{'window':w,'operation':op,'outcome':'success','baseline_n':n,'fault_n':n,'delta_p50_ms':None,'delta_p95_ms':None,'delta_p99_ms':None}
                          for w,op,n in [('before','search',5),('fault','detail',17)]]
        with tempfile.TemporaryDirectory() as d:
            render(Path(d)/'render',result);html=(Path(d)/'render/report.html').read_text();md=(Path(d)/'render/report.md').read_text()
        self.assertIn('<td>before</td><td>search</td><td>success</td><td>5</td>',html)
        self.assertIn('<td>fault</td><td>detail</td><td>success</td><td>17</td>',html)
        self.assertIn('|before|search|success|5|5|',md)
    def test_duplicate_json_key_refused(self):
        with self.assertRaises(ValueError):strict_json('{"status":"failed","status":"passed"}')
    def test_nonfinite_numbers_refused(self):
        for raw in ('NaN','Infinity','-Infinity','1e9999'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):strict_json(raw)
    def test_valid_numeric_json_preserved(self):
        self.assertEqual(strict_json('{"x":1.25,"a":[0,false]}'),{'x':1.25,'a':[0,False]})
    def test_old_run_not_silently_backfilled(self):
        r=fixture();del r.summary['measurement_schema']
        with self.assertRaisesRegex(EvidenceError,'v8_measurement'):validate_run(r)
    def test_summary_latency_tamper_refused(self):
        r=fixture();r.summary['request_statistics'][0]['p95_ms']=0
        with self.assertRaisesRegex(EvidenceError,'statistics'):validate_run(r)
    def test_summary_http_count_tamper_refused(self):
        r=fixture();r.summary['workload_http_requests']-=1
        with self.assertRaisesRegex(EvidenceError,'request_count'):validate_run(r)
    def test_bool_status_refused(self):
        r=fixture();r.requests[0]['status']=True;refresh(r)
        with self.assertRaisesRegex(EvidenceError,'http_status'):validate_run(r)
    def test_false_outcome_refused(self):
        r=fixture();r.requests[0]['status']=503;refresh(r)
        with self.assertRaisesRegex(EvidenceError,'outcome'):validate_run(r)
    def test_negative_latency_refused(self):
        r=fixture();r.requests[0]['latency_ms']=-1;refresh(r)
        with self.assertRaisesRegex(EvidenceError,'latency'):validate_run(r)
    def test_queue_plus_transport_must_match_total(self):
        r=fixture();r.requests[0]['transport_ms']=100;refresh(r)
        with self.assertRaisesRegex(EvidenceError,'components'):validate_run(r)
    def test_unattempted_transport_not_http_success(self):
        r=fixture();r.requests[0].update(transport_ms=None,transport_attempted=False);refresh(r)
        with self.assertRaisesRegex(EvidenceError,'unsent'):validate_run(r)
    def test_backwards_clock_refused(self):
        r=fixture();r.requests[0]['request_finished_seconds']=0
        with self.assertRaisesRegex(EvidenceError,'inverted'):validate_run(r)
    def test_unadmitted_workflow_refused(self):
        r=fixture();r.timeline=[x for x in r.timeline if not(x.get('stage')=='workflow_admitted' and x['operation_number']==0)]
        r.summary['admitted_workflows']-=1;r.summary['skipped_workflows']+=1
        with self.assertRaisesRegex(EvidenceError,'without_workflow'):validate_run(r)
    def test_duplicate_admission_refused(self):
        r=fixture();r.timeline.append(next(x for x in r.timeline if x.get('stage')=='workflow_admitted'))
        with self.assertRaisesRegex(EvidenceError,'admission'):validate_run(r)
    def test_inconsistent_passed_orders_refused(self):
        r=fixture();r.summary['final_consistency']['rows'][0]['consistent']=False
        with self.assertRaisesRegex(EvidenceError,'inconsistent'):validate_run(r)
    def test_effect_not_seen_cannot_be_passed(self):
        r=fixture('kafka-outage');r.summary['fault_effect_observed']=False
        with self.assertRaisesRegex(EvidenceError,'effect'):validate_run(r)
    def test_plan_tamper_refused(self):
        r=fixture();r.plan['operations'][0]['quantity']=99
        with self.assertRaisesRegex(EvidenceError,'operations'):validate_run(r)
    def test_context_stability_lie_refused(self):
        r=fixture();r.context['after']['config_sha256']='d'*64
        with self.assertRaisesRegex(EvidenceError,'stability'):validate_run(r)
    def test_context_topology_tamper_refused(self):
        r=fixture();r.snapshot[0]['image_id']='different'
        with self.assertRaisesRegex(EvidenceError,'topology'):validate_run(r)
    def test_source_files_hash_must_match(self):
        r=fixture();r.context['before']['source_files']['mvp_app/core.py']='x'
        with self.assertRaisesRegex(EvidenceError,'source_fingerprint'):validate_run(r)

class FileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.run=fixture();self.path=save(self.root,self.run)
    def test_round_trip_and_hashes(self):
        run=load_run(self.root,self.run.run_id)
        self.assertEqual(len(run.hashes),6);self.assertEqual(run.summary,self.run.summary)
    def test_no_arbitrary_path(self):
        for value in ('../sim-aaaaaaaaaaaa','/tmp/a','sim-aaaaaaaaaaa','sim-'+'A'*12):
            with self.subTest(value=value),self.assertRaises(EvidenceError):load_run(self.root,value)
    def test_symlink_file_rejected(self):
        path=self.path/'summary.json';raw=path.read_bytes();path.unlink();target=self.root/'outside';target.write_bytes(raw);path.symlink_to(target)
        with self.assertRaisesRegex(EvidenceError,'symlinked'):load_run(self.root,self.run.run_id)
    def test_symlink_run_directory_rejected(self):
        renamed=self.path.with_name('copy');self.path.rename(renamed);self.path.symlink_to(renamed,target_is_directory=True)
        with self.assertRaisesRegex(EvidenceError,'symlinked'):load_run(self.root,self.run.run_id)
    def test_duplicate_json_in_file_refused(self):
        (self.path/'summary.json').write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(EvidenceError,'structured'):load_run(self.root,self.run.run_id)
    def test_truncated_jsonl_refused(self):
        with (self.path/'requests.jsonl').open('a') as stream:stream.write('{')
        with self.assertRaisesRegex(EvidenceError,'structured'):load_run(self.root,self.run.run_id)
    def test_oversize_file_refused(self):
        with (self.path/'summary.json').open('wb') as stream:stream.truncate(16*1024**2+1)
        with self.assertRaisesRegex(EvidenceError,'size_limit'):load_run(self.root,self.run.run_id)
    def test_renderer_will_not_overwrite_file(self):
        result=compare(fixture(),fixture('redis-outage'));render(self.root/'out',result)
        with self.assertRaisesRegex(EvidenceError,'already_exists'):render(self.root/'out',result)

class ComparisonTests(unittest.TestCase):
    def test_matching_conditions_produces_labelled_test_only_not_live_pass(self):
        result=compare(fixture(),fixture('redis-outage'))
        self.assertTrue(result['conditions_match']);self.assertEqual(result['comparison_status'],'test_only')
        self.assertIn('TEST_ONLY_NOT_LIVE_DATABASE_EVIDENCE',result['warnings'])
    def test_live_label_is_explicitly_not_independent_attestation(self):
        result=compare(fixture(live=True),fixture('redis-outage',live=True))
        self.assertEqual(result['comparison_status'],'complete');self.assertTrue(result['not_an_independent_runtime_attestation'])
    def test_same_run_refused(self):
        with self.assertRaisesRegex(EvidenceError,'distinct'):compare(fixture(),fixture('kafka-outage',run_id='sim-'+'a'*12))
    def test_specialized_operation_scenario_not_comparable(self):
        with self.assertRaisesRegex(EvidenceError,'supported_fault'):compare(fixture(),fixture('row-lock'))
    def test_seed_plan_difference_blocks_deltas(self):
        right=fixture('kafka-outage');right.plan=Plan(scenario='kafka-outage',seed=1).document()
        result=compare(fixture(),right)
        self.assertEqual(result['comparison_status'],'not_comparable')
        self.assertIn('planned_operations_differ',result['blockers'])
        self.assertTrue(all(x['delta_p50_ms'] is None for x in result['deltas']))
    def test_config_difference_blocks_comparison(self):
        right=fixture('kafka-outage')
        for side in ('before','after'):right.context[side]['config_sha256']='a'*64
        self.assertIn('config_sha256_differs',compare(fixture(),right)['blockers'])
    def test_engine_difference_blocks_comparison(self):
        right=fixture('kafka-outage')
        for side in ('before','after'):right.context[side]['engine_sha256']='a'*64
        self.assertIn('engine_sha256_differs',compare(fixture(),right)['blockers'])
    def test_image_difference_blocks_comparison(self):
        right=fixture('kafka-outage');right.snapshot[0]['image_id']='new-test-image'
        for side in ('before','after'):right.context[side]['topology_sha256']=digest(right.snapshot)
        self.assertIn('topology_sha256_differs',compare(fixture(),right)['blockers'])
    def test_source_difference_blocks_comparison(self):
        right=fixture('kafka-outage')
        for side in ('before','after'):
            right.context[side]['source_files']['new.py']='e'*64
            right.context[side]['source_sha256']=digest(right.context[side]['source_files'])
        self.assertIn('source_sha256_differs',compare(fixture(),right)['blockers'])
    def test_absent_context_blocks_comparison(self):
        right=fixture('kafka-outage');right.context={'available':False,'stable':False}
        self.assertIn('fault_context_missing_or_changed',compare(fixture(),right)['blockers'])
    def test_failed_baseline_cannot_authorize_comparison(self):
        left=fixture();left.summary['status']='failed'
        self.assertIn('baseline_not_passed',compare(left,fixture('kafka-outage'))['blockers'])
    def test_fault_failure_not_presented_as_recovered(self):
        right=fixture('kafka-outage',live=True);right.summary['status']='failed';right.summary['final_consistency']['consistent']=False
        result=compare(fixture(live=True),right)
        self.assertEqual(result['comparison_status'],'fault_not_recovered');self.assertFalse(result['fault']['final_consistency'])
    def test_actual_fault_window_used_not_nominal_options(self):
        right=fixture('kafka-outage');window=aligned_windows(right)[2]
        self.assertAlmostEqual(window['start_seconds'],8.1);self.assertAlmostEqual(window['end_seconds'],18.1)
    def test_long_request_is_counted_at_boundary_not_pure_fault(self):
        right=fixture('kafka-outage');row=right.requests[15]
        row.update(request_started_seconds=12.5,request_finished_seconds=15.,elapsed_seconds=15.001,
                   latency_ms=2500,client_queue_ms=2,transport_ms=2498)
        refresh(right);result=compare(fixture(),right)
        self.assertGreater(result['fault']['boundary_requests'],0)
        self.assertEqual(result['fault']['http_requests'],80)
        self.assertTrue(any(':boundary' in g['window'] for g in result['fault']['groups']))
    def test_low_sample_quantile_differences_are_null_not_zero(self):
        result=compare(fixture(),fixture('redis-outage'))
        before=next(r for r in result['deltas'] if r['window']=='before')
        self.assertEqual(before['delta_p50_ms'],0);self.assertIsNone(before['delta_p95_ms']);self.assertIsNone(before['delta_p99_ms'])
    def test_grouped_comparison_does_not_blend_operation_mix(self):
        left,right=fixture(),fixture('kafka-outage')
        right.requests[20]['operation']='search';refresh(right)
        result=compare(left,right)
        search=next(r for r in result['deltas'] if r['operation']=='search')
        self.assertEqual(search['baseline_n'],0);self.assertIsNone(search['delta_p50_ms'])
    def test_http_errors_not_discarded_from_total(self):
        right=fixture('kafka-outage');right.requests[20].update(status=503,outcome='error',error='mariadb_unavailable');refresh(right)
        result=compare(fixture(),right)
        self.assertEqual(result['fault']['outcomes']['error'],1);self.assertGreater(result['fault']['error_or_unknown_percent'],0)
    def test_unknown_response_distinct_from_http_error(self):
        right=fixture('kafka-outage');right.requests[20].update(status=0,outcome='client_unknown',error='TimeoutError');refresh(right)
        result=compare(fixture(),right)
        self.assertEqual(result['fault']['outcomes']['client_unknown'],1);self.assertEqual(result['fault']['outcomes']['error'],0)
    def test_409_counted_separately(self):
        right=fixture('kafka-outage');right.requests[20].update(status=409,outcome='conflict');refresh(right)
        result=compare(fixture(),right)
        self.assertEqual(result['fault']['outcomes']['conflict'],1);self.assertEqual(result['fault']['error_or_unknown_percent'],0)
    def test_diagnostic_failure_is_unknown_not_zero(self):
        right=fixture('kafka-outage')
        for r in right.timeline:
            if r.get('stage')=='diagnostics':r['data']['dependencies']['kafka']={'reachable':False}
        self.assertIsNone(compare(fixture(),right)['fault']['kafka_lag']['sampled_maximum'])
    def test_changed_context_during_run_blocks(self):
        right=fixture('kafka-outage');right.summary['status']='failed';right.context['stable']=False;right.context['after']['config_sha256']='d'*64
        self.assertIn('fault_context_missing_or_changed',compare(fixture(),right)['blockers'])
    def test_missing_fault_timing_blocks_not_guesses(self):
        right=fixture('kafka-outage');right.timeline=[r for r in right.timeline if r.get('stage')!='fault_applied']
        self.assertIn('fault_timeline_missing_or_duplicate',compare(fixture(),right)['blockers'])
    def test_past_workload_restoration_blocks_window_comparison(self):
        right=fixture('kafka-outage')
        next(r for r in right.timeline if r.get('stage')=='fault_restore')['elapsed_seconds']=100
        self.assertIn('fault_outside_paired_workload_window',compare(fixture(),right)['blockers'])
    def test_error_codes_are_allowlisted_not_arbitrary_messages(self):
        right=fixture('kafka-outage');right.requests[20].update(status=503,outcome='error',error='password_secret');refresh(right)
        result=compare(fixture(),right)
        self.assertNotIn('password_secret',json.dumps(result));self.assertEqual(result['fault']['error_codes'],{'unclassified':1})
    def test_report_is_static_without_requests_credentials_or_order_bodies(self):
        right=fixture('redis-outage');right.requests[0]['raw_secret']='secret-never-export'
        result=compare(fixture(),right)
        with tempfile.TemporaryDirectory() as d:
            render(Path(d),result);text=(Path(d)/'report.html').read_text()
            self.assertIn('Content-Security-Policy',text);self.assertNotIn('<script',text)
            self.assertNotIn('secret-never-export',text);self.assertNotIn('http://',text);self.assertIn('통신 p50',text)
