import {useEffect, useRef, useState} from 'react';
import {errorText, request} from '../lib/api.js';
import {isOrderId, labels, number} from '../lib/orders.js';
import {CreateOrder} from './CreateOrder.jsx';

const firstPage = {cursor: null, history: []};

export function Orders({onOpenOrder, active, refreshKey = 0}) {
  const [mode, setMode] = useState('original'), [query, setQuery] = useState(''), [status, setStatus] = useState('');
  const [page, setPage] = useState(firstPage), [result, setResult] = useState({orders: [], next_cursor: null});
  const [loading, setLoading] = useState(false), [message, setMessage] = useState(''), [failed, setFailed] = useState(false);
  const [listFailed, setListFailed] = useState(false);
  const [note, setNote] = useState('MariaDB 원본 · 최신순 25개씩 · 전체 주문 검색과 상태 필터를 사용할 수 있습니다.');
  const [creating, setCreating] = useState(false);
  const revision = useRef(0), signature = useRef(''), lastRefresh = useRef(refreshKey);

  async function loadOrders({nextMode = mode, q = query.trim(), nextStatus = status, nextPage = page, reset = true} = {}) {
    const filter = JSON.stringify([nextMode, q, nextStatus]);
    if (reset || filter !== signature.current) nextPage = firstPage;
    signature.current = filter; setPage(nextPage);
    const current = ++revision.current;
    setMessage(''); setFailed(false); setListFailed(false); setLoading(true);
    try {
      const params = new URLSearchParams({limit: '25', q, status: nextStatus});
      if (nextPage.cursor) params.set('cursor', nextPage.cursor);
      const data = await request(nextMode === 'original' ? '/api/orders?' + params : '/api/search?q=' + encodeURIComponent(q));
      if (current !== revision.current) return;
      setResult(data);
      const total = data.total?.value ?? data.total;
      setNote(nextMode === 'original'
        ? 'MariaDB 원본 · 최신순 25개씩 · ' + new Date().toLocaleTimeString('ko-KR') + ' 갱신 · 페이지 조회는 원자적 스냅샷이 아닙니다.'
        : 'Elasticsearch · 최대 50개 표시' + (typeof total === 'number' ? ' / 전체 ' + number(total) + (data.total?.relation === 'gte' ? '개 이상' : '개') : '') + ' · 최신 주문이 아직 반영되지 않았을 수 있습니다.');
    } catch (error) {
      if (current !== revision.current) return;
      setResult({orders: [], next_cursor: null}); setListFailed(true); setFailed(true); setMessage(errorText(error));
    } finally { if (current === revision.current) setLoading(false); }
  }

  useEffect(() => {
    if (active) loadOrders();
    else { revision.current++; setLoading(false); }
    return () => { revision.current++; };
  }, [active]);
  useEffect(() => {
    if (active && refreshKey !== lastRefresh.current) loadOrders({reset: false});
    lastRefresh.current = refreshKey;
  }, [active, refreshKey]);

  function changeMode(nextMode) {
    setMode(nextMode); setQuery(''); setStatus('');
    loadOrders({nextMode, q: '', nextStatus: ''});
  }
  async function lookup() {
    const id = query.trim();
    if (!isOrderId(id)) return;
    const current = ++revision.current;
    setLoading(true); setFailed(false); setMessage('주문 ID로 원본을 조회하고 있습니다…');
    try {
      const data = await request('/api/orders/' + id + '?fresh=1');
      if (current !== revision.current) return;
      setMessage(''); onOpenOrder(data.order, data.source);
    } catch (error) {
      if (current === revision.current) { setFailed(true); setMessage(errorText(error)); }
    } finally { if (current === revision.current) setLoading(false); }
  }
  function created(data) {
    setCreating(false); setMode('original'); setQuery(''); setStatus('');
    loadOrders({nextMode: 'original', q: '', nextStatus: ''});
    onOpenOrder(data.order, 'mariadb', (data.created ? '주문을 저장했습니다.' : '이전에 저장된 주문을 확인했습니다.') + ' 읽기 전용 대조로 검색 반영을 확인해 보세요.');
  }

  const rows = result.orders, filtered = query.trim() || (mode === 'original' && status);
  const title = loading ? '주문을 불러오는 중입니다' : listFailed ? '목록을 불러오지 못했습니다' : filtered ? '검색 결과가 없습니다' : mode === 'original' ? '아직 주문이 없습니다' : '검색 인덱스에 주문이 없습니다';
  const hint = loading ? '잠시만 기다려 주세요.' : listFailed ? '새로고침으로 다시 시도할 수 있습니다.' : mode === 'original' ? filtered ? '다른 검색어나 상태를 선택해 보세요.' : '첫 주문을 만들어 저장과 조회를 시작하세요.' : '검색 반영에 시간이 걸릴 수 있습니다. 잠시 후 다시 검색해 주세요.';
  return <>
    <header><div className="section-heading"><h1 tabIndex="-1">주문 내역</h1><button id="newOrder" className="secondary" onClick={() => setCreating(true)}>직접 주문 입력</button></div><p>실습 스토어에 저장된 주문과 처리 상태를 확인하세요.</p></header>
    <div className="toolbar"><div className="tabs" role="group" aria-label="주문 데이터 선택"><button id="originalTab" aria-pressed={mode === 'original'} onClick={() => changeMode('original')}>주문 원본</button><button id="searchTab" aria-pressed={mode === 'search'} onClick={() => changeMode('search')}>검색 인덱스</button></div><button className="secondary" id="refresh" disabled={loading} onClick={() => loadOrders({reset: false})}>새로고침</button></div>
    <form className="filterbar" id="filterForm" onSubmit={event => { event.preventDefault(); loadOrders(); }}>
      <div className="field"><label htmlFor="query" id="queryLabel">{mode === 'original' ? '전체 주문에서 찾기' : 'Elasticsearch에서 검색'}</label><input id="query" type="search" maxLength="100" placeholder="상품 이름 또는 주문 ID" aria-describedby="listNote" disabled={loading} value={query} onChange={event => { setQuery(event.target.value); setMessage('검색 버튼이나 Enter를 누르면 새 조건으로 조회합니다.'); setFailed(false); }}/></div>
      <div id="statusFilterField" hidden={mode !== 'original'}><label htmlFor="statusFilter">상태</label><select id="statusFilter" disabled={loading} value={status} onChange={event => { setStatus(event.target.value); loadOrders({nextStatus: event.target.value}); }}><option value="">전체</option>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div>
      <button id="search" type="submit" className="secondary" disabled={loading}>검색</button><button id="lookup" type="button" className="secondary" hidden={mode !== 'original'} disabled={loading || !isOrderId(query.trim())} onClick={lookup}>주문 ID 조회</button>
    </form>
    <div id="listMessage" className={'notice' + (failed ? ' error' : '')} role="status">{message}</div>
    <div className="scroll" id="orderTable" hidden={loading || listFailed || !rows.length}><table><caption className="hint" id="tableCaption">{mode === 'original' ? '주문 원본' : '검색 인덱스'} · {rows.length}개 표시</caption><thead><tr><th scope="col">상품</th><th scope="col">상태</th><th scope="col">합계</th><th scope="col">버전</th></tr></thead><tbody id="rows">{rows.map(order => <tr key={order.id}><td><button className="order-link" onClick={() => onOpenOrder(order, mode === 'original' ? 'mariadb' : 'elasticsearch')}>{order.item}</button><span className="cell-id" title={order.id}>{order.id.slice(0, 8)}…{order.id.slice(-4)}</span></td><td><span className={'badge ' + (Object.hasOwn(labels, order.status) ? order.status : '')}>{labels[order.status] || order.status}</span></td><td>{number(order.total)}원</td><td>v{order.version}</td></tr>)}</tbody></table></div>
    <div id="empty" className="empty" hidden={!loading && !listFailed && rows.length > 0}><div className="empty-symbol" aria-hidden="true">▤</div><strong id="emptyTitle">{title}</strong><p className="hint" id="emptyHint">{hint}</p><button id="emptyCreate" className="secondary" hidden={loading || listFailed || mode !== 'original' || Boolean(filtered)} onClick={() => setCreating(true)}>첫 주문 만들기</button></div>
    <div className="toolbar" id="pagination" hidden={loading || listFailed || mode !== 'original'}><button id="previousPage" className="secondary" disabled={loading || !page.history.length} onClick={() => loadOrders({reset: false, nextPage: {cursor: page.history.at(-1), history: page.history.slice(0, -1)}})}>이전</button><span id="pageLabel" className="hint" role="status">{page.history.length + 1}페이지 · {rows.length}개 표시</span><button id="nextPage" className="secondary" disabled={loading || !result.next_cursor} onClick={() => loadOrders({reset: false, nextPage: {cursor: result.next_cursor, history: [...page.history, page.cursor]}})}>다음</button></div>
    <p className="table-note" id="listNote">{note}</p>
    <CreateOrder open={creating} onClose={() => setCreating(false)} onSaved={created}/>
  </>;
}
