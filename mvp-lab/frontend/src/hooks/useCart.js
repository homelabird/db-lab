import { useCallback, useRef, useState } from 'react';
import { catalog } from '../lib/catalog.js';
import { loadCart, persistCart, validAcknowledgement, validReceipt } from '../lib/cart.js';
import { errorText, request } from '../lib/api.js';

export function useCart() {
  const [cart, setCart] = useState(loadCart);
  const current = useRef(cart);
  const saving = useRef(false);
  const [busy, setBusy] = useState(false);
  const [storageAvailable, setStorageAvailable] = useState(true);
  const [message, setMessage] = useState({ text: '', error: false });

  const commit = useCallback(next => {
    // Persist unresolved request keys before sending, even when React batches renders.
    setStorageAvailable(persistCart(next));
    current.current = next;
    setCart(next);
  }, []);

  const add = useCallback((product, quantity) => {
    const old = current.current;
    const line = old.lines.find(line => line.name === product.name);
    if (saving.current || line?.key) {
      const text = '주문 저장을 확인 중입니다. 장바구니에서 같은 주문을 먼저 재시도해 주세요.';
      setMessage({ text, error: true });
      return { ok: false, text };
    }
    if (!Number.isInteger(quantity) || quantity < 1 || quantity + (line?.quantity || 0) > 1000) {
      return { ok: false, text: '상품별 수량은 1개부터 1,000개까지 담을 수 있습니다.' };
    }
    commit({
      lines: line ? old.lines.map(item => item === line ? { ...item, quantity: item.quantity + quantity } : item)
        : [...old.lines, { name: product.name, quantity, key: null }],
      receipts: old.lines.length ? old.receipts : [],
    });
    setMessage({ text: '', error: false });
    return { ok: true, text: `${product.name} ${quantity}개를 담았습니다.` };
  }, [commit]);

  const changeQuantity = useCallback((name, quantity) => {
    if (saving.current || !Number.isInteger(quantity) || quantity < 1 || quantity > 1000) return;
    const old = current.current;
    commit({ ...old, lines: old.lines.map(line => line.name === name && !line.key ? { ...line, quantity } : line) });
  }, [commit]);

  const remove = useCallback(name => {
    if (saving.current || current.current.lines.some(line => line.name === name && line.key)) return;
    commit({ ...current.current, lines: current.current.lines.filter(line => line.name !== name) });
    setMessage({ text: '', error: false });
  }, [commit]);

  const confirmReceipt = useCallback((order, source) => {
    if (source !== 'mariadb' || !validReceipt(order)) return;
    const old = current.current;
    const receipt = old.receipts.find(receipt => receipt.id === order.id);
    if (receipt && order.version >= receipt.version) {
      commit({ ...old, receipts: old.receipts.map(item => item === receipt ? { ...order } : item) });
    }
  }, [commit]);

  const checkout = useCallback(async () => {
    if (saving.current || !current.current.lines.length) return;
    saving.current = true;
    setBusy(true);
    setMessage({ text: '상품별로 주문을 저장하고 있습니다…', error: false });
    try {
      while (current.current.lines.length) {
        let line = current.current.lines[0];
        const product = catalog.find(product => product.name === line.name);
        if (!line.key) {
          line = { ...line, key: crypto.randomUUID() };
          commit({ ...current.current, lines: [line, ...current.current.lines.slice(1)] });
        }
        const result = await request('/api/orders', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': line.key },
          body: JSON.stringify({ item: product.name, quantity: line.quantity, unit_price: product.price }),
        });
        if (!validAcknowledgement(result.order, line, product)) throw new Error('invalid_order_response');
        commit({ lines: current.current.lines.slice(1), receipts: [...current.current.receipts, result.order] });
      }
      setMessage({ text: '모든 상품의 주문이 저장되었습니다.', error: false });
    } catch (error) {
      setMessage({ text: errorText(error) + ' 저장된 상품은 아래에서 확인하세요. 남은 상품을 같은 요청 키로 다시 시도하면 중복 주문을 방지합니다.', error: true });
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }, [commit]);

  return { cart, busy, message, storageAvailable, add, changeQuantity, remove, confirmReceipt, checkout };
}
