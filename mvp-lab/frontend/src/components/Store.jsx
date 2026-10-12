import { useRef, useState } from 'react';
import { catalog } from '../lib/catalog.js';
import { number } from '../lib/orders.js';
import { Dialog } from './Dialog.jsx';
import { HeroArt } from './HeroArt.jsx';
import { ProductArt } from './ProductArt.jsx';

function ProductDialog({ product, onClose, onAdd }) {
  const [quantity, setQuantity] = useState('1');
  const [message, setMessage] = useState('');
  const valid = Number.isInteger(Number(quantity)) && Number(quantity) >= 1 && Number(quantity) <= 1000;
  return <Dialog id="productDialog" labelledBy="productTitle" onClose={onClose}>
    <div className="dialog-heading"><span className="eyebrow">OBJET COLLECTION</span><button className="close" data-close="productDialog" aria-label="상품 상세 닫기" onClick={onClose}>×</button></div>
    <div className="product-detail-layout">
      <div id="productImage" className={'product-visual art-' + product.id}><ProductArt product={product} /></div>
      <div className="product-detail-copy"><p id="productCaption" className="eyebrow">{product.caption}</p><h2 id="productTitle">{product.name}</h2><p id="productDescription">{product.description}</p><p id="productPrice" className="detail-price">{number(product.price)}원</p>
        <dl className="product-facts"><div><dt>컬렉션</dt><dd id="productCategory">{product.category === 'desk' ? '데스크 · 매일 머무는 공간' : '리빙 · 나를 위한 시간'}</dd></div><div><dt>주문 안내</dt><dd>실습용 상품 · 결제 및 배송 없음</dd></div></dl>
        <form id="productForm" onSubmit={event => {
          event.preventDefault();
          if (valid) {
            const result = onAdd(product, Number(quantity));
            if (result.ok) onClose();
            else setMessage(result.text);
          }
        }}>
          <label htmlFor="productQuantity">수량</label><input id="productQuantity" type="number" min="1" max="1000" required value={quantity} onChange={event => setQuantity(event.target.value)} />
          <div className="checkout-total"><span>상품 합계</span><output id="productTotal" htmlFor="productQuantity">{valid ? number(Number(quantity) * product.price) + '원' : '수량을 확인하세요'}</output></div>
          <p id="productMessage" className={'notice' + (message ? ' error' : '')} role="status">{message}</p><button id="addToCart" type="submit">장바구니에 담기</button>
        </form>
      </div>
    </div>
  </Dialog>;
}

export function Store({ onAdd }) {
  const [category, setCategory] = useState('all');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('curated');
  const [product, setProduct] = useState(null);
  const search = useRef(null);
  const products = catalog.filter(product => (category === 'all' || product.category === category)
    && product.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  if (sort !== 'curated') products.sort((a, b) => sort === 'low' ? a.price - b.price : b.price - a.price);

  return <>
    <div className="hero"><div className="hero-copy"><p className="eyebrow">THE EVERYDAY COLLECTION · 01</p><h1 tabIndex="-1">일상을 바꾸는,<br />작은 취향.</h1><p>빛이 머무는 책상, 따뜻한 커피 한 잔.<br />매일의 순간을 더 좋아하게 만드는 물건들.</p><a href="#collection" className="shop-cta">지금 쇼핑하기 <span aria-hidden="true">→</span></a><span className="hero-note">천천히 고르고, 오래 좋아하는 것들.</span></div><HeroArt /></div>
    <div className="shop-benefits" aria-label="쇼핑 안내"><span><strong>01</strong> 마음에 드는 상품 찾기</span><span><strong>02</strong> 장바구니에서 함께 확인</span><span><strong>03</strong> 주문 내역으로 이어지는 쇼핑</span></div>
    <div className="collection-heading" id="collection" tabIndex="-1"><div><p className="eyebrow">CURATED ESSENTIALS</p><h2>매일 함께하고 싶은 것들</h2></div><span className="hint">실습용 상품 컬렉션 · 실제 결제 없음</span></div>
    <div className="catalog-toolbar"><div className="category-tabs" role="group" aria-label="상품 카테고리">{[['all', '전체'], ['desk', '데스크'], ['living', '리빙']].map(([value, label]) => <button key={value} data-category={value} aria-pressed={category === value} onClick={() => setCategory(value)}>{label}</button>)}</div><label className="catalog-search" htmlFor="catalogQuery"><span>상품 찾기</span><input ref={search} id="catalogQuery" type="search" placeholder="어떤 물건을 찾으세요?" maxLength="100" value={query} onChange={event => setQuery(event.target.value)} /></label></div>
    <div className="catalog-meta"><span id="catalogCount" role="status">상품 {products.length}개</span><label htmlFor="catalogSort">정렬 <select id="catalogSort" value={sort} onChange={event => setSort(event.target.value)}><option value="curated">추천순</option><option value="low">낮은 가격순</option><option value="high">높은 가격순</option></select></label></div>
    <div id="products" className="products">{products.map(product => <article key={product.id} className="product">
      <button className={'product-visual art-' + product.id} aria-label={product.name + ' 상품 이미지 자세히 보기'} onClick={() => setProduct(product)}><ProductArt product={product} /></button>
      <p className="product-category">{product.caption}</p><h3><button className="product-title" onClick={() => setProduct(product)}>{product.name}</button></h3><p className="product-description">{product.description}</p>
      <div className="product-bottom"><span className="product-price">{number(product.price)}<small>원</small></span><button className="product-buy" aria-label={product.name + ' 자세히 보기'} onClick={() => setProduct(product)}>자세히 보기</button><button className="product-add" aria-label={product.name + ' 장바구니에 담기'} onClick={() => onAdd(product, 1)}>담기 +</button></div>
    </article>)}</div>
    <div id="catalogEmpty" className="empty" hidden={products.length > 0}><strong>찾으시는 상품이 없어요</strong><p id="catalogEmptyHint" role="status">{query.trim() ? `“${query.trim()}”에 맞는 상품이 없습니다. 검색 조건을 바꿔보세요.` : '선택한 카테고리에 상품이 없습니다. 전체 상품을 확인해 보세요.'}</p><button id="resetCatalog" className="secondary" onClick={() => { setCategory('all'); setQuery(''); setSort('curated'); search.current.focus(); }}>전체 상품 보기</button></div>
    <div className="store-note"><span>작은 물건, 오래가는 취향.</span><p>원하는 물건을 담고, 나만의 컬렉션을 만들어 보세요.<br />실제 결제·배송 없이 주문 흐름을 경험할 수 있습니다.</p><a href="#cart">장바구니 확인 →</a></div>
    <aside className="start-guide" aria-label="실습 시작 안내"><div><strong>주문 뒤에는 저장소도 살펴보세요.</strong><p>주문 상세의 저장소 대조 → 같은 주문으로 장애 전후 비교</p></div><a href="#guide">DB Lab 실습 가이드 ↗</a></aside>
    {product && <ProductDialog product={product} onClose={() => setProduct(null)} onAdd={onAdd} />}
  </>;
}
