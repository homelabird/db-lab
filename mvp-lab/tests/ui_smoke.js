// Automated: python scripts/test-ui.py from the repository root.
// Browser-only regression check. Open the MVP page, then paste this whole
// expression in DevTools (or evaluate_script). Uses fake responses; reload after.
// No requests reach a real database while the check runs.
(async () => {
  const get = id => document.getElementById(id);
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const settle = () => new Promise(resolve => setTimeout(resolve, 35));
  const until = async (condition, message) => {
    const deadline = Date.now() + 3000;
    while (!condition()) {
      assert(Date.now() < deadline, message);
      await settle();
    }
  };
  const dialogOpen = id => Boolean(get(id)?.open);
  const input = async (node, value) => {
    const prototype = node instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(node, value);
    node.dispatchEvent(new Event(node instanceof HTMLSelectElement ? 'change' : 'input', {bubbles:true}));
    await settle();
  };
  const click = async node => { node.click(); await settle(); };
  const close = async id => { get(id).close(); await settle(); };
  const originalInterval = window.setInterval, originalClearInterval = window.clearInterval, timers = new Map();
  window.setInterval = (callback, delay, ...args) => {
    const id = originalInterval(callback, delay, ...args); timers.set(id, () => callback(...args)); return id;
  };
  window.clearInterval = id => { timers.delete(id); originalClearInterval(id); };
  const poll = () => { for (const callback of timers.values()) callback(); };
  const originalFetch = window.fetch;
  const order = {id:'00000000-0000-4000-8000-000000000001',item:'테스트 키보드',quantity:2,unit_price:30000,total:60000,status:'created',version:1};
  let rows = [], failCreate = true, conflict = false, failList = false, delaySearch = null, delayList = null;
  let unstable = false, sqlDown = false, timeoutDetail = false, failDiagnostics = false, dependenciesReachable = false;
  let projected = false;
  let delayInspection = null, delayDiagnostics = null;
  let cartMode = false, loseCartResponse = true, staleCartRead = false;
  const cartKeys = [], cartOrders = new Map();
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
      if (cartMode) {
        const body = JSON.parse(options.body), key = options.headers['Idempotency-Key'];
        assert(Object.keys(body).sort().join(',') === 'item,quantity,unit_price','cart uses existing order contract');
        assert(body.unit_price === ({'데일리 무선 키보드':59000,'포레스트 테이블 램프':48000})[body.item],'catalog prices remain fixed');
        cartKeys.push({key, ...body});
        const created = !cartOrders.has(key);
        if (created) {
          const saved = {...order, ...body, id:'00000000-0000-4000-8000-0000000000' + (cartOrders.size + 10), total:body.quantity * body.unit_price};
          cartOrders.set(key, saved); rows.push(saved);
        }
        if (loseCartResponse && body.item === '포레스트 테이블 램프') {
          loseCartResponse = false;
          return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true}));
        }
        return response({order:{...cartOrders.get(key)},created}, created ? 201 : 200);
      }
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
      if (cartMode) {
        const saved = [...cartOrders.values()].find(saved => path.endsWith(saved.id));
        assert(JSON.parse(options.body).expected_version === saved.version,'cart receipt expected version');
        saved.status = JSON.parse(options.body).status; saved.version++;
        return response({order:{...saved}});
      }
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
      const value = response({orders:filtered.slice(offset,offset+25),has_more:filtered.length>offset+25,next_cursor:filtered.length>offset+25?'second-page':null});
      return delayList ? new Promise(resolve => { delayList.resolve = () => resolve(value); }) : value;
    }
    if (path === '/api/diagnostics') {
      const value = failDiagnostics ? response({error:'api_capacity_exceeded'},503) : response({dependencies_reachable:dependenciesReachable,dependencies:{mariadb:{reachable:true,outbox_pending:2},redis:{reachable:dependenciesReachable},kafka:{reachable:true,lag:3},elasticsearch:{reachable:true}}});
      return delayDiagnostics ? new Promise(resolve => { delayDiagnostics.resolve = () => resolve(value); }) : value;
    }
    if (path.startsWith('/api/study/order/')) {
      const value = response({sql_stable_during_observation:!unstable,authoritative_valid:!sqlDown,mariadb:{reachable:!sqlDown,order:sqlDown?undefined:{...order}},redis:{reachable:true,order:{...order,version:1,status:'created'},comparison:unstable?'match':'mismatch',different_fields:['status','version']},elasticsearch:{reachable:true,order:projected?{...order}:null,comparison:projected?'match':'missing'}});
      return delayInspection ? new Promise(resolve => { delayInspection.resolve = () => resolve(value); }) : value;
    }
    if (timeoutDetail) return new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason), {once:true}));
    if (cartMode) {
      const saved = [...cartOrders.values()].find(saved => path.includes(saved.id));
      if (saved) return response({order:staleCartRead && !path.includes('fresh=1') ? {...saved,status:'created',version:1} : {...saved},source:path.includes('fresh=1')?'mariadb':'redis'});
    }
    if (path.includes(olderId)) return response({order:{...order,id:olderId,item:'이전 주문'},source:'mariadb'});
    if (path.includes('00000000-0000-4000-8000-000000000099')) return response({error:'order_not_found'},404);
    return response({order:{...order},source:path.includes('fresh=1')?'mariadb':'redis'});
  };
  try {
    location.hash = 'shop'; await settle();
    assert(!get('shopView').hidden && get('products').children.length === 4,'storefront default catalog');
    assert(calls.length === 0,'store and guide do not query hidden DB views');
    await input(get('catalogSort'), 'low');
    assert(get('products').firstChild.textContent.includes('모닝 세라믹 머그'),'lowest-price sort');
    await input(get('catalogSort'), 'high');
    assert(get('products').firstChild.textContent.includes('에어 오버이어 헤드폰'),'highest-price sort');
    await input(get('catalogSort'), 'curated');
    await click(document.querySelector('[data-category="desk"]'));
    assert(get('products').children.length === 2,'category filter');
    await input(get('catalogQuery'), '없는 상품');
    assert(!get('catalogEmpty').hidden,'catalog empty');
    await click(get('resetCatalog'));
    assert(get('products').children.length === 4 && get('catalogQuery').value === '' && document.querySelector('[data-category="all"]').getAttribute('aria-pressed') === 'true','empty catalog resets all filters');
    assert(document.activeElement === get('catalogQuery'),'catalog reset restores search focus');
    await click(document.querySelector('[data-category="desk"]'));
    await click(document.querySelector('.product-buy'));
    assert(dialogOpen('productDialog') && get('productDialog').textContent.includes('데일리 무선 키보드'),'product detail');
    const productDialogBounds = get('productDialog').getBoundingClientRect();
    const productTitleBounds = get('productTitle').getBoundingClientRect();
    const productClose = get('productDialog').querySelector('[data-close]'), productCloseBounds = productClose.getBoundingClientRect();
    assert(get('productDialog').scrollTop === 0 && productTitleBounds.top >= productDialogBounds.top && productTitleBounds.bottom <= productDialogBounds.bottom
      && productCloseBounds.top >= productDialogBounds.top && productCloseBounds.bottom <= productDialogBounds.bottom,'product initially shows title and close control');
    const productInputBounds = get('productQuantity').getBoundingClientRect();
    assert(document.activeElement === (productInputBounds.top >= productDialogBounds.top && productInputBounds.bottom <= productDialogBounds.bottom ? get('productQuantity') : productClose),'product initial focus stays on a visible control');
    assert(get('productQuantity').value === '1' && get('productTotal').textContent.includes('59,000'),'product defaults');
    await input(get('productQuantity'), '0'); get('productForm').requestSubmit(); await settle();
    assert(dialogOpen('productDialog') && get('cartItems').children.length === 0,'invalid product quantity never added');
    await input(get('productQuantity'), '3');
    assert(get('productTotal').textContent === '177,000원','quantity total');
    get('productForm').requestSubmit(); await settle();
    assert(!dialogOpen('productDialog') && get('cartCount').textContent.includes('3'),'add to cart');
    assert(keys.length === 0 && cartKeys.length === 0,'browsing and cart never create orders');
    location.hash = 'cart'; await settle();
    assert(!get('cartView').hidden && get('cartItems').querySelectorAll('.cart-line').length === 1,'cart view');
    const quantity = get('cartItems').querySelector('[data-cart-quantity]');
    await input(quantity, ''); await click(get('checkoutButton'));
    assert(!quantity.validity.valid && keys.length === 0 && cartKeys.length === 0,'blank cart quantity cannot checkout');
    assert(get('checkoutButton').disabled && quantity.getAttribute('aria-invalid') === 'true' && get('cartInputMessage').classList.contains('error'),'invalid quantity feedback and disabled checkout');
    assert(get('cartTotal').textContent === '수량 확인 필요','invalid quantity never displays an old checkout total');
    await input(quantity, '0'); await click(get('checkoutButton'));
    assert(!quantity.validity.valid && keys.length === 0 && cartKeys.length === 0,'zero cart quantity cannot checkout');
    quantity.focus();
    await input(quantity, '2');
    assert(document.activeElement === quantity,'cart quantity edit preserves focus');
    assert(!get('checkoutButton').disabled && quantity.getAttribute('aria-invalid') === 'false' && !get('cartInputMessage').textContent,'valid quantity restores checkout');
    assert(get('cartTotal').textContent.includes('118,000') && get('cartCount').textContent.includes('2'),'cart quantity and total');
    const stored = JSON.parse(sessionStorage.getItem('objet-cart-v1'));
    assert(stored.lines.length === 1 && stored.lines[0].name === '데일리 무선 키보드' && stored.lines[0].quantity === 2,'tab cart saved');
    assert(document.documentElement.scrollWidth <= innerWidth,'cart mobile overflow');
    await click(document.querySelectorAll('.product-add')[1]);
    get('cartItems').querySelector('[data-cart-remove]').focus();
    get('cartItems').querySelector('[data-cart-remove]').click(); await settle();
    assert(document.activeElement === get('cartItems').querySelector('[data-cart-quantity]'),'delete focuses nearest remaining quantity');
    get('cartItems').querySelector('[data-cart-remove]').click(); await settle();
    assert(get('cartItems').querySelectorAll('.cart-line').length === 0 && get('checkoutButton').disabled,'remove and empty cart');
    assert(document.activeElement === get('cartEmpty').querySelector('a'),'last deletion focuses empty-cart shopping action');
    assert(cartKeys.length === 0,'cart editing never writes DB');
    location.hash = 'shop'; await settle();
    const addProduct = async (index, quantity) => {
      await click(document.querySelectorAll('.product-buy')[index]);
      await input(get('productQuantity'), String(quantity));
      get('productForm').requestSubmit(); await settle();
    };
    await addProduct(0, 1); await addProduct(1, 2);
    location.hash = 'cart'; await settle();
    assert(get('cartItems').querySelectorAll('.cart-line').length === 2 && get('cartTotal').textContent.includes('155,000'),'multi-product cart');
    cartMode = true; AbortSignal.timeout = () => originalTimeout.call(AbortSignal, 10);
    get('checkoutButton').click(); get('checkoutButton').click();
    await until(() => !get('checkoutButton').disabled && get('cartMessage').classList.contains('error'), 'partial checkout settles after the lost response');
    assert(cartKeys.length === 2 && cartOrders.size === 2,'cart double checkout blocked');
    assert(get('cartItems').querySelectorAll('.cart-line').length === 1 && get('cartItems').textContent.includes('포레스트 테이블 램프'),'acknowledged line removed after partial save');
    assert(!get('receipt').hidden && get('receiptItems').querySelectorAll('.receipt-order').length === 1,'partial receipt visible');
    assert(get('receiptTotal').textContent === '59,000원' && get('cartTotal').textContent === '96,000원','saved receipt total excludes remaining unconfirmed cart');
    assert(get('cartMessage').textContent.includes('초과') && get('cartMessage').textContent.includes('다시'),'uncertain partial checkout explained');
    const pendingQuantity = get('cartItems').querySelector('[data-cart-quantity]');
    assert(pendingQuantity.disabled || pendingQuantity.readOnly,'uncertain line quantity locked');
    const pending = JSON.parse(sessionStorage.getItem('objet-cart-v1'));
    assert(pending.lines.length === 1 && pending.lines[0].key === cartKeys[1].key && pending.receipts.length === 1,'partial checkout and key persisted');
    AbortSignal.timeout = originalTimeout;
    get('checkoutButton').click(); await settle();
    assert(cartKeys.length === 3 && cartKeys[1].key === cartKeys[2].key,'uncertain cart retry uses same key');
    assert(cartKeys.filter(call => call.item === '데일리 무선 키보드').length === 1 && cartOrders.size === 2,'saved line is never posted again');
    assert(get('cartItems').querySelectorAll('.cart-line').length === 0 && get('checkoutButton').disabled,'successful checkout empties cart');
    assert(!get('receipt').hidden && get('receiptItems').querySelectorAll('.receipt-order').length === 2,'complete receipts retained');
    assert(get('receiptTotal').textContent === '155,000원','completed receipt total retained after cart is empty');
    assert(document.documentElement.scrollWidth <= innerWidth,'receipt mobile overflow');
    get('receiptItems').querySelector('.receipt-order').click(); await settle();
    assert(dialogOpen('orderDialog') && get('orderTitle').textContent === '데일리 무선 키보드','receipt opens existing order detail');
    get('updateForm').requestSubmit(); await settle();
    assert(JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].version === 2,'status change updates saved receipt snapshot');
    staleCartRead = true; get('detail').click(); await settle();
    assert(get('properties').textContent.includes('v1') && JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].version === 2,'stale Redis view cannot downgrade saved receipt');
    const updatedReceipt = [...cartOrders.values()][0]; updatedReceipt.status = 'shipped'; updatedReceipt.version = 3;
    get('fresh').click(); await settle(); staleCartRead = false;
    assert(JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].version === 3,'fresh original updates receipt snapshot');
    updatedReceipt.version = 2; get('fresh').click(); await settle();
    assert(JSON.parse(sessionStorage.getItem('objet-cart-v1')).receipts[0].version === 3,'an older original response cannot downgrade the receipt');
    updatedReceipt.version = 3;
    await close('orderDialog'); await click(get('receiptItems').querySelector('.receipt-order'));
    assert(get('properties').textContent.includes('배송 완료') && get('properties').textContent.includes('v3'),'reopened receipt uses latest known original');
    await close('orderDialog'); cartMode = false; rows = [];
    await click(document.querySelector('[data-category="all"]'));
    location.hash = 'orders'; get('originalTab').click(); await settle();
    document.querySelector('.skip-link').click(); await settle();
    assert(location.hash === '#orders' && !get('ordersView').hidden && document.activeElement === get('content'),'skip link preserves current view');
    assert(!get('emptyCreate').hidden,'empty state action');
    await click(get('emptyCreate'));
    assert(dialogOpen('createDialog') && document.activeElement.id === 'item','create focus');
    assert(!get('item').readOnly && get('item').value === '' && get('price').value === '30000' && get('quantity').value === '1','catalog values do not leak into custom draft');
    await input(get('item'), ' '); get('createForm').requestSubmit(); await settle();
    assert(keys.length === 0 && get('createMessage').classList.contains('error'),'blank manual item never creates a pending request');
    await input(get('item'), order.item); await input(get('quantity'), '2'); await input(get('price'), '30000');
    await input(get('quantity'), '0'); get('createForm').requestSubmit(); await settle();
    assert(keys.length === 0, 'invalid quantity never submitted');
    await input(get('quantity'), '2');
    AbortSignal.timeout = () => originalTimeout.call(AbortSignal, 10);
    get('createForm').requestSubmit(); get('createForm').requestSubmit();
    await until(() => keys.length === 1 && !get('saveOrder').disabled && get('createMessage').textContent.includes('초과'), 'manual create settles after the lost response');
    assert(dialogOpen('createDialog') && get('createMessage').classList.contains('error'),'failure preserves form');
    assert(get('item').value === order.item,'draft preserved');
    assert(get('createMessage').textContent.includes('초과'),'uncertain create timeout explained');
    assert(keys.length === 1,'double submission blocked');
    const pendingManual = JSON.parse(sessionStorage.getItem('objet-manual-order-v1'));
    assert(pendingManual.key === keys[0] && pendingManual.body === JSON.stringify({item:order.item,quantity:2,unit_price:30000}),'uncertain manual request is saved before retry');
    assert(['item','quantity','price'].every(id => get(id).disabled),'uncertain manual payload is locked');
    await close('createDialog'); await click(get('newOrder'));
    assert(get('item').value === order.item && get('quantity').value === '2' && get('price').value === '30000' && get('item').disabled,'reopened manual request preserves its immutable draft');
    AbortSignal.timeout = originalTimeout;
    const manualFetch = window.fetch;
    window.fetch = async (path, options = {}) => {
      const result = await manualFetch(path, options);
      return options.method === 'POST' ? response({order:{...order,quantity:3,unit_price:90000,total:270000},created:false}) : result;
    };
    get('createForm').requestSubmit(); await settle();
    assert(dialogOpen('createDialog') && !dialogOpen('orderDialog') && get('createMessage').classList.contains('error'),'mismatched manual acknowledgement cannot clear draft or open detail');
    assert(sessionStorage.getItem('objet-manual-order-v1') === JSON.stringify(pendingManual),'mismatched manual acknowledgement preserves exact retry data');
    window.fetch = manualFetch;
    get('createForm').requestSubmit(); await settle();
    assert(keys.length === 3 && keys.every(key => key === keys[0]),'retry idempotency survives response loss and mismatched acknowledgement');
    assert(!dialogOpen('createDialog') && dialogOpen('orderDialog'),'save opens detail');
    assert(sessionStorage.getItem('objet-manual-order-v1') === null,'confirmed manual acknowledgement clears pending request');
    assert(get('detailMessage').textContent.includes('이전에 저장된'),'recovered existing order explained');
    assert(get('properties').textContent.includes('60,000'),'human readable total');
    get('copyOrderId').click(); await settle();
    assert(await navigator.clipboard.readText() === order.id,'full order ID copied');
    assert(get('status').options.length === 2,'created transitions');
    await close('orderDialog');
    const originalRow = get('rows').querySelector('.order-link');
    originalRow.focus(); await click(originalRow);
    delayList = {};
    get('updateForm').requestSubmit(); await settle();
    assert(order.status === 'paid' && get('status').options[0].value === 'shipped','status transition');
    await until(() => Boolean(delayList.resolve), 'status update starts list refresh');
    get('orderDialog').dispatchEvent(new Event('cancel', {cancelable:true})); await settle();
    assert(!dialogOpen('orderDialog') && get('orderTable').hidden && document.activeElement === get('ordersView').querySelector('h1'),'closing detail during list refresh focuses the current view heading');
    delayList.resolve(); delayList = null; await settle();
    const refreshedRow = get('rows').querySelector('.order-link');
    refreshedRow.focus(); await click(refreshedRow);
    conflict = true; get('updateForm').requestSubmit(); await settle();
    assert(get('detailMessage').textContent.includes('원본 조회'),'conflict recovery');
    conflict = false;
    get('fresh').click(); await settle();
    assert(get('sourceLabel').textContent.includes('mariadb'),'fresh source');
    timeoutDetail = true;
    AbortSignal.timeout = () => originalTimeout.call(AbortSignal, 10);
    get('detail').click();
    await until(() => !get('detail').disabled && get('detailMessage').textContent.includes('초과'), 'detail timeout restores controls');
    assert(get('detailMessage').textContent.includes('초과') && !get('detail').disabled,'timeout shows uncertain outcome and restores controls');
    timeoutDetail = false; AbortSignal.timeout = originalTimeout;
    get('inspect').click(); await settle();
    assert(get('comparisonRows').children.length === 3 && get('comparisonRows').textContent.includes('데이터 없음'),'three source comparison');
    assert(get('comparisonRows').textContent.includes('v2') && get('comparisonRows').textContent.includes('v1') && get('comparisonRows').textContent.includes('상태, 버전'),'different versions and fields visible');
    const beforePolling = calls.filter(path => path.startsWith('/api/study/order/')).length;
    delayInspection = {}; await click(get('watchProjection')); poll(); await settle();
    assert(calls.filter(path => path.startsWith('/api/study/order/')).length === beforePolling + 1,'projection polling never overlaps');
    delayInspection.resolve(); delayInspection = null; await settle();
    projected = true; poll(); await settle();
    assert(!get('watchProjection').checked && get('comparison').textContent.includes('자동 확인을 종료'),'projection polling stops at verified agreement');
    projected = false;
    unstable = true; get('inspect').click(); await settle();
    assert(get('comparison').classList.contains('error') && !get('comparisonRows').textContent.includes('원본과 일치'),'unstable SQL cannot show verified agreement');
    unstable = false; sqlDown = true; get('inspect').click(); await settle();
    assert(get('comparisonRows').textContent.includes('조회 실패') && get('comparisonRows').textContent.includes('비교 불가'),'failed reads cannot verify projection agreement');
    sqlDown = false;
    get('updateForm').requestSubmit(); await settle();
    assert(get('updateForm').hidden,'terminal state cannot update');
    get('orderDialog').close(); await settle();
    await input(get('query'), '없는 상품');
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
    await input(get('query'), olderId);
    assert(!get('lookup').disabled,'ID lookup available outside loaded window');
    get('lookup').click(); await settle();
    assert(dialogOpen('orderDialog') && get('orderTitle').textContent === '이전 주문','older order can be opened by ID');
    get('orderDialog').close(); await settle();
    await input(get('query'), '00000000-0000-4000-8000-000000000099');
    get('lookup').click(); await settle();
    assert(get('listMessage').textContent.includes('찾을 수 없') && !get('lookup').disabled,'missing ID shows recoverable error');
    await input(get('query'), '');
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
    delayDiagnostics = {}; await click(get('watchHealth')); poll(); await settle();
    assert(calls.filter(path => path === '/api/diagnostics').length === beforeHealthPolling + 1,'health polling never overlaps');
    delayDiagnostics.resolve(); delayDiagnostics = null; await settle();
    location.hash = 'guide'; await settle();
    assert(!get('guideView').hidden,'guide navigation');
    const hiddenHealthCalls = calls.length; poll(); await settle();
    assert(calls.length === hiddenHealthCalls,'hidden health view does not poll');
    if (get('watchHealth').checked) await click(get('watchHealth'));
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
    await input(get('query'), '페이지 상품 30'); get('refresh').click(); await settle();
    assert(get('rows').children.length === 1 && get('previousPage').disabled,'refresh with edited filter resets old cursor');
    await input(get('query'), ''); get('refresh').click(); await settle();
    get('nextPage').click(); await settle();
    get('previousPage').click(); await settle();
    assert(get('rows').children.length === 25 && get('previousPage').disabled,'previous page');
    await input(get('statusFilter'), 'paid');
    assert(get('rows').children.length === 1 && get('pageLabel').textContent.startsWith('1'),'status filter resets cursor');
    await input(get('statusFilter'), '');
    await input(get('query'), '페이지 상품 30'); get('filterForm').requestSubmit(); await settle();
    assert(get('rows').children.length === 1 && get('rows').textContent.includes('상품 30'),'server search reaches beyond first page');
    assert(document.documentElement.scrollWidth <= innerWidth,'page overflow');
    return 'PASS: storefront/product/cart/partial-checkout/idempotent-retry/receipt/checkout/retry/detail/status/timeout/comparison/polling/clipboard/ID-lookup/server-search/status-filter/pagination/race/errors/navigation/diagnostics/mobile/keyboard/text-safety/layout';
  } finally {
    window.fetch = originalFetch; AbortSignal.timeout = originalTimeout;
    window.setInterval = originalInterval; window.clearInterval = originalClearInterval;
  }
})()
