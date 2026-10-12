import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useCart } from './hooks/useCart.js';
import { Store } from './components/Store.jsx';
import { Cart } from './components/Cart.jsx';
import { Orders } from './components/Orders.jsx';
import { OrderDetail } from './components/OrderDetail.jsx';
import { Health } from './components/Health.jsx';
import { Guide } from './components/Guide.jsx';

const navigation = [['shop', '스토어'], ['orders', '주문 내역'], ['health', '연결 상태'], ['guide', '실습 가이드']];

export default function App() {
  const [hash, setHash] = useState(location.hash);
  const [selected, setSelected] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [toast, setToast] = useState('');
  const content = useRef(null), toastElement = useRef(null), initial = useRef(true);
  const model = useCart();
  const name = ['cart', 'orders', 'health', 'guide'].includes(hash.slice(1)) ? hash.slice(1) : 'shop';

  useEffect(() => {
    const navigate = () => setHash(location.hash);
    window.addEventListener('hashchange', navigate);
    return () => window.removeEventListener('hashchange', navigate);
  }, []);

  useLayoutEffect(() => {
    setToast('');
    if (initial.current) { initial.current = false; return; }
    const target = hash === '#collection' ? document.getElementById('collection') : document.getElementById(name + 'View').querySelector('h1');
    target.focus({ preventScroll: true });
    if (hash === '#collection') target.scrollIntoView({ block: 'start' });
    else window.scrollTo(0, 0);
  }, [hash, name]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => { if (!toastElement.current.contains(document.activeElement)) setToast(''); }, 5000);
    return () => clearTimeout(timer);
  }, [toast]);

  const openOrder = useCallback((order, source, message = '') => {
    model.confirmReceipt(order, source);
    setSelected({ order, source, message });
  }, [model.confirmReceipt]);
  const confirmOrder = useCallback((order, source, updated = false) => {
    model.confirmReceipt(order, source);
    if (updated) setRefreshKey(value => value + 1);
  }, [model.confirmReceipt]);
  function add(product, quantity) {
    const result = model.add(product, quantity);
    setToast(result.text);
    return result;
  }

  return <>
    <a className="skip-link" href="#content" onClick={event => { event.preventDefault(); content.current.focus({ preventScroll: true }); content.current.scrollIntoView({ block: 'start' }); }}>본문으로 건너뛰기</a>
    <div className="announcement">OBJECTS FOR YOUR EVERYDAY <span>일상을 채우는 작은 취향 · 실습용 스토어</span></div>
    <header className="store-header"><a href="#shop" className="brand" aria-label="오브제 스토어 홈">objet<span>오브제</span></a>
      <nav aria-label="주 메뉴">{navigation.map(([value, label]) => <a key={value} href={'#' + value} aria-current={name === value ? 'page' : undefined}>{label}</a>)}</nav>
      <a className="header-cart" href="#cart" aria-label="장바구니" aria-current={name === 'cart' ? 'page' : undefined}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M5 7h14l1 14H4L5 7Z" /><path d="M8 8V6a4 4 0 0 1 8 0v2" /></svg><span>장바구니</span><span id="cartCount" className="cart-count">{model.cart.lines.reduce((sum, line) => sum + line.quantity, 0)}</span></a>
    </header>
    <main ref={content} id="content" tabIndex="-1"><div className="document">
      <section className="view" id="shopView" hidden={name !== 'shop'}><Store onAdd={add} /></section>
      <section className="view" id="cartView" hidden={name !== 'cart'}><Cart model={model} onOpenOrder={openOrder} /></section>
      <section className="view" id="ordersView" hidden={name !== 'orders'}><Orders active={name === 'orders'} refreshKey={refreshKey} onOpenOrder={openOrder} /></section>
      <section className="view" id="healthView" hidden={name !== 'health'}><Health active={name === 'health'} /></section>
      <section className="view" id="guideView" hidden={name !== 'guide'}><Guide /></section>
    </div></main>
    <footer className="store-footer"><a href="#shop" className="brand">objet</a><span>작은 주문 시스템 · DB Lab</span><span>실습용 스토어 / 실제 결제·배송 없음</span></footer>
    <div ref={toastElement} id="shopNotice" className="shop-toast" role="status" hidden={!toast || model.busy}><span id="shopNoticeText">{toast}</span><a href="#cart">장바구니 보기 →</a></div>
    {selected && <OrderDetail key={selected.order.id} order={selected.order} source={selected.source} initialMessage={selected.message} onClose={() => setSelected(null)} onOrderConfirmed={confirmOrder} />}
  </>;
}
