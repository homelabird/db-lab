import { catalog } from './catalog.js';
import { isOrderId, labels } from './orders.js';

export const cartStorageKey = 'objet-cart-v1';

export function validReceipt(order) {
  return Boolean(order && isOrderId(order.id) && typeof order.item === 'string' && order.item.length <= 100
    && Number.isInteger(order.quantity) && order.quantity >= 1 && order.quantity <= 1000
    && Number.isInteger(order.unit_price) && order.unit_price >= 0 && order.unit_price <= 100000000
    && order.total === order.quantity * order.unit_price && Object.hasOwn(labels, order.status)
    && Number.isInteger(order.version) && order.version >= 1);
}

export function loadCart(storage) {
  try {
    const saved = JSON.parse((storage || globalThis.sessionStorage).getItem(cartStorageKey) || '{}');
    const names = new Set();
    const lines = Array.isArray(saved?.lines) ? saved.lines.filter(line => {
      if (!line || !catalog.some(product => product.name === line.name) || names.has(line.name)
        || !Number.isInteger(line.quantity) || line.quantity < 1 || line.quantity > 1000
        || (line.key != null && !isOrderId(line.key))) return false;
      names.add(line.name);
      return true;
    }).map(({ name, quantity, key }) => ({ name, quantity, key: key || null })) : [];
    return { lines, receipts: Array.isArray(saved?.receipts) ? saved.receipts.filter(validReceipt) : [] };
  } catch {
    return { lines: [], receipts: [] };
  }
}

export function persistCart(cart, storage) {
  try {
    (storage || globalThis.sessionStorage).setItem(cartStorageKey, JSON.stringify(cart));
    return true;
  } catch {
    return false;
  }
}

export function validAcknowledgement(order, line, product) {
  return validReceipt(order) && order.item === product.name
    && order.quantity === line.quantity && order.unit_price === product.price;
}
