import test from 'node:test';
import assert from 'node:assert/strict';
import { catalog } from './catalog.js';
import { cartStorageKey, loadCart, persistCart, validAcknowledgement } from './cart.js';

const key = '00000000-0000-4000-8000-000000000001';
const product = catalog[0];
const line = { name: product.name, quantity: 2, key };
const order = { id: key, item: product.name, quantity: 2, unit_price: product.price, total: product.price * 2, status: 'paid', version: 2 };

test('reload preserves unresolved payload, request key and confirmed receipt version', () => {
  let saved;
  const storage = { setItem(name, value) { assert.equal(name, cartStorageKey); saved = value; }, getItem() { return saved; } };
  assert.equal(persistCart({ lines: [line], receipts: [order] }, storage), true);
  assert.deepEqual(loadCart(storage), { lines: [line], receipts: [order] });
});

test('untrusted tab storage cannot change catalog prices or introduce invalid lines and receipts', () => {
  const saved = { lines: [{ ...line, unit_price: 1 }, line, { ...line, name: 'unknown' }, { ...line, quantity: 1001 }, { ...line, key: [key] }],
    receipts: [order, { ...order, total: 1 }, { ...order, id: [key] }] };
  assert.deepEqual(loadCart({ getItem: () => JSON.stringify(saved) }), { lines: [line], receipts: [order] });
});

test('unavailable or malformed storage is a bounded empty-cart fallback', () => {
  const broken = { getItem() { throw new Error('unavailable'); }, setItem() { throw new Error('unavailable'); } };
  assert.deepEqual(loadCart(broken), { lines: [], receipts: [] });
  assert.deepEqual(loadCart({ getItem: () => '{broken' }), { lines: [], receipts: [] });
  assert.equal(persistCart({ lines: [line], receipts: [] }, broken), false);
});

test('only an exact submitted payload acknowledgement can remove a cart line', () => {
  assert.equal(validAcknowledgement(order, line, product), true);
  for (const invalid of [{ ...order, quantity: 1, total: product.price }, { ...order, unit_price: 1, total: 2 },
    { ...order, item: 'other' }, { ...order, status: 'unknown' }, { ...order, total: NaN }]) {
    assert.equal(validAcknowledgement(invalid, line, product), false);
  }
});
