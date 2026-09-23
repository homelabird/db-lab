"""Requires REAL ansible-playbook. Uses a harmless script fixture, not Docker.

Separate from the ordinary host suite. Missing Ansible is a blocked prerequisite,
not a skipped test that can be reported as a passing Ansible validation.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = shutil.which('ansible-playbook')


class RealAnsibleTests(unittest.TestCase):
    def setUp(self):
        if not PLAYBOOK:
            raise RuntimeError('ansible-playbook is required; run scripts/test-ansible.sh')
        self.tmp=tempfile.TemporaryDirectory(prefix='db-lab-ansible-integration-');self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.project=self.base/'project';self.project.mkdir()
        (self.project/'scripts').mkdir();(self.project/'scripts/control.py').write_text('')
        (self.project/'mvp-lab').mkdir();(self.project/'mvp-lab/lab.sh').write_text('#!/bin/bash\n')
        (self.project/'kafka-lab').mkdir();(self.project/'kafka-lab/lab.sh').write_text('#!/bin/bash\n')
        (self.project/'all.sh').write_text('''#!/bin/bash
set -eu
printf '%s\\n' "$*" >> called.txt
[ "${3:-}" != "init" ] || { [ -f mvp-lab/.env ] || echo fixed-test-setting > mvp-lab/.env; }
printf 'NATIVE-PRIVATE-SENTINEL'
[ "${3:-}" != "doctor" ] || exit 7
''')
        self.inv=self.base/'hosts.json'
        self.inv.write_text(json.dumps({'all':{'children':{'db_lab':{'hosts':{
            'local_a':{'ansible_connection':'local','ansible_python_interpreter':sys.executable,
                       'db_lab_project_root':str(self.project)}}}}}}))
        self.env=dict(os.environ,ANSIBLE_CONFIG=str(ROOT/'ansible/ansible.cfg'),
                      ANSIBLE_LIBRARY=str(ROOT/'ansible/library'),
                      ANSIBLE_MODULE_UTILS=str(ROOT/'ansible/module_utils'),
                      ANSIBLE_LOCAL_TEMP=str(self.base/'ansible-tmp'),
                      ANSIBLE_NOCOLOR='1',ANSIBLE_HOST_KEY_CHECKING='True')

    def run_play(self, request=None, *, target='mvp', consent=False, check=False, filename='control.yml'):
        extra={'db_lab_target':target,'db_lab_request':request or {'action':'status'},'db_lab_allow_changes':consent}
        args=[PLAYBOOK,'-i',str(self.inv),str(ROOT/'ansible'/filename),'-e',json.dumps(extra)]
        if check:args.append('--check')
        return subprocess.run(args,cwd=ROOT/'ansible',env=self.env,text=True,capture_output=True,timeout=60)

    def test_real_ansible_syntax_of_both_playbooks(self):
        for name in ('control.yml','collect.yml','deploy.yml','bootstrap.yml'):
            p=subprocess.run([PLAYBOOK,'-i',str(self.inv),str(ROOT/'ansible'/name),'--syntax-check'],cwd=ROOT/'ansible',env=self.env,text=True,capture_output=True,timeout=60)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)

    def test_real_ansible_module_packaging_and_execution(self):
        p=self.run_play();self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertEqual((self.project/'called.txt').read_text().strip(),'--fail-fast mvp status')
        self.assertNotIn('NATIVE-PRIVATE-SENTINEL',p.stdout+p.stderr)

    def test_real_check_mode_never_calls_controller(self):
        p=self.run_play({'action':'up'},check=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertFalse((self.project/'called.txt').exists());self.assertFalse((self.project/'reports').exists());self.assertFalse((self.project/'.state').exists())

    def test_real_consent_failure_before_execution(self):
        p=self.run_play({'action':'up'})
        self.assertNotEqual(p.returncode,0);self.assertFalse((self.project/'called.txt').exists())

    def test_real_benchmark_dispatch_requires_consent_and_preserves_bounded_args(self):
        request={'action':'benchmark','options':{'seconds':15,'rate':100}}
        refused=self.run_play(request,target='kafka')
        self.assertNotEqual(refused.returncode,0)
        self.assertFalse((self.project/'called.txt').exists())
        planned=self.run_play(request,target='kafka',check=True)
        self.assertEqual(planned.returncode,0,planned.stdout+planned.stderr)
        self.assertFalse((self.project/'called.txt').exists())
        applied=self.run_play(request,target='kafka',consent=True)
        self.assertEqual(applied.returncode,0,applied.stdout+applied.stderr)
        self.assertEqual((self.project/'called.txt').read_text().strip(),
                         '--fail-fast benchmark kafka --rate 100 --seconds 15')

    def test_real_failure_is_not_ignored(self):
        p=self.run_play({'action':'doctor'})
        self.assertNotEqual(p.returncode,0);self.assertIn('7',p.stdout)
        self.assertNotIn('NATIVE-PRIVATE-SENTINEL',p.stdout+p.stderr)

    def test_real_init_preserves_existing_file(self):
        first=self.run_play({'action':'init'},consent=True)
        self.assertEqual(first.returncode,0,first.stdout+first.stderr)
        data=(self.project/'mvp-lab/.env').read_bytes()
        second=self.run_play({'action':'init'},consent=True)
        self.assertEqual(second.returncode,0,second.stdout+second.stderr)
        self.assertEqual((self.project/'mvp-lab/.env').read_bytes(),data)
        self.assertIn('changed=0',second.stdout)

    def test_inventory_request_is_not_shadowed_by_play_default(self):
        inv=json.loads(self.inv.read_text())
        host=inv['all']['children']['db_lab']['hosts']['local_a']
        host['db_lab_request']={'action':'doctor'}
        self.inv.write_text(json.dumps(inv))
        p=subprocess.run([PLAYBOOK,'-i',str(self.inv),str(ROOT/'ansible/control.yml')],
                         cwd=ROOT/'ansible',env=self.env,text=True,capture_output=True,timeout=60)
        self.assertNotEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertEqual((self.project/'called.txt').read_text().strip(),'--fail-fast mvp doctor')

    def test_inventory_consent_and_init_are_respected(self):
        inv=json.loads(self.inv.read_text())
        host=inv['all']['children']['db_lab']['hosts']['local_a']
        host.update(db_lab_request={'action':'init'},db_lab_allow_changes=True)
        self.inv.write_text(json.dumps(inv))
        p=subprocess.run([PLAYBOOK,'-i',str(self.inv),str(ROOT/'ansible/control.yml')],
                         cwd=ROOT/'ansible',env=self.env,text=True,capture_output=True,timeout=60)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertTrue((self.project/'mvp-lab/.env').is_file())

    def test_real_fetch_collects_exactly_allowlisted_files(self):
        run_id='accept-0123456789ab'
        folder=self.project/'mvp-lab/reports/share'/run_id
        folder.mkdir(parents=True)
        for name in ('summary.json','report.md','junit.xml'):
            (folder/name).write_text('PUBLIC-FIXTURE-'+name)
        (folder/'do-not-fetch.env').write_text('PRIVATE-FIXTURE')
        inv=json.loads(self.inv.read_text())
        host=inv['all']['children']['db_lab']['hosts']['local_a']
        host.update(db_lab_acceptance_id=run_id,db_lab_collect_root=str(self.base/'collected'))
        self.inv.write_text(json.dumps(inv))
        p=subprocess.run([PLAYBOOK,'-i',str(self.inv),str(ROOT/'ansible/collect.yml')],
                         cwd=ROOT/'ansible',env=self.env,text=True,capture_output=True,timeout=60)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        dest=self.base/'collected/local_a'/run_id
        self.assertEqual(sorted(x.name for x in dest.iterdir()),['junit.xml','report.md','summary.json'])
        self.assertEqual((dest/'report.md').read_text(),'PUBLIC-FIXTURE-report.md')
        self.assertNotIn('PRIVATE-FIXTURE',p.stdout+p.stderr)

    def test_real_multi_host_writes_require_limit(self):
        inv=json.loads(self.inv.read_text());hosts=inv['all']['children']['db_lab']['hosts'];hosts['local_b']=dict(hosts['local_a'])
        self.inv.write_text(json.dumps(inv))
        p=self.run_play({'action':'up'},consent=True)
        self.assertNotEqual(p.returncode,0);self.assertFalse((self.project/'called.txt').exists())

    def run_bootstrap(self, vars, *, check=False):
        args=[PLAYBOOK,'-i',str(self.inv),str(ROOT/'ansible/bootstrap.yml'),'-e',json.dumps(vars)]
        if check: args.append('--check')
        return subprocess.run(args,cwd=ROOT/'ansible',env=self.env,text=True,capture_output=True,timeout=60)

    def test_bootstrap_requires_explicit_install_confirmation(self):
        p=self.run_bootstrap({'db_lab_engine':'podman','db_lab_allow_changes':True,
                              'ansible_distribution':'Debian','ansible_distribution_major_version':'12'})
        self.assertNotEqual(p.returncode,0)
        self.assertIn('explicitly confirm the selected package source',p.stdout+p.stderr)

    def test_bootstrap_check_mode_only_plans_supported_package_set(self):
        inventory=json.loads(self.inv.read_text())
        inventory['all']['children']['db_lab']['hosts']['local_a']['ansible_become_method']='sudo'
        self.inv.write_text(json.dumps(inventory))
        p=self.run_bootstrap({'db_lab_engine':'docker','ansible_distribution':'Ubuntu',
                              'ansible_distribution_version':'24.04',
                              'ansible_distribution_major_version':'24'},check=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn('docker-compose-v2',p.stdout)
        self.assertIn('Check mode makes no package, service, group, firewall, or repository changes',p.stdout)

    def test_debian_podman_plan_selects_signed_backports_compose(self):
        inventory=json.loads(self.inv.read_text())
        inventory['all']['children']['db_lab']['hosts']['local_a']['ansible_become_method']='sudo'
        self.inv.write_text(json.dumps(inventory))
        p=self.run_bootstrap({'db_lab_engine':'podman','ansible_distribution':'Debian',
                              'ansible_distribution_version':'12.11','ansible_distribution_major_version':'12'},check=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn('podman-compose from bookworm-backports',p.stdout)
        self.assertIn('signed official Bookworm Backports source',p.stdout)


if __name__=='__main__':unittest.main()
