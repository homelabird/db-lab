export function errorText(error) {
  if (error.name === 'TimeoutError') return '응답 대기 시간이 초과되었습니다. 처리 결과가 확실하지 않으니 연결 상태와 원본을 확인해 주세요.';
  if (error.body?.error === 'api_capacity_exceeded') return '요청이 몰리고 있습니다. 잠시 후 다시 시도해 주세요.';
  if (error.body?.error === 'elasticsearch_search_incomplete') return '검색 응답이 불완전합니다. Elasticsearch 연결 상태를 확인하고 다시 검색해 주세요.';
  if (error.body?.error === 'mariadb_authentication') return 'MariaDB 인증에 실패했습니다. 연결 설정이나 잘못된 암호 실험 상태를 확인해 주세요.';
  if (error.status === 409) return '주문 상태나 버전이 변경되었습니다. 원본 조회 후 다시 확인해 주세요.';
  if (error.status === 404) return '주문을 찾을 수 없습니다. 목록을 새로고침해 주세요.';
  if (error.status === 503) return '저장소에 연결하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.';
  return error.status ? `요청을 처리하지 못했습니다 (${error.status}). 입력값을 확인하고 다시 시도해 주세요.` : '서버 응답을 받지 못했습니다. 연결을 확인하고 다시 시도해 주세요.';
}

export async function request(path, options = {}) {
  const response = await fetch(path, { ...options, signal: options.signal || AbortSignal.timeout(20000) });
  const body = await response.json();
  if (!response.ok) throw Object.assign(new Error(body.error || 'request_failed'), { status: response.status, body });
  return body;
}
