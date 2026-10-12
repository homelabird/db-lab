import {useEffect, useRef, useState} from 'react';
import {Dialog} from './Dialog.jsx';
import {errorText, request} from '../lib/api.js';
import {number} from '../lib/orders.js';
import {validReceipt} from '../lib/cart.js';
import {clearPendingOrder, loadPendingOrder, savePendingOrder, validManualOrder} from '../lib/manual-order.js';

const emptyDraft = {item: '', quantity: '1', price: '30000'};

export function CreateOrder({open, onClose, onSaved}) {
  const [pending, setPending] = useState(loadPendingOrder);
  const [draft, setDraft] = useState(() => {
    if (!pending) return emptyDraft;
    const data = JSON.parse(pending.body);
    return {item: data.item, quantity: String(data.quantity), price: String(data.unit_price)};
  });
  const [message, setMessage] = useState('');
  const [failed, setFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [storageAvailable, setStorageAvailable] = useState(true);
  const pendingRef = useRef(pending), busy = useRef(false), itemInput = useRef(null), openRef = useRef(open);
  openRef.current = open;
  useEffect(() => {
    if (open) {
      setMessage(pendingRef.current ? '이전에 보낸 주문의 저장 결과를 확인해 주세요. 같은 요청으로 재시도합니다.' : '');
      setFailed(false); itemInput.current?.focus();
    }
  }, [open]);
  const updateDraft = event => { if (!pendingRef.current) setDraft(current => ({...current, [event.target.name]: event.target.value})); };
  const quantity = Number(draft.quantity), price = Number(draft.price);
  const validTotal = draft.quantity !== '' && draft.price !== '' && Number.isInteger(quantity) && quantity >= 1 && quantity <= 1000 && Number.isInteger(price) && price >= 0 && price <= 100000000;

  async function save(event) {
    event.preventDefault();
    if (busy.current || !event.currentTarget.reportValidity()) return;
    const data = pendingRef.current ? JSON.parse(pendingRef.current.body) : {item: draft.item.trim(), quantity, unit_price: price};
    if (!validManualOrder(data)) { setFailed(true); setMessage('상품 이름과 수량·단가를 확인해 주세요.'); return; }
    const originHash = window.location.hash;
    busy.current = true; setSaving(true); setFailed(false);
    if (!pendingRef.current) {
      pendingRef.current = {body: JSON.stringify(data), key: crypto.randomUUID()};
      setPending(pendingRef.current); setStorageAvailable(savePendingOrder(pendingRef.current));
      setDraft({item: data.item, quantity: String(data.quantity), price: String(data.unit_price)});
    }
    setMessage('주문을 저장하고 있습니다…');
    try {
      const result = await request('/api/orders', {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': pendingRef.current.key}, body: pendingRef.current.body});
      if (!validReceipt(result.order) || result.order.item !== data.item || result.order.quantity !== data.quantity || result.order.unit_price !== data.unit_price) throw new Error('invalid_order_response');
      if (!openRef.current || window.location.hash !== originHash) {
        setMessage('주문을 저장했습니다. 같은 요청으로 재시도하면 저장된 주문을 확인합니다.');
        return;
      }
      onSaved(result);
      setStorageAvailable(clearPendingOrder()); pendingRef.current = null; setPending(null); setDraft(emptyDraft);
    } catch (error) {
      setFailed(true); setMessage(errorText(error) + ' 같은 내용으로 다시 저장하면 중복 주문을 방지합니다.');
    } finally { busy.current = false; setSaving(false); }
  }

  if (!open) return null;
  return <Dialog id="createDialog" labelledBy="createTitle" onClose={onClose} onCancel={() => !busy.current}>
    <div className="dialog-heading"><h2 id="createTitle">주문하기</h2><button className="close" data-close="createDialog" aria-label="새 주문 닫기" disabled={saving} onClick={onClose}>×</button></div>
    <p className="hint">수량과 금액을 확인하세요. 실제 결제는 발생하지 않습니다.</p>
    <p className="hint">저장 결과가 불확실할 때는 같은 내용으로 재시도하세요. 새로고침 뒤에도 같은 탭에서 직접 주문 입력을 열면 요청을 이어서 확인합니다.</p>
    {!storageAvailable && <p className="notice error" role="status">브라우저 보관을 사용할 수 없습니다. 새로고침하면 재시도 정보가 사라집니다. 이 화면에서 같은 요청으로 재시도하세요.</p>}
    <form id="createForm" onSubmit={save}>
      <div id="customItem"><label htmlFor="item">상품 이름</label><input ref={itemInput} id="item" name="item" placeholder="예: 연습용 키보드" maxLength="100" required value={draft.item} onChange={updateDraft} disabled={saving || Boolean(pending)}/></div>
      <div className="split"><div><label htmlFor="quantity">수량</label><input id="quantity" name="quantity" type="number" min="1" max="1000" required value={draft.quantity} onChange={updateDraft} disabled={saving || Boolean(pending)}/></div><div id="customPrice"><label htmlFor="price">단가 (원)</label><input id="price" name="price" type="number" min="0" max="100000000" required value={draft.price} onChange={updateDraft} disabled={saving || Boolean(pending)}/></div></div>
      <div className="checkout-total"><span>주문 합계</span><output id="checkoutTotal" htmlFor="quantity price">{validTotal ? number(quantity * price) + '원' : '수량과 단가를 확인하세요'}</output></div>
      <div className={'notice' + (failed ? ' error' : '')} id="createMessage" role="status">{message}</div>
      <div className="actions"><button type="button" className="secondary" data-close="createDialog" disabled={saving} onClick={onClose}>취소</button><button id="saveOrder" type="submit" disabled={saving}>주문 확정</button></div>
    </form>
  </Dialog>;
}
