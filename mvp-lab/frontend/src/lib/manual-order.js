import {isOrderId} from './orders.js';

export const manualOrderStorageKey = 'objet-manual-order-v1';

export function validManualOrder(data) {
  return Boolean(data && Object.keys(data).sort().join(',') === 'item,quantity,unit_price'
    && typeof data.item === 'string' && data.item.length > 0 && data.item.length <= 100
    && data.item === data.item.trim() && data.item.isWellFormed()
    && Number.isInteger(data.quantity) && data.quantity >= 1 && data.quantity <= 1000
    && Number.isInteger(data.unit_price) && data.unit_price >= 0 && data.unit_price <= 100000000);
}

export function loadPendingOrder(storage) {
  try {
    const pending = JSON.parse((storage || globalThis.sessionStorage).getItem(manualOrderStorageKey) || 'null');
    if (!pending || Object.keys(pending).sort().join(',') !== 'body,key' || !isOrderId(pending.key) || typeof pending.body !== 'string') return null;
    const data = JSON.parse(pending.body);
    return validManualOrder(data) && JSON.stringify(data) === pending.body ? pending : null;
  } catch { return null; }
}

export function savePendingOrder(pending, storage) {
  try {
    (storage || globalThis.sessionStorage).setItem(manualOrderStorageKey, JSON.stringify(pending));
    return true;
  } catch { return false; }
}

export function clearPendingOrder(storage) {
  try {
    (storage || globalThis.sessionStorage).removeItem(manualOrderStorageKey);
    return true;
  } catch { return false; }
}
