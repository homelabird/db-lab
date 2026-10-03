// Automated: python scripts/test-ui.py from the repository root.
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
  let unstable = false, sqlDown = false, timeoutDetail = false, failDiagnostics = false, dependenciesReachable = false;
  let projected = false;
  const olderId = '00000000-0000-4000-8000-000000000002';
  const keys = [], calls = [];
  const originalTimeout = AbortSignal.timeout;
  const response = (body, status = 200) => ({ok:status < 400,status,json:async () => body});
  window.fetch = async (path, options = {}) => {
    calls.push(path);
    assert(options.signal instanceof AbortSignal, 'every request has a deadline');
    if (path.startsWith('/api/search')) {
      if (delaySearch) return new Promise(resolve => { delaySearch.resolve = resolve; });
      return response({orders:[],total:{value:0,relation:'eq'}});
    }
    if (options.method === 'POST') {
      keys.push(options.headers['Idempotency-Key']);
      // The write committed but its response was lost. Retry must reuse the key.
      rows = [{...order}];
      if (failCreate) {
        failCreate = false;
        return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true}));
      }
      return response({order:{...order},created:false},200);
    }
    if (options.method === 'PATCH') {
      if (conflict) return response({error:'order_version_conflict'},409);
      assert(JSON.parse(options.body).expected_version === order.version,'expected version');
      order.status = JSON.parse(options.body).status; order.version++;
      rows = [{...order}]; return response({order:{...order}});
    }
    if (path === '/api/orders' || path.startsWith('/api/orders?')) {
      if (failList) return response({error:'mariadb_unavailable'},503);
      const params = new URL(path, location.origin).searchParams;
      const q = (params.get('q') || '').toLocaleLowerCase(), status = params.get('status');
      const filtered = rows.filter(o => (!q || (o.item+' '+o.id).toLocaleLowerCase().includes(q)) && (!status || o.status===status));
      const offset = params.has('cursor') ? 25 : 0;
      return response({orders:filtered.slice(offset,offset+25),has_more:filtered.length>offset+25,next_cursor:filtered.length>offset+25?'second-page':null});
    }
    if (path === '/api/diagnostics') return failDiagnostics ? response({error:'api_capacity_exceeded'},503) : response({dependencies_reachable:dependenciesReachable,dependencies:{mariadb:{reachable:true,outbox_pending:2},redis:{reachable:dependenciesReachable},kafka:{reachable:true,lag:3},elasticsearch:{reachable:true}}});
    if (path.startsWith('/api/study/order/')) return response({sql_stable_during_observation:!unstable,authoritative_valid:!sqlDown,mariadb:{reachable:!sqlDown,order:sqlDown?undefined:{...order}},redis:{reachable:true,order:{...order,version:1,status:'created'},comparison:unstable?'match':'mismatch',different_fields:['status','version']},elasticsearch:{reachable:true,order:projected?{...order}:null,comparison:projected?'match':'missing'}});
    if (timeoutDetail) return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true}));
    if (path.includes(olderId)) return response({order:{...order,id:olderId,item:'이전 주문'},source:'mariadb'});
    if (path.includes('00000000-0000-4000-8000-000000000099')) return response({error:'order_not_found'},404);
    return response({order:{...order},source:path.includes('fresh=1')?'mariadb':'redis'});
  };
  try {
    location.hash = 'shop'; await settle();
    assert(!get('shopView').hidden && get('products').children.length === 4,'storefront default catalog');
    assert(calls.length === 0,'store and guide do not query hidden DB views');
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
    assert(!get('item').readOnly && get('item').value === '' && get('price').value === '30000' && get('quantity').value === '1','catalog values do not leak into custom draft');
    get('item').value = order.item; get('quantity').value = '2'; get('price').value = '30000';
    get('quantity').value = '0'; get('createForm').requestSubmit();
    assert(keys.length === 0, 'invalid quantity never submitted');
    get('quantity').value = '2';
    AbortSignal.timeout = () => originalTimeout.call(AbortSignal, 10);
    get('createForm').requestSubmit(); get('createForm').requestSubmit(); await settle();
    assert(get('createDialog').open && get('createMessage').classList.contains('error'),'failure preserves form');
    assert(get('item').value === order.item,'draft preserved');
    assert(get('createMessage').textContent.includes('초과'),'uncertain create timeout explained');
    assert(keys.length === 1,'double submission blocked');
    AbortSignal.timeout = originalTimeout;
    get('createForm').requestSubmit(); await settle();
    assert(keys.length === 2 && keys[0] === keys[1],'retry idempotency');
    assert(!get('createDialog').open && get('orderDialog').open,'save opens detail');
    assert(get('detailMessage').textContent.includes('이전에 저장된'),'recovered existing order explained');
    assert(get('properties').textContent.includes('60,000'),'human readable total');
    get('copyOrderId').click(); await settle();
    assert(await navigator.clipboard.readText() === order.id,'full order ID copied');
    assert(get('status').options.length === 2,'created transitions');
    get('updateForm').requestSubmit(); await settle();
    assert(order.status === 'paid' && get('status').options[0].value === 'shipped','status transition');
    conflict = true; get('updateForm').requestSubmit(); await settle();
    assert(get('detailMessage').textContent.includes('원본 조회'),'conflict recovery');
    conflict = false;
    get('fresh').click(); await settle();
    assert(get('sourceLabel').textContent.includes('mariadb'),'fresh source');
    timeoutDetail = true;
    AbortSignal.timeout = () => originalTimeout.call(AbortSignal, 10);
    get('detail').click(); await settle();
    assert(get('detailMessage').textContent.includes('초과') && !get('detail').disabled,'timeout shows uncertain outcome and restores controls');
    timeoutDetail = false; AbortSignal.timeout = originalTimeout;
    get('inspect').click(); await settle();
    assert(get('comparisonRows').children.length === 3 && get('comparisonRows').textContent.includes('데이터 없음'),'three source comparison');
    assert(get('comparisonRows').textContent.includes('v2') && get('comparisonRows').textContent.includes('v1') && get('comparisonRows').textContent.includes('상태, 버전'),'different versions and fields visible');
    const beforePolling = calls.filter(path => path.startsWith('/api/study/order/')).length;
    get('watchProjection').checked = true; get('watchProjection').dispatchEvent(new Event('change')); monitor(); await settle();
    assert(calls.filter(path => path.startsWith('/api/study/order/')).length === beforePolling + 1,'projection polling never overlaps');
    projected = true; monitor(); await settle();
    assert(!get('watchProjection').checked && get('comparison').textContent.includes('자동 확인을 종료'),'projection polling stops at verified agreement');
    projected = false;
    unstable = true; get('inspect').click(); await settle();
    assert(get('comparison').classList.contains('error') && !get('comparisonRows').textContent.includes('원본과 일치'),'unstable SQL cannot show verified agreement');
    unstable = false; sqlDown = true; get('inspect').click(); await settle();
    assert(get('comparisonRows').textContent.includes('연결 실패') && get('comparisonRows').textContent.includes('비교 불가'),'SQL failure is not projection mismatch');
    sqlDown = false;
    get('updateForm').requestSubmit(); await settle();
    assert(get('updateForm').hidden,'terminal state cannot update');
    get('orderDialog').close(); await settle();
    get('query').value = '없는 상품'; get('query').dispatchEvent(new Event('input'));
    get('filterForm').requestSubmit(); await settle();
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
    get('query').value = olderId; get('query').dispatchEvent(new Event('input'));
    assert(!get('lookup').disabled,'ID lookup available outside loaded window');
    get('lookup').click(); await settle();
    assert(get('orderDialog').open && get('orderTitle').textContent === '이전 주문','older order can be opened by ID');
    get('orderDialog').close(); await settle();
    get('query').value = '00000000-0000-4000-8000-000000000099'; get('query').dispatchEvent(new Event('input'));
    get('lookup').click(); await settle();
    assert(get('listMessage').textContent.includes('찾을 수 없') && !get('lookup').disabled,'missing ID shows recoverable error');
    get('query').value = ''; get('query').dispatchEvent(new Event('input'));
    const beforeDiagnostics = calls.filter(path => path === '/api/diagnostics').length;
    location.hash = 'health'; await settle();
    assert(get('ordersView').hidden && !get('healthView').hidden,'separate views');
    assert(get('services').children.length === 4 && get('metrics').textContent.includes('3'),'diagnostics summary');
    assert(calls.filter(path => path === '/api/diagnostics').length === beforeDiagnostics + 1,'health diagnoses on entry');
    assert(get('services').textContent.includes('우회') && get('healthMessage').textContent.includes('대기'),'failure impact and backlog visible');
    failDiagnostics = true; get('diagnostics').click(); await settle();
    assert(get('services').children.length === 0 && get('healthMessage').textContent.includes('몰리고'),'failed diagnostics clears stale health');
    failDiagnostics = false; dependenciesReachable = true; get('diagnostics').click(); await settle();
    assert(!get('healthMessage').classList.contains('error') && get('healthMessage').textContent.includes('대기'),'reachable services with backlog are not completion');
    const beforeHealthPolling = calls.filter(path => path === '/api/diagnostics').length;
    get('watchHealth').checked = true; get('watchHealth').dispatchEvent(new Event('change')); monitor(); await settle();
    assert(calls.filter(path => path === '/api/diagnostics').length === beforeHealthPolling + 1,'health polling never overlaps');
    location.hash = 'guide'; await settle();
    assert(!get('guideView').hidden,'guide navigation');
    const hiddenHealthCalls = calls.length; monitor(); await settle();
    assert(calls.length === hiddenHealthCalls,'hidden health view does not poll'); get('watchHealth').checked = false;
    rows.push({...order,id:olderId,item:'외부에서 생성한 주문'});
    location.hash = 'orders'; await settle();
    assert(get('rows').children.length === 2,'orders refresh on reentry');
    assert(get('rows').lastChild.lastChild.offsetWidth > 0,'version visible on mobile');
    assert(get('refresh').getBoundingClientRect().height >= 44 && get('query').getBoundingClientRect().height >= 44,'touch controls at least 44px');
    order.item = '<img src=x onerror=alert(1)>'; rows = [{...order}]; get('refresh').click(); await settle();
    assert(get('rows').textContent.includes('<img') && !get('rows').querySelector('img'),'order content rendered as text');
    rows = Array.from({length:31},(_,i)=>({...order,id:olderId.slice(0,-2)+String(i).padStart(2,'0'),item:'페이지 상품 '+i,status:i===30?'paid':'created'}));
    get('refresh').click(); await settle();
    assert(get('rows').children.length === 25 && !get('nextPage').disabled,'first page is bounded');
    get('nextPage').click(); await settle();
    assert(get('rows').children.length === 6 && get('nextPage').disabled && !get('previousPage').disabled,'next page and terminal cursor');
    get('query').value = '페이지 상품 30'; get('refresh').click(); await settle();
    assert(get('rows').children.length === 1 && get('previousPage').disabled,'refresh with edited filter resets old cursor');
    get('query').value = ''; get('refresh').click(); await settle();
    get('nextPage').click(); await settle();
    get('previousPage').click(); await settle();
    assert(get('rows').children.length === 25 && get('previousPage').disabled,'previous page');
    get('statusFilter').value = 'paid'; get('statusFilter').dispatchEvent(new Event('change')); await settle();
    assert(get('rows').children.length === 1 && get('pageLabel').textContent.startsWith('1'),'status filter resets cursor');
    get('statusFilter').value = ''; get('statusFilter').dispatchEvent(new Event('change')); await settle();
    get('query').value = '페이지 상품 30'; get('filterForm').requestSubmit(); await settle();
    assert(get('rows').children.length === 1 && get('rows').textContent.includes('상품 30'),'server search reaches beyond first page');
    assert(document.documentElement.scrollWidth <= innerWidth,'page overflow');
    return 'PASS: storefront/checkout/retry/detail/status/timeout/comparison/polling/clipboard/ID-lookup/server-search/status-filter/pagination/race/errors/navigation/diagnostics/mobile/keyboard/text-safety/layout';
  } finally { window.fetch = originalFetch; AbortSignal.timeout = originalTimeout; }
})()
