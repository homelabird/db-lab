export const labels = { created: '접수', paid: '결제 완료', shipped: '배송 완료', cancelled: '취소' };
export const transitions = { created: ['paid', 'cancelled'], paid: ['shipped', 'cancelled'], shipped: [], cancelled: [] };
export const number = value => Number(value).toLocaleString('ko-KR');
export const isOrderId = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
