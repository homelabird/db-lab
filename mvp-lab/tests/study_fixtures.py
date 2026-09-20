"""Synthetic report inputs ONLY. No DB timings or Docker results are asserted by these fixtures."""
import copy
import json
from pathlib import Path
from tools.measurement import digest
from tools.simulation import Plan,request_summary
from tools.study_analysis import Run


def context():
    files={'mvp_app/core.py':'a'*64,'tools/simulation.py':'b'*64}
    snapshot=[{'service':name,'image_id':'sha256:'+('1' if name in ('api','worker') else '2')*64,'volumes':{'/data':name+'-test-only-volume'}}
              for name in ('api','elasticsearch','kafka','mariadb','redis','worker')]
    before={'schema':1,'engine_sha256':'e'*64,'source_sha256':digest(files),'config_sha256':'c'*64,
            'topology_sha256':digest(snapshot),'client_python':'3.13.5','client_platform':'Linux','source_files':files}
    return {'schema':1,'available':True,'stable':True,'before':before,'after':copy.deepcopy(before)},snapshot


def fixture(scenario='baseline',run_id=None,live=False):
    plan=Plan(scenario=scenario).document();run_id=run_id or ('sim-' + ('a' if scenario=='baseline' else 'b')*12)
    ctx,snapshot=context()
    origin=5.;timeline=[{'elapsed_seconds':origin,'stage':'workload_started','clock_origin_seconds':origin}]
    if scenario!='baseline':
        for t,name in [(13.,'fault_requested'),(13.1,'fault_applied'),(23.1,'fault_restore_requested'),(23.2,'fault_restore')]:
            timeline.append({'elapsed_seconds':t,'stage':name})
    requests=[]
    for op in plan['operations']:
        start=origin+op['at_seconds']+.01
        ms=100 if scenario!='baseline' and 13.1<=start<23.1 else 10
        row={'elapsed_seconds':round(start+ms/1000+.001,6),'utc':'test-only','scope':'workload','phase':'baseline',
             'phase_finished':'baseline','operation':'detail','outcome':'success','status':200,'workflow_number':op['number'],
             'request_started_seconds':start,'request_finished_seconds':round(start+ms/1000,6),
             'latency_ms':ms,'client_queue_ms':2,'transport_ms':ms-2,'transport_attempted':True,'error':None}
        requests.append(row)
        timeline.extend([{'elapsed_seconds':start-.001,'stage':'workflow_admitted','operation_number':op['number']},
                         {'elapsed_seconds':start,'stage':'workflow_started','operation_number':op['number']},
                         {'elapsed_seconds':start+ms/1000,'stage':'workflow_finished','operation_number':op['number'],'outcome':'completed'}])
    timeline.append({'elapsed_seconds':22,'stage':'diagnostics','data':{'dependencies':{
        'mariadb':{'reachable':True,'outbox_pending':0 if scenario=='baseline' else 12},
        'kafka':{'reachable':True,'lag':0 if scenario=='baseline' else 5}}}})
    summary={'schema':1,'measurement_schema':2,'run_id':run_id,'scenario':scenario,
             'evidence_kind':'real-http-and-docker-actions' if live else 'TEST-ONLY-SYNTHETIC-REPORT-INPUT-NOT-DB-TIMING',
             'status':'passed','fault_restored':True,'fault_applied':scenario!='baseline','fault_effect_observed':True,
             'final_dependencies_reachable':True,'admitted_workflows':len(plan['operations']),'skipped_workflows':0,
             'workload_http_requests':len(requests),'elapsed_seconds':48,
             'request_statistics':request_summary(requests),'contract_errors':[],
             'final_consistency':{'consistent':True,'stable_rounds':2,'acknowledged_orders':1,
                                  'rows':[{'consistent':True,'state':'acknowledged_present'}]}}
    return Run(run_id,summary,plan,ctx,snapshot,requests,timeline,{'fixture':'not_real_file_hash'})


def refresh(run):
    run.summary['request_statistics']=request_summary(run.requests)
    run.summary['workload_http_requests']=sum(r['scope']=='workload' for r in run.requests)
    return run


def save(root,run):
    base=Path(root)/'reports/simulations'/run.run_id;base.mkdir(parents=True,exist_ok=True)
    for name,value in [('summary',run.summary),('plan',run.plan),('comparison-context',run.context),('runtime-snapshot',run.snapshot)]:
        (base/(name+'.json')).write_text(json.dumps(value))
    for name,value in [('requests',run.requests),('timeline',run.timeline)]:
        (base/(name+'.jsonl')).write_text(''.join(json.dumps(x)+'\n' for x in value))
    (base/'report.md').write_text('TEST FIXTURE\n')
    return base
