import {Fragment, useCallback, useEffect, useRef, useState} from 'react';
import {Dialog} from './Dialog.jsx';
import {errorText, request} from '../lib/api.js';
import {labels, number, transitions} from '../lib/orders.js';

const comparisons = {match: '원본과 일치', mismatch: '원본과 다름', missing: '데이터 없음', unknown: '비교 불가'};
const fields = {id: '주문 ID', item: '상품', quantity: '수량', unit_price: '단가', total: '합계', status: '상태', version: '버전', created_at: '주문 시각'};

export function OrderDetail({order, source, onClose, onOrderConfirmed, initialMessage = ''}) {
  const [currentOrder, setCurrentOrder] = useState(order), [currentSource, setCurrentSource] = useState(source);
  const [nextStatus, setNextStatus] = useState(transitions[order.status]?.[0] || '');
  const [busy, setBusy] = useState(false), [message, setMessage] = useState(initialMessage), [failed, setFailed] = useState(false);
  const [comparison, setComparison] = useState(null), [watch, setWatch] = useState(false), [projectionComplete, setProjectionComplete] = useState(false);
  const [rawResult, setRawResult] = useState({order, source});
  const revision = useRef(0), inFlight = useRef(false), watchRef = useRef(false);
  useEffect(() => () => { revision.current++; }, []);

  const action = useCallback(async name => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setFailed(false); setMessage('요청 중입니다…'); setComparison(null); setProjectionComplete(false);
    const current = ++revision.current;
    try {
      const path = name === 'inspect' ? '/api/study/order/' : '/api/orders/';
      const options = name === 'update' ? {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status: nextStatus, expected_version: currentOrder.version})} : {};
      const result = await request(path + encodeURIComponent(currentOrder.id) + (name === 'fresh' ? '?fresh=1' : ''), options);
      if (current !== revision.current) return;
      setRawResult(result);
      if (name === 'inspect') {
        setComparison(result);
        if (watchRef.current && result.sql_stable_during_observation && result.authoritative_valid && result.elasticsearch?.comparison === 'match') {
          watchRef.current = false; setWatch(false); setProjectionComplete(true);
        }
      } else {
        const dataSource = result.source || 'mariadb';
        setCurrentOrder(result.order); setCurrentSource(dataSource); setNextStatus(transitions[result.order.status]?.[0] || '');
        onOrderConfirmed(result.order, dataSource, name === 'update');
      }
      setMessage(name === 'update' ? '주문 상태를 변경했습니다.' : '조회했습니다.');
    } catch (error) {
      if (current !== revision.current) return;
      setFailed(true); setMessage(errorText(error) + ' 표시된 주문은 이전 값입니다.'); setRawResult(error.body || {error: error.message});
    } finally {
      if (current === revision.current) { inFlight.current = false; setBusy(false); }
    }
  }, [currentOrder, nextStatus, onOrderConfirmed]);

  useEffect(() => {
    if (!watch) return;
    const timer = setInterval(() => { if (!document.hidden) action('inspect'); }, 5000);
    return () => clearInterval(timer);
  }, [watch, action]);

  async function copyOrderId() {
    try { await navigator.clipboard.writeText(currentOrder.id); setFailed(false); setMessage('주문 ID를 복사했습니다.'); }
    catch { setFailed(true); setMessage('복사 권한을 사용할 수 없습니다. 상세의 주문 ID를 직접 선택해 복사해 주세요.'); }
  }
  const options = transitions[currentOrder.status] || [];
  const comparable = comparison?.sql_stable_during_observation && comparison?.authoritative_valid;
  const comparisonMessage = !comparison ? '' : projectionComplete ? '검색 반영을 확인했습니다. 자동 확인을 종료합니다. 원자적 스냅샷은 아닙니다.' : comparable ? '조회 시점의 원본과 대조했습니다. 원자적 스냅샷은 아닙니다.' : '원본을 검증하지 못했거나 관찰 중 변경됐습니다. 다시 대조해 주세요.';
  const properties = [['상태', labels[currentOrder.status] || currentOrder.status], ['수량', number(currentOrder.quantity)], ['단가', number(currentOrder.unit_price) + '원'], ['합계', number(currentOrder.total) + '원'], ['버전', 'v' + currentOrder.version], ['주문 ID', currentOrder.id], ['주문 시각', currentOrder.created_at ? new Date(currentOrder.created_at).toLocaleString('ko-KR') : '—']];

  return <Dialog id="orderDialog" labelledBy="orderTitle" onClose={onClose}>
    <div className="dialog-heading"><span className="hint">주문 상세</span><button className="close" data-close="orderDialog" aria-label="주문 상세 닫기" onClick={onClose}>×</button></div>
    <h2 id="orderTitle">{currentOrder.item}</h2><div className={'notice' + (failed ? ' error' : '')} id="detailMessage" role="status">{message}</div>
    <dl className="properties" id="properties">{properties.map(([label, value]) => <Fragment key={label}><dt>{label}</dt><dd>{value}</dd></Fragment>)}</dl><button id="copyOrderId" className="secondary" onClick={copyOrderId}>주문 ID 복사</button>
    <section className="detail-section"><h3>주문 상태 변경</h3><p className="hint">결제·배송은 실습 상태입니다. 실제 결제나 배송은 발생하지 않습니다.</p><p id="transitionHint" className="hint">{options.length ? '현재 버전 v' + currentOrder.version + '을 기준으로 변경합니다.' : '처리가 끝난 주문은 상태를 변경할 수 없습니다.'}</p><form id="updateForm" hidden={!options.length} onSubmit={event => { event.preventDefault(); action('update'); }}><label htmlFor="status">다음 상태</label><div className="actions"><select id="status" value={nextStatus} onChange={event => setNextStatus(event.target.value)}>{options.map(value => <option key={value} value={value}>{labels[value]}</option>)}</select><button id="update" type="submit" disabled={busy}>변경</button></div></form></section>
    <section className="detail-section"><h3>저장소 비교</h3><p className="hint" id="sourceLabel">표시된 데이터: {currentSource || 'MariaDB'} · 조회 방식에 따라 이전 값일 수 있습니다.</p><div className="actions"><button id="detail" className="secondary" disabled={busy} onClick={() => action('detail')}>일반 조회</button><button id="fresh" className="secondary" disabled={busy} onClick={() => action('fresh')}>원본 조회</button><button id="inspect" className="secondary" disabled={busy} onClick={() => action('inspect')}>읽기 전용 대조</button></div>
      <label className="monitor-toggle"><input id="watchProjection" type="checkbox" checked={watch} onChange={event => { watchRef.current = event.target.checked; setWatch(event.target.checked); if (event.target.checked && !document.hidden) action('inspect'); }}/>검색 반영 자동 확인 (5초)</label>
      <div id="comparison" className={'notice' + (comparison && !comparable ? ' error' : '')} role="status">{comparisonMessage}</div><div id="comparisonRows" className="storage-comparison" aria-label="저장소별 주문 대조">{comparison && ['mariadb', 'redis', 'elasticsearch'].map(name => {
        const state = comparison[name] || {reachable: false}, snapshot = state.order;
        const verdict = !state.reachable ? '조회 실패' : !snapshot ? '데이터 없음' : name === 'mariadb' ? comparable ? '기준 원본' : '비교 불가' : comparisons[comparable ? state.comparison : 'unknown'] || comparisons.unknown;
        return <div key={name} className={'storage-source' + (!state.reachable ? ' bad' : verdict === '원본과 다름' || verdict === '비교 불가' || !snapshot ? ' warn' : '')}><div className="section-heading"><strong>{name}</strong><span className="badge">{verdict}</span></div><p>{snapshot ? (labels[snapshot.status] || snapshot.status) + ' · v' + snapshot.version : '주문 데이터 확인 불가'}</p>{comparable && state.different_fields?.length > 0 && <small>다른 항목: {state.different_fields.map(field => fields[field] || field).join(', ')}</small>}</div>;
      })}</div>
      <details><summary>조회 방식 안내</summary><p className="hint">일반 조회는 Redis 캐시를 먼저 확인하고, 실패하면 MariaDB로 우회합니다. 원본 조회는 MariaDB를 읽고 캐시도 채웁니다. 읽기 전용 대조는 캐시를 변경하지 않습니다. TTL은 캐시 저장 시점 기준이며 저장소 대조는 원자적 스냅샷이 아닙니다.</p></details>
    </section><details><summary>API 응답 보기</summary><pre id="detailResult">{JSON.stringify(rawResult, null, 2)}</pre></details>
  </Dialog>;
}
