// Browser-only regression check. Open the MVP page, then paste this whole
// expression in DevTools (or evaluate_script). Uses fake responses; reload after.
// No requests reach a real database while the check runs.
(async () => {
  const get = id => document.getElementById(id);
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const settle = () => new Promise(resolve => setTimeout(resolve, 35));
  const originalFetch = window.fetch;
  const order = {id:'00000000-0000-4000-8000-000000000001',item:'테스트 키보드',quantity:2,unit_price:30000,total:60000,status:'created',version:1};
  let rows = [], failCreate = true, conflict = false, failList = false, delaySearch = null;
  const keys = [];
  const response = (body, status = 200) => ({ok:status < 400,status,json:async () => body});
  window.fetch = async (path, options = {}) => {
    if (path.startsWith('/api/search')) {
      if (delaySearch) return new Promise(resolve => { delaySearch.resolve = resolve; });
      return response({orders:[],total:{value:0,relation:'eq'}});
    }
    if (options.method === 'POST') {
      keys.push(options.headers['Idempotency-Key']);
      if (failCreate) { failCreate = false; throw new TypeError('network'); }
      rows = [{...order}]; return response({order:{...order},created:true},201);
    }
    if (options.method === 'PATCH') {
      if (conflict) return response({error:'order_version_conflict'},409);
      assert(JSON.parse(options.body).expected_version === order.version,'expected version');
      order.status = JSON.parse(options.body).status; order.version++;
      rows = [{...order}]; return response({order:{...order}});
    }
    if (path === '/api/orders') return failList ? response({error:'mariadb_unavailable'},503) : response({orders:rows});
    if (path === '/api/diagnostics') return response({dependencies_reachable:false,dependencies:{mariadb:{reachable:true,outbox_pending:2},redis:{reachable:false},kafka:{reachable:true,lag:3},elasticsearch:{reachable:true}}});
    if (path.startsWith('/api/study/order/')) return response({redis:{reachable:true,comparison:'match'},elasticsearch:{reachable:true,comparison:'missing'}});
    return response({order:{...order},source:path.includes('fresh=1')?'mariadb':'redis'});
  };
  try {
    location.hash = 'shop'; await settle();
    assert(!get('shopView').hidden && get('products').children.length === 4,'storefront default catalog');
    document.querySelector('[data-category="desk"]').click();
    assert(get('products').children.length === 2,'category filter');
    get('catalogQuery').value = '없는 상품'; get('catalogQuery').dispatchEvent(new Event('input'));
    assert(!get('catalogEmpty').hidden,'catalog empty');
    get('catalogQuery').value = ''; get('catalogQuery').dispatchEvent(new Event('input'));
    document.querySelector('.product-buy').click();
    assert(get('createDialog').open && get('item').value === '데일리 무선 키보드','product checkout');
    assert(get('item').readOnly && get('price').readOnly && get('customItem').hidden,'catalog price locked');
    get('quantity').value = '3'; get('quantity').dispatchEvent(new Event('input'));
    assert(get('checkoutTotal').textContent === '177,000원','quantity total');
    assert(keys.length === 0,'browsing never creates orders');
    get('createDialog').close(); await settle();
    document.querySelector('[data-category="all"]').click();
    location.hash = 'orders'; get('originalTab').click(); await settle();
    assert(!get('emptyCreate').hidden,'empty state action');
    get('emptyCreate').click();
    assert(get('createDialog').open && document.activeElement.id === 'item','create focus');
    get('item').value = order.item; get('quantity').value = '2'; get('price').value = '30000';
    get('createForm').requestSubmit(); await settle();
    assert(get('createDialog').open && get('createMessage').classList.contains('error'),'failure preserves form');
    assert(get('item').value === order.item,'draft preserved');
    get('createForm').requestSubmit(); await settle();
    assert(keys.length === 2 && keys[0] === keys[1],'retry idempotency');
    assert(!get('createDialog').open && get('orderDialog').open,'save opens detail');
    assert(get('properties').textContent.includes('60,000'),'human readable total');
    assert(get('status').options.length === 2,'created transitions');
    get('updateForm').requestSubmit(); await settle();
    assert(order.status === 'paid' && get('status').options[0].value === 'shipped','status transition');
    conflict = true; get('updateForm').requestSubmit(); await settle();
    assert(get('detailMessage').textContent.includes('원본 조회'),'conflict recovery');
    conflict = false;
    get('fresh').click(); await settle();
    assert(get('sourceLabel').textContent.includes('mariadb'),'fresh source');
    get('inspect').click(); await settle();
    assert(get('comparison').textContent.includes('데이터 없음'),'comparison summary');
    get('updateForm').requestSubmit(); await settle();
    assert(get('updateForm').hidden,'terminal state cannot update');
    get('orderDialog').close(); await settle();
    get('query').value = '없는 상품'; get('query').dispatchEvent(new Event('input'));
    assert(!get('empty').hidden && get('emptyCreate').hidden,'filtered empty');
    get('searchTab').click(); await settle();
    assert(!get('search').hidden && get('emptyHint').textContent.includes('반영'),'search empty');
    delaySearch = {}; get('refresh').click(); await settle();
    get('originalTab').click(); await settle();
    delaySearch.resolve(response({orders:[],total:0})); await settle();
    assert(!get('orderTable').hidden,'late search cannot replace original');
    failList = true; get('refresh').click(); await settle();
    assert(get('listMessage').classList.contains('error') && get('orderTable').hidden,'failed list hides stale data');
    failList = false; get('refresh').click(); await settle();
    assert(!get('orderTable').hidden && !get('refresh').disabled,'list retry');
    location.hash = 'health'; await settle(); get('diagnostics').click(); await settle();
    assert(get('ordersView').hidden && !get('healthView').hidden,'separate views');
    assert(get('services').children.length === 4 && get('metrics').textContent.includes('3'),'diagnostics summary');
    location.hash = 'guide'; await settle();
    assert(!get('guideView').hidden,'guide navigation');
    location.hash = 'orders'; await settle();
    assert(document.documentElement.scrollWidth <= innerWidth,'page overflow');
    return 'PASS: storefront/category/checkout/total/empty/create/retry/detail/status/conflict/search/race/error recovery/navigation/diagnostics/layout';
  } finally { window.fetch = originalFetch; }
})()
