#!/usr/bin/env python3
"""Test built React UI mocks and the real Vite/API proxy with DB doubles."""
from pathlib import Path
import mimetypes
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def ready(page):
    page.wait_for_function("() => document.querySelector('#root #products')?.children.length === 4")


def dialog_open(page, name):
    return page.evaluate('id => Boolean(document.getElementById(id)?.open)', name)


def detail_start_visible(page):
    position = page.locator('#orderDialog').evaluate("""dialog => {
        const top = dialog.getBoundingClientRect().top;
        const title = dialog.querySelector('#orderTitle').getBoundingClientRect();
        const close = dialog.querySelector('[data-close="orderDialog"]').getBoundingClientRect();
        return {scrollTop: dialog.scrollTop, titleTop: title.top, closeTop: close.top,
                visible: dialog.scrollTop === 0 && title.top >= top && close.top >= top
                         && title.bottom <= innerHeight && close.bottom <= innerHeight};
    }""")
    assert position['visible'], 'Order dialog initially scrolls past its title or close control: ' + str(position)


def dev_proxy_check(browser):
    sys.path[:0] = [str(ROOT / 'mvp-lab'), str(ROOT / 'mvp-lab/tests')]
    from mvp_app.api import Application, make_handler
    from mvp_app.http_policy import BoundedHTTPServer
    from fakes import MemoryRepo, MemoryCache, MemorySearch, MemoryBroker

    repo = MemoryRepo()
    server = BoundedHTTPServer(('127.0.0.1', 0), make_handler(
        lambda: Application(repo, MemoryCache(), MemorySearch(), MemoryBroker())))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    process = context = None
    thread.start()
    try:
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        process = subprocess.Popen(['node', 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1',
                                    '--port', str(port), '--strictPort'], cwd=ROOT / 'mvp-lab/frontend',
                                   env={**os.environ, 'MVP_API_URL': f'http://127.0.0.1:{server.server_port}'},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError('Test Vite process exited before readiness')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Test Vite readiness deadline exceeded')
                time.sleep(.1)
        context = browser.new_context(service_workers='block')
        page = context.new_page()
        page.set_default_timeout(15000)
        origins = []
        page.on('request', lambda request: origins.append(request.all_headers().get('origin'))
                if request.method in {'POST', 'PATCH'} else None)
        page.goto(origin, wait_until='domcontentloaded', timeout=15000)
        ready(page)
        created, updated, health = page.evaluate("""async () => {
            const request = async (path, method = 'GET', body) => {
                const response = await fetch(path, {method, signal: AbortSignal.timeout(5000),
                    headers: {'Content-Type': 'application/json', 'Idempotency-Key': 'dev-proxy-check'},
                    body: body && JSON.stringify(body)});
                return {status: response.status, body: await response.json()};
            };
            const created = await request('/api/orders', 'POST', {item: 'Dev proxy check', quantity: 1, unit_price: 100});
            if (created.status !== 201) return [created, null, null];
            const updated = await request('/api/orders/' + created.body.order.id, 'PATCH', {status: 'paid', expected_version: 1});
            return [created, updated, await request('/health/ready')];
        }""")
        assert created['status'] == 201, created
        assert updated['status'] == 200 and updated['body']['order']['version'] == 2, updated
        assert health['status'] == 200 and health['body']['database_reachable'], health
        assert origins == [origin, origin], origins
        order_id = created['body']['order']['id']
        hostile = {'Origin': 'https://hostile.invalid'}
        denied = context.request.post(origin + '/api/orders', headers=hostile,
                                      data={'item': 'Rejected write', 'quantity': 1, 'unit_price': 100}, timeout=5000)
        assert denied.status == 403 and denied.json()['error'] == 'cross_origin_write_rejected'
        denied = context.request.patch(origin + '/api/orders/' + order_id, headers=hostile,
                                       data={'status': 'shipped', 'expected_version': 2}, timeout=5000)
        assert denied.status == 403 and denied.json()['error'] == 'cross_origin_write_rejected'
        assert len(repo.orders) == 1 and repo.get(order_id)['status'] == 'paid' and repo.get(order_id)['version'] == 2
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            try:
                if process is not None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
    print('PASS: real Vite/API loopback with fake DBs; browser Origin POST 201/PATCH 200; hostile Origin 403; owned servers stopped.')


def main():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        print('BLOCKED: install scripts/requirements-ui.txt and run python -m playwright install chromium.', file=sys.stderr)
        return 127
    if not shutil.which('node') or not (ROOT / 'mvp-lab/frontend/node_modules/vite/bin/vite.js').is_file():
        print('BLOCKED: install Node.js and run npm ci in mvp-lab/frontend first.', file=sys.stderr)
        return 127
    static = ROOT / 'mvp-lab/mvp_app/static'
    if not (static / 'index.html').is_file():
        print('BLOCKED: run npm ci and npm run build in mvp-lab/frontend first.', file=sys.stderr)
        return 127
    html = (static / 'index.html').read_text(encoding='utf-8')
    assert 'type="module"' in html and '/assets/' in html, 'Expected the built React entry point'
    smoke = (ROOT / 'mvp-lab/tests/ui_smoke.js').read_text(encoding='utf-8')
    started = time.monotonic()
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                print('BLOCKED: run python -m playwright install chromium.', file=sys.stderr)
                return 127
            raise
        try:
            dev_proxy_check(browser)
            for width, height in ((1280, 900), (390, 844), (320, 740)):
                context = browser.new_context(viewport={'width': width, 'height': height}, service_workers='block',
                                              permissions=['clipboard-read', 'clipboard-write'])
                try:
                    errors = []
                    restored_keys = []
                    receipt_stale_read = False
                    pending_key = '00000000-0000-4000-8000-000000000012'
                    retry_receipt = {'id': '00000000-0000-4000-8000-000000000011', 'item': '포레스트 테이블 램프',
                                     'quantity': 2, 'unit_price': 48000, 'total': 96000, 'status': 'created',
                                     'version': 1, 'created_at': '2026-10-11T00:00:00.000+00:00'}
                    list_orders = []
                    manual_mode = False
                    manual_requests, manual_orders = [], {}
                    manual_payload = {'item': '새로고침 복구 주문', 'quantity': 2, 'unit_price': 30000}
                    manual_receipt = {**manual_payload, 'id': '00000000-0000-4000-8000-000000000030',
                                      'total': 60000, 'status': 'created', 'version': 1,
                                      'created_at': '2026-10-11T00:00:00.000+00:00'}
                    def fixture(route):
                        if route.request.url == 'https://db-lab.invalid/':
                            route.fulfill(content_type='text/html', body=html,
                                          headers={'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; connect-src 'self'"})
                        elif route.request.url.startswith('https://db-lab.invalid/assets/'):
                            asset = (static / urlsplit(route.request.url).path.lstrip('/')).resolve()
                            if not asset.is_relative_to(static.resolve()) or not asset.is_file():
                                errors.append('Missing or invalid built asset: ' + route.request.url)
                                route.abort()
                                return
                            route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(asset)[0] or 'application/octet-stream')
                        elif route.request.url.startswith('https://db-lab.invalid/api/orders/' + retry_receipt['id']):
                            if route.request.method == 'PATCH':
                                patch = route.request.post_data_json
                                if patch['expected_version'] != retry_receipt['version']:
                                    errors.append('Receipt update used a stale expected version')
                                retry_receipt.update(status=patch['status'], version=retry_receipt['version'] + 1)
                                route.fulfill(json={'order': retry_receipt})
                            else:
                                fresh = '?fresh=1' in route.request.url
                                returned = {**retry_receipt, 'status': 'created', 'version': 1} if receipt_stale_read and not fresh else retry_receipt
                                route.fulfill(json={'order': returned, 'source': 'mariadb' if fresh else 'redis'})
                        elif route.request.method == 'GET' and route.request.url.startswith('https://db-lab.invalid/api/orders/' + manual_receipt['id']):
                            route.fulfill(json={'order': manual_receipt, 'source': 'mariadb'})
                        elif route.request.method == 'GET' and route.request.url.startswith('https://db-lab.invalid/api/orders'):
                            route.fulfill(json={'orders': list_orders, 'has_more': False, 'next_cursor': None})
                        elif route.request.method == 'POST' and route.request.url == 'https://db-lab.invalid/api/orders':
                            if manual_mode:
                                key = route.request.headers.get('idempotency-key')
                                manual_requests.append({'key': key, 'body': route.request.post_data})
                                if route.request.post_data_json != manual_payload:
                                    errors.append('Restored manual request changed its payload')
                                if key not in manual_orders:
                                    manual_orders[key] = manual_receipt
                                    route.abort()
                                else:
                                    route.fulfill(json={'order': manual_orders[key], 'created': False})
                                return
                            restored_keys.append(route.request.headers.get('idempotency-key'))
                            if route.request.post_data_json != {'item': '포레스트 테이블 램프', 'quantity': 2, 'unit_price': 48000}:
                                errors.append('Restored pending request changed its payload')
                            returned = {**retry_receipt, 'quantity': 1, 'total': 48000} if len(restored_keys) == 1 else retry_receipt
                            route.fulfill(json={'order': returned, 'created': False})
                        else:
                            errors.append('Unexpected request: ' + route.request.url)
                            route.abort()
                    context.route('**/*', fixture)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' and 'Content Security Policy' in message.text else None)
                    page.goto('https://db-lab.invalid/')
                    ready(page)
                    result = page.evaluate('() => Promise.race([' + smoke +
                                           ", new Promise((_, reject) => setTimeout(() => reject(Error('UI smoke timed out')), 30000))])")
                    assert isinstance(result, str) and result.startswith('PASS:'), result
                    page.evaluate('sessionStorage.clear()')
                    page.goto('https://db-lab.invalid/')
                    ready(page)
                    buy = page.get_by_role('button', name='데일리 무선 키보드 자세히 보기')
                    buy.focus()
                    page.keyboard.press('Enter')
                    page.locator('#productDialog').wait_for(state='visible')
                    assert dialog_open(page, 'productDialog')
                    page.keyboard.press('Shift+Tab')
                    assert page.locator('#productDialog').evaluate('(dialog) => dialog.contains(document.activeElement)')
                    page.keyboard.press('Escape')
                    page.wait_for_function("() => !document.getElementById('productDialog')?.open")
                    assert not dialog_open(page, 'productDialog')
                    assert buy.evaluate('(button) => document.activeElement === button')
                    buy.click()
                    page.locator('#productQuantity').fill('2')
                    assert page.locator('#productTotal').inner_text() == '118,000원'
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.locator('#addToCart').click()
                    page.evaluate("location.hash = 'cart'")
                    page.wait_for_function("() => !document.getElementById('cartView').hidden")
                    assert page.locator('.cart-line').count() == 1
                    assert page.locator('[data-cart-quantity]').input_value() == '2'
                    quantity = page.locator('[data-cart-quantity]')
                    for invalid in ('', '0', '1001', '1.5'):
                        quantity.fill(invalid)
                        assert quantity.get_attribute('aria-invalid') == 'true'
                        assert page.locator('#checkoutButton').is_disabled()
                        assert page.locator('#cartTotal').inner_text() == '수량 확인 필요'
                        assert page.locator('#cartInputMessage').inner_text()
                    quantity.fill('2')
                    assert not page.locator('#checkoutButton').is_disabled()
                    assert page.locator('#cartInputMessage').inner_text() == ''
                    quantity.focus()
                    page.keyboard.press('ControlOrMeta+A')
                    page.keyboard.type('3')
                    assert quantity.evaluate('(input) => document.activeElement === input')
                    assert '177,000' in page.locator('#cartTotal').inner_text()
                    page.keyboard.press('ControlOrMeta+A')
                    page.keyboard.type('2')
                    assert quantity.evaluate('(input) => document.activeElement === input')
                    assert restored_keys == []
                    cart_state = page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1'))")
                    assert cart_state['lines'][0]['quantity'] == 2
                    page.reload()
                    ready(page)
                    page.wait_for_function("() => !document.getElementById('cartView').hidden")
                    assert page.locator('.cart-line').count() == 1
                    assert page.locator('[data-cart-quantity]').input_value() == '2'
                    assert '118,000' in page.locator('#cartTotal').inner_text()
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).lines[0].key") == cart_state['lines'][0]['key']
                    page.locator('#cartView .cart-list-heading a').click()
                    page.wait_for_function("() => location.hash === '#collection' && !document.getElementById('shopView').hidden")
                    assert page.locator('#collection').evaluate('(section) => document.activeElement === section')
                    assert page.evaluate("scrollY > 0 && document.getElementById('collection').getBoundingClientRect().top < innerHeight")
                    page.get_by_role('button', name='포레스트 테이블 램프 장바구니에 담기').click()
                    page.locator('.header-cart').click()
                    page.wait_for_function("() => !document.getElementById('cartView').hidden")
                    page.get_by_role('button', name='데일리 무선 키보드 장바구니에서 삭제').focus()
                    page.keyboard.press('Enter')
                    assert page.get_by_role('spinbutton', name='포레스트 테이블 램프 수량').evaluate('(input) => document.activeElement === input')
                    page.get_by_role('button', name='포레스트 테이블 램프 장바구니에서 삭제').focus()
                    page.keyboard.press('Enter')
                    assert page.locator('#cartEmpty a').evaluate('(link) => document.activeElement === link')
                    receipt = {'id': '00000000-0000-4000-8000-000000000010', 'item': '데일리 무선 키보드',
                               'quantity': 1, 'unit_price': 59000, 'total': 59000, 'status': 'created',
                               'version': 1, 'created_at': '2026-10-11T00:00:00.000+00:00'}
                    page.evaluate("state => sessionStorage.setItem('objet-cart-v1', JSON.stringify(state))",
                                  {'lines': [{'name': '포레스트 테이블 램프', 'quantity': 2, 'key': pending_key}],
                                   'receipts': [receipt]})
                    page.reload()
                    ready(page)
                    assert page.locator('[data-cart-quantity]').is_disabled()
                    assert page.locator('[data-cart-remove]').is_disabled()
                    assert page.locator('.receipt-order').count() == 1
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).lines[0].key") == pending_key
                    page.locator('#checkoutButton').click()
                    page.wait_for_function("() => document.getElementById('cartMessage').classList.contains('error') && !document.getElementById('checkoutButton').disabled")
                    assert page.locator('.cart-line').count() == 1
                    assert page.locator('.receipt-order').count() == 1
                    assert page.locator('[data-cart-quantity]').is_disabled()
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).lines[0].key") == pending_key
                    page.locator('#checkoutButton').click()
                    page.wait_for_function("() => document.querySelectorAll('.receipt-order').length === 2")
                    assert restored_keys == [pending_key, pending_key]
                    assert page.locator('.cart-line').count() == 0
                    assert page.locator('#receiptTotal').inner_text() == '155,000원'
                    receipt_button = page.get_by_role('button', name='포레스트 테이블 램프 주문 상세 보기')
                    receipt_button.focus()
                    page.keyboard.press('Enter')
                    page.locator('#orderDialog').wait_for(state='visible')
                    assert dialog_open(page, 'orderDialog')
                    detail_start_visible(page)
                    page.keyboard.press('Escape')
                    assert receipt_button.evaluate('(button) => document.activeElement === button')
                    page.keyboard.press('Enter')
                    page.locator('#update').click()
                    page.wait_for_function("() => JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[1].version === 2")
                    assert '결제 완료 · v2' in page.locator('#receiptItems').inner_text()
                    page.keyboard.press('Escape')
                    assert receipt_button.evaluate('(button) => document.activeElement === button')
                    page.reload()
                    ready(page)
                    page.get_by_role('button', name='포레스트 테이블 램프 주문 상세 보기').click()
                    assert 'v2' in page.locator('#properties').inner_text()
                    receipt_stale_read = True
                    page.locator('#detail').click()
                    page.wait_for_function("() => !document.getElementById('detail').disabled")
                    assert 'v1' in page.locator('#properties').inner_text()
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[1].version") == 2
                    retry_receipt.update(status='shipped', version=3)
                    page.locator('#fresh').click()
                    page.wait_for_function("() => JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[1].version === 3")
                    page.locator('#orderDialog').evaluate('(dialog) => dialog.close()')
                    page.reload()
                    ready(page)
                    page.get_by_role('button', name='포레스트 테이블 램프 주문 상세 보기').click()
                    assert '배송 완료' in page.locator('#properties').inner_text() and 'v3' in page.locator('#properties').inner_text()
                    page.locator('#orderDialog').evaluate('(dialog) => dialog.close()')
                    receipts = [{**receipt, 'id': f'00000000-0000-4000-8000-{index:012d}'} for index in range(20, 25)]
                    page.evaluate("state => sessionStorage.setItem('objet-cart-v1', JSON.stringify(state))",
                                  {'lines': [], 'receipts': receipts})
                    page.reload()
                    ready(page)
                    assert page.locator('.receipt-order').count() == 5
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts.length") == 5
                    page.evaluate("state => sessionStorage.setItem('objet-cart-v1', JSON.stringify(state))",
                                  {'lines': [], 'receipts': [receipt]})
                    page.reload()
                    ready(page)
                    assert page.locator('.cart-line').count() == 0
                    assert page.locator('#receipt').is_visible()
                    page.locator('.receipt-order').click()
                    page.locator('#orderDialog').wait_for(state='visible')
                    assert dialog_open(page, 'orderDialog')
                    assert page.locator('#orderTitle').inner_text() == receipt['item']
                    page.locator('#orderDialog').evaluate('(dialog) => dialog.close()')
                    page.evaluate("sessionStorage.setItem('objet-cart-v1', '{broken')")
                    page.reload()
                    ready(page)
                    assert page.locator('.cart-line').count() == 0
                    assert page.locator('#checkoutButton').is_disabled()
                    page.evaluate("state => sessionStorage.setItem('objet-cart-v1', JSON.stringify(state))",
                                  {'lines': [{'name': '없는 상품', 'quantity': 1, 'key': 'invalid-product'},
                                             {'name': '데일리 무선 키보드', 'quantity': 1001, 'key': 'invalid-quantity'},
                                             {'name': '포레스트 테이블 램프', 'quantity': 1, 'key': '<invalid>'}],
                                   'receipts': []})
                    page.reload()
                    ready(page)
                    assert page.locator('.cart-line').count() == 0
                    assert page.locator('#checkoutButton').is_disabled()
                    page.evaluate("state => sessionStorage.setItem('objet-cart-v1', JSON.stringify(state))",
                                  {'lines': [], 'receipts': [receipt]})
                    list_orders = [{**receipt, 'status': 'paid', 'version': 2}]
                    page.reload()
                    ready(page)
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].version") == 1
                    page.evaluate("location.hash = 'orders'")
                    page.wait_for_function("() => !document.getElementById('ordersView').hidden && document.querySelectorAll('#rows .order-link').length === 1")
                    page.locator('#rows .order-link').click()
                    page.locator('#orderDialog').wait_for(state='visible')
                    detail_start_visible(page)
                    assert 'v2' in page.locator('#properties').inner_text()
                    page.wait_for_function("() => JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].version === 2")
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].status") == 'paid'
                    page.keyboard.press('Escape')
                    page.wait_for_function("() => !document.getElementById('orderDialog')?.open")
                    page.evaluate("location.hash = 'cart'")
                    page.wait_for_function("() => !document.getElementById('cartView').hidden")
                    assert '결제 완료 · v2' in page.locator('#receiptItems').inner_text()
                    page.reload()
                    ready(page)
                    assert '결제 완료 · v2' in page.locator('#receiptItems').inner_text()
                    assert page.evaluate("JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].version") == 2
                    list_orders = []
                    page.evaluate("location.hash = 'orders'")
                    page.wait_for_function("() => !document.getElementById('ordersView').hidden")
                    manual_mode = True
                    page.locator('#newOrder').click()
                    page.locator('#item').fill(manual_payload['item'])
                    page.locator('#quantity').fill(str(manual_payload['quantity']))
                    page.locator('#price').fill(str(manual_payload['unit_price']))
                    page.locator('#saveOrder').click()
                    page.wait_for_function("() => document.getElementById('createMessage').classList.contains('error') && !document.getElementById('saveOrder').disabled")
                    pending_manual = page.evaluate("JSON.parse(sessionStorage.getItem('objet-manual-order-v1'))")
                    assert manual_requests == [pending_manual]
                    assert page.locator('#item').is_disabled()
                    page.reload()
                    ready(page)
                    page.wait_for_function("() => !document.getElementById('ordersView').hidden")
                    page.locator('#newOrder').click()
                    assert page.locator('#item').input_value() == manual_payload['item']
                    assert page.locator('#quantity').input_value() == str(manual_payload['quantity'])
                    assert page.locator('#price').input_value() == str(manual_payload['unit_price'])
                    assert all(page.locator('#' + field).is_disabled() for field in ('item', 'quantity', 'price'))
                    page.locator('#saveOrder').click()
                    page.locator('#orderDialog').wait_for(state='visible')
                    assert '이전에 저장된 주문을 확인했습니다.' in page.locator('#orderDialog').inner_text()
                    assert manual_requests == [pending_manual, pending_manual] and len(manual_orders) == 1
                    assert page.evaluate("sessionStorage.getItem('objet-manual-order-v1')") is None
                    page.keyboard.press('Escape')
                    page.wait_for_function("() => !document.getElementById('orderDialog')?.open")
                    manual_mode = False
                    page.locator('.skip-link').focus()
                    page.keyboard.press('Enter')
                    assert page.evaluate("location.hash === '#orders' && !document.getElementById('ordersView').hidden && document.activeElement.id === 'content'")
                    manual = page.locator('#newOrder')
                    manual.click()
                    page.locator('#createDialog').wait_for(state='visible')
                    assert dialog_open(page, 'createDialog')
                    page.keyboard.press('Shift+Tab')
                    assert page.locator('#createDialog').evaluate('(dialog) => dialog.contains(document.activeElement)')
                    page.keyboard.press('Escape')
                    page.wait_for_function("() => !document.getElementById('createDialog')?.open")
                    assert not dialog_open(page, 'createDialog')
                    assert manual.evaluate('(button) => document.activeElement === button')
                    page.evaluate("location.hash = 'shop'")
                    page.wait_for_function("() => !document.getElementById('shopView').hidden")
                    page.get_by_role('link', name='주문 내역', exact=True).click()
                    page.wait_for_function("() => !document.getElementById('ordersView').hidden")
                    page.locator('#newOrder').click()
                    page.locator('#createDialog').wait_for(state='visible')
                    page.go_back()
                    page.wait_for_function("() => !document.getElementById('shopView').hidden && !document.getElementById('createDialog')?.open")
                    assert page.locator('#shopView h1').evaluate('(heading) => document.activeElement === heading')
                    page.get_by_role('button', name='데일리 무선 키보드 자세히 보기').click()
                    page.locator('#productDialog').wait_for(state='visible')
                    page.evaluate("location.hash = 'collection'")
                    page.wait_for_function("() => !document.getElementById('productDialog')?.open")
                    assert page.locator('#collection').evaluate('(section) => document.activeElement === section')
                    assert not errors, errors
                    print(f'Chromium {width}x{height}: {result}; keyboard dialogs/delete/skip; cart and receipt reload/version/retry/SQL-list confirmation; manual lost-response reload/retry; hash/back dialog closure; collection scrolling')
                finally:
                    context.close()
        finally:
            browser.close()
    print(f'Ran 4 tests in {time.monotonic() - started:.3f}s')
    print('PASS: 3 mocked UI sizes and real Vite/API loopback with fake DBs; no Docker or real DB was contacted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
