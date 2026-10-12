import test from 'node:test';
import assert from 'node:assert/strict';
import {clearPendingOrder, loadPendingOrder, manualOrderStorageKey, savePendingOrder, validManualOrder} from './manual-order.js';

const data = {item: '테스트 키보드', quantity: 2, unit_price: 30000};
const pending = {body: JSON.stringify(data), key: '00000000-0000-4000-8000-000000000001'};

test('same-tab recovery preserves the exact request body and key until confirmation', () => {
  const values = new Map();
  const storage = {
    getItem: key => values.get(key),
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key)
  };
  assert.equal(savePendingOrder(pending, storage), true);
  assert.ok(values.has(manualOrderStorageKey));
  assert.deepEqual(loadPendingOrder(storage), pending);
  assert.equal(clearPendingOrder(storage), true);
  assert.equal(loadPendingOrder(storage), null);
});

test('tab storage cannot supply malformed keys, bodies or out-of-contract payloads', () => {
  const invalid = [null, [], {...pending, key: [pending.key]}, {...pending, key: 'other'}, {...pending, extra: true},
    {...pending, body: 'broken'}, {...pending, body: '{"item":"테스트 키보드","quantity":2,"quantity":3,"unit_price":30000}'},
    ...[{}, {...data, extra: true}, {...data, quantity: 0}, {...data, quantity: 1001}, {...data, quantity: '2'},
      {...data, quantity: 1.5}, {...data, unit_price: -1}, {...data, unit_price: 100000001}, {...data, unit_price: 0.5},
      {...data, item: ''}, {...data, item: ' padded '}, {...data, item: 'x'.repeat(101)}, {...data, item: '\ud800'}]
      .map(body => ({...pending, body: JSON.stringify(body)}))];
  for (const value of invalid) assert.equal(loadPendingOrder({getItem: () => JSON.stringify(value)}), null);
  assert.equal(validManualOrder(data), true);
  assert.equal(validManualOrder({...data, quantity: 1000, unit_price: 100000000}), true);
  assert.equal(validManualOrder({...data, unit_price: 0}), true);
});

test('unavailable or malformed storage has a safe empty recovery and reports failed writes', () => {
  const broken = {
    getItem() { throw new Error('unavailable'); },
    setItem() { throw new Error('unavailable'); },
    removeItem() { throw new Error('unavailable'); }
  };
  assert.equal(loadPendingOrder(broken), null);
  assert.equal(loadPendingOrder({getItem: () => '{broken'}), null);
  assert.equal(savePendingOrder(pending, broken), false);
  assert.equal(clearPendingOrder(broken), false);
});
