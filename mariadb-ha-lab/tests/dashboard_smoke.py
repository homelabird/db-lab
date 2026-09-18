#!/usr/bin/env python3
"""Optional Playwright UI test with explicit synthetic API fixtures, NOT a live DB test.
Requires playwright Python package + Chromium. Not needed to run the actual lab.
"""
import json
from pathlib import Path
import shutil
import time
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / 'scripts/dashboard.html').read_text()

def fixture(healthy=3):
    nodes=[]
    for i in range(1,4):
        up = i <= healthy
        nodes.append({'node': f'galera{i}', 'ready':up, 'sampled_at':time.time(),
            'wsrep_cluster_status':'Primary' if up else 'non-Primary',
            'wsrep_local_state_comment':'Synced' if up else 'Joining',
            'wsrep_cluster_size':'3', 'wsrep_ready':'ON' if up else 'OFF',
            'wsrep_connected':'ON', 'wsrep_cluster_state_uuid':'test-fixture-not-a-real-cluster',
            'Questions':'1234', 'Threads_connected':'3', 'wsrep_flow_control_paused':'0.0'})
    return {'sampled_at':time.time(), 'ready_nodes':healthy, 'single_uuid':healthy>0, 'nodes':nodes}

with sync_playwright() as p:
    binary=shutil.which('chromium') or shutil.which('chromium-browser')
    browser=p.chromium.launch(executable_path=binary,headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1100})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    # Render local HTML in about:blank. No network or server navigation is needed.
    shim = '<script>window.fixture=' + json.dumps(fixture(3)) + ';window.fetch=async()=>({ok:true,json:async()=>window.fixture});</script>'
    page.set_content(shim + html)
    page.wait_for_function("document.querySelectorAll('.card').length===3")
    assert 'Ready 3/3' in page.locator('#summary').inner_text()
    assert page.locator('.badge.good').count()==3
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.evaluate('(data)=>{window.fixture=data}',fixture(1))
    page.locator('#refresh').click()
    page.wait_for_function("document.querySelector('#summary').textContent.includes('Ready 1/3')")
    assert page.locator('.badge.good').count()==1
    page.set_viewport_size({'width':390,'height':900})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.locator('details').first.locator('summary').click()
    assert 'galera1' in page.locator('details').first.locator('pre').inner_text()
    assert not errors, errors
    print('PASS: desktop/mobile layout, 3 node cards, degraded-state refresh, raw JSON, no JS errors.')
    print('Scope: synthetic API fixture only. No actual MariaDB/container/replication was exercised.')
    browser.close()
