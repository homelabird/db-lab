import {useCallback, useEffect, useRef, useState} from 'react';
import {errorText, request} from '../lib/api.js';
import {number} from '../lib/orders.js';

const roles = {mariadb: '주문 원본', redis: '상세 조회 캐시', elasticsearch: '주문 검색', kafka: '이벤트 전달'};
const impacts = {mariadb: '신규 주문과 원본 조회가 실패할 수 있습니다.', redis: '일반 조회는 MariaDB로 우회합니다.', elasticsearch: '검색은 실패하지만 원본 주문은 조회할 수 있습니다.', kafka: '주문은 저장되지만 새 검색 반영이 지연될 수 있습니다.'};

export function Health({active}) {
  const [result, setResult] = useState(null), [rawResult, setRawResult] = useState(null), [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('아직 진단하지 않았습니다.'), [failed, setFailed] = useState(false), [watch, setWatch] = useState(false);
  const inFlight = useRef(false), mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const loadDiagnostics = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setResult(null); setFailed(false); setMessage('연결 상태를 확인하고 있습니다…');
    try {
      const data = await request('/api/diagnostics');
      if (!mounted.current) return;
      setResult(data); setRawResult(data); setFailed(!data.dependencies_reachable);
      const waiting = data.dependencies.mariadb?.outbox_pending > 0 || data.dependencies.kafka?.lag > 0;
      setMessage(new Date().toLocaleTimeString('ko-KR') + ' 기준 · ' + (data.dependencies_reachable ? '모든 저장소에 연결할 수 있습니다.' : '일부 저장소에 연결할 수 없습니다.') + (waiting ? ' 처리 대기가 있습니다. 주문별 검색 반영을 대조해 주세요.' : ''));
    } catch (error) {
      if (!mounted.current) return;
      setFailed(true); setMessage(errorText(error)); setRawResult(error.body || {error: error.message});
    } finally { inFlight.current = false; if (mounted.current) setBusy(false); }
  }, []);
  useEffect(() => { if (active) loadDiagnostics(); }, [active, loadDiagnostics]);
  useEffect(() => {
    if (!active || !watch) return;
    const timer = setInterval(() => { if (!document.hidden) loadDiagnostics(); }, 5000);
    return () => clearInterval(timer);
  }, [active, watch, loadDiagnostics]);
  return <>
    <header><div className="section-heading"><h1 tabIndex="-1">연결 상태</h1><button id="diagnostics" className="secondary" disabled={busy} onClick={loadDiagnostics}>진단 새로고침</button></div><p>저장소 연결과 처리 대기를 확인하세요.</p></header>
    <div className={'notice' + (failed ? ' error' : '')} id="healthMessage" role="status">{message}</div>
    <label className="monitor-toggle"><input id="watchHealth" type="checkbox" checked={watch} onChange={event => { setWatch(event.target.checked); if (event.target.checked && active && !document.hidden) loadDiagnostics(); }}/>5초마다 연결 상태 새로고침</label>
    <div className="service-list" id="services">{result && Object.entries(result.dependencies).map(([name, state]) => <div key={name} className="service"><div>{name}<small>{roles[name]}</small>{!state.reachable && <small>{impacts[name]}</small>}</div><span className={'badge ' + (state.reachable ? 'paid' : 'bad')}>{state.reachable ? '연결 가능' : '연결 실패'}</span></div>)}</div>
    <div className="metric-row" id="metrics">{result && [['이벤트 전송 대기', result.dependencies.mariadb?.outbox_pending], ['검색 소비 대기', result.dependencies.kafka?.lag]].map(([label, value]) => <div key={label}><strong>{value == null ? '—' : number(value)}</strong><span>{label}</span></div>)}</div>
    <p className="hint">연결 성공이 전체 주문 처리의 정상 동작을 보장하지는 않습니다. worker 동작은 실습 가이드의 smoke 명령으로 확인하세요.</p>
    <details><summary>진단 응답 보기</summary><pre id="diagnosticsResult">{rawResult ? JSON.stringify(rawResult, null, 2) : '아직 진단하지 않았습니다.'}</pre></details>
  </>;
}
