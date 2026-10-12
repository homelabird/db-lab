import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { catalog } from '../lib/catalog.js';
import { labels, number } from '../lib/orders.js';
import { ProductArt } from './ProductArt.jsx';

function CartLine({ line, busy, onQuantity, onRemove, onValidity }) {
  const product = catalog.find(product => product.name === line.name);
  const [quantity, setQuantity] = useState(String(line.quantity));
  const valid = Number.isInteger(Number(quantity)) && Number(quantity) >= 1 && Number(quantity) <= 1000;
  useEffect(() => { setQuantity(String(line.quantity)); onValidity(line.name, true); }, [line.name, line.quantity, onValidity]);
  return <article className="cart-line">
    <div className={'product-visual art-' + product.id}><ProductArt product={product} /></div>
    <div><h3>{product.name}</h3><p>{number(product.price)}원 / 개</p><label>수량<input type="number" min="1" max="1000" required value={quantity}
      data-cart-quantity={product.name} aria-label={product.name + ' 수량'} aria-describedby="cartInputMessage" aria-invalid={!valid}
      disabled={busy || Boolean(line.key)} onChange={event => {
        setQuantity(event.target.value);
        onValidity(line.name, event.target.validity.valid);
        if (event.target.validity.valid) onQuantity(line.name, Number(event.target.value));
      }} /></label>{line.key && <small>저장 결과 확인 중 · 같은 요청으로 재시도</small>}</div>
    <div><strong className={valid ? undefined : 'quantity-error'}>{valid ? number(product.price * line.quantity) + '원' : '수량 확인 필요'}</strong><button data-cart-remove={product.name} aria-label={product.name + ' 장바구니에서 삭제'} disabled={busy || Boolean(line.key)} onClick={() => onRemove(line.name)}>삭제</button></div>
  </article>;
}

export function Cart({ model, onOpenOrder }) {
  const { cart, busy, message, storageAvailable, changeQuantity, remove, checkout } = model;
  const [invalidNames, setInvalidNames] = useState(new Set());
  const items = useRef(null), receiptTitle = useRef(null), emptyLink = useRef(null), checkoutButton = useRef(null);
  const nextFocus = useRef(null), wasBusy = useRef(false);
  const onValidity = useCallback((name, valid) => setInvalidNames(old => {
    if (old.has(name) === !valid) return old;
    const next = new Set(old);
    if (valid) next.delete(name); else next.add(name);
    return next;
  }), []);
  const invalid = cart.lines.some(line => invalidNames.has(line.name));
  const quantity = cart.lines.reduce((sum, line) => sum + line.quantity, 0);
  const total = cart.lines.reduce((sum, line) => sum + catalog.find(product => product.name === line.name).price * line.quantity, 0);
  const step = busy ? 'checkoutStep' : cart.lines.length ? 'cartStep' : cart.receipts.length ? 'receiptStep' : null;

  useLayoutEffect(() => {
    if (nextFocus.current !== null) {
      const inputs = [...items.current.querySelectorAll('input:not(:disabled)')];
      const target = inputs[Math.min(nextFocus.current, inputs.length - 1)]
        || (cart.lines.length ? checkoutButton.current : cart.receipts.length ? receiptTitle.current : emptyLink.current);
      target?.focus();
      nextFocus.current = null;
    }
    if (wasBusy.current && !busy && !cart.lines.length) receiptTitle.current?.focus();
    wasBusy.current = busy;
  }, [cart.lines, cart.receipts.length, busy]);

  return <>
    <header><p className="eyebrow">YOUR COLLECTION</p><h1 tabIndex="-1">장바구니</h1><p>고른 물건을 한 번 더 살펴보세요.</p></header>
    <ol className="checkout-steps" aria-label="주문 단계">{[['cartStep', '상품 확인'], ['checkoutStep', '실습 주문'], ['receiptStep', '주문 완료']].map(([id, label], index) => <li key={id} id={id} aria-current={step === id ? 'step' : undefined}><span>{index + 1}</span> {label}</li>)}</ol>
    <div id="cartMessage" className={'notice' + (message.error ? ' error' : '')} role="status">{message.text}</div>
    <div id="cartInputMessage" className="notice error" role="status">{invalid ? '상품별 수량을 1개부터 1,000개까지 입력해 주세요.' : ''}</div>
    <div className="cart-layout" id="cartLayout" hidden={!cart.lines.length}>
      <div><div className="cart-list-heading"><h2>담은 상품</h2><a href="#collection">쇼핑 계속하기 →</a></div>
        <div id="cartItems" ref={items}>{cart.lines.map(line => <CartLine key={line.name} line={line} busy={busy} onQuantity={changeQuantity} onValidity={onValidity} onRemove={name => {
          nextFocus.current = cart.lines.findIndex(line => line.name === name);
          remove(name);
        }} />)}</div>
        <p id="cartPersistence" className="hint">{storageAvailable ? '장바구니는 현재 브라우저 탭에 보관됩니다.' : '브라우저 보관을 사용할 수 없습니다. 새로고침하면 장바구니와 재시도 정보가 사라집니다.'}</p>
      </div>
      <aside className="order-summary" aria-labelledby="summaryTitle"><p className="eyebrow">ORDER SUMMARY</p><h2 id="summaryTitle">주문 요약</h2><dl><div><dt>상품 수량</dt><dd id="cartQuantity">{invalid ? '수량 확인 필요' : number(quantity) + '개'}</dd></div><div><dt>상품 금액</dt><dd id="cartSubtotal">{invalid ? '수량 확인 필요' : number(total) + '원'}</dd></div><div><dt>배송</dt><dd>실습 · 배송 없음</dd></div><div className="summary-total"><dt>주문 합계</dt><dd id="cartTotal">{invalid ? '수량 확인 필요' : number(total) + '원'}</dd></div></dl>
        <button ref={checkoutButton} id="checkoutButton" disabled={busy || !cart.lines.length || invalid} onClick={() => {
          if ([...items.current.querySelectorAll('input')].every(input => input.reportValidity())) checkout();
        }}>{busy ? '주문을 저장하고 있습니다…' : cart.lines.some(line => line.key) ? '남은 상품 주문 재시도' : '실습 주문 확정'}</button><p className="hint">실제 결제·배송은 발생하지 않습니다.<br />여러 상품은 상품별 주문으로 저장됩니다.</p>
      </aside>
    </div>
    <div id="cartEmpty" className="empty" hidden={Boolean(cart.lines.length || cart.receipts.length)}><div className="empty-symbol" aria-hidden="true">♧</div><strong>아직 담은 상품이 없어요</strong><p>매일 함께하고 싶은 물건을 찾아보세요.</p><a ref={emptyLink} href="#collection" className="shop-cta">상품 둘러보기 →</a></div>
    <section id="receipt" className="receipt" aria-labelledby="receiptTitle" hidden={!cart.receipts.length}><div className="receipt-mark" aria-hidden="true">✓</div><p className="eyebrow">THANK YOU FOR YOUR ORDER</p><h2 ref={receiptTitle} id="receiptTitle" tabIndex="-1">{cart.lines.length ? '저장된 주문을 확인하세요.' : '주문이 접수되었습니다.'}</h2><p id="receiptNote">{cart.lines.length ? '아래 상품은 저장되었습니다. 남은 상품은 위 장바구니에서 확인하세요.' : '고른 상품을 상품별 주문으로 저장했습니다. 상세에서 처리 상태를 확인할 수 있습니다.'}</p>
      <div className="receipt-total"><span>저장된 주문 합계</span><output id="receiptTotal">{number(cart.receipts.reduce((sum, order) => sum + order.total, 0))}원</output></div>
      <div id="receiptItems">{cart.receipts.map(order => <div key={order.id} data-receipt-id={order.id} className="receipt-line"><div><strong>{order.item}</strong><p>{number(order.quantity)}개 · {number(order.total)}원</p><span className={'receipt-state badge ' + order.status}>{labels[order.status]} · v{order.version}</span><small>주문 ID {order.id}</small></div><button className="secondary receipt-order" aria-label={order.item + ' 주문 상세 보기'} onClick={() => onOpenOrder(order, 'mariadb')}>주문 상세 보기</button></div>)}</div>
      <p className="hint">마지막으로 확인한 원본 기준입니다. 주문 상세의 원본 조회로 최신 상태를 확인하세요.</p><div className="actions"><a href="#orders" className="shop-cta">주문 내역 확인 →</a><a href="#collection" className="text-link">쇼핑 계속하기</a></div>
    </section>
  </>;
}
