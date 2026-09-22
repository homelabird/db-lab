#!/usr/bin/env python3
"""Realistic, deterministic seed data, shared by the ES 7.x and 9.x labs.

Version-neutral: no imports from lablib or a lab directory, so both
`elasticsearch/scripts/generate_and_load.py` and
`elasticsearch-9/scripts/generate_and_load.py` delegate here and stay in sync.

Guarantees kept from seed-v3 so existing tests and verification still hold:
  * one RNG per (seed, index); batch-size/retries never change the dataset;
  * incident cadence driven by the global document counter (`i % 100`) so the
    first document of every index always carries its known incident
    (transactions: fraud + high risk, web-logs: 502, audit: LOGIN + FAIL);
  * `document_id` == bulk `_id`, unique per index;
  * the per-index source-byte budget is reproduced exactly (budget_for).
"""
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

GENERATOR_VERSION = 'seed-v4-real'
DEFAULT_INDICES = ('lab-transactions-v1', 'lab-web-logs-v1', 'lab-audit-v1',
                   'lab-commerce-v1', 'lab-observability-v1')
DEFAULT_WEIGHTS = (40, 25, 15, 12, 8)

HOUR_WEIGHTS = (1, 1, 1, 1, 1, 2, 3, 5, 6, 8, 10, 12, 12, 10, 8, 7, 8, 10, 12, 14, 12, 9, 6, 3)

# ---------------------------------------------------------------- domain data

CARD_ISSUERS = (
    ('BC카드', 'bc', '3569'), ('삼성카드', 'samsung', '4752'), ('신한카드', 'shinhan', '4017'),
    ('KB국민카드', 'kb', '4213'), ('현대카드', 'hyundai', '4458'), ('롯데카드', 'lotte', '3792'),
    ('우리카드', 'woori', '4374'), ('하나카드', 'hana', '4475'), ('NH농협카드', 'nh', '5412'),
    ('비자(VISA)', 'visa-intl', '4562'), ('마스터카드', 'master-intl', '5577'), ('아멕스', 'amex-intl', '3701'),
)
FX_RATES = {'USD': 1300, 'JPY': 9.2, 'EUR': 1410, 'CNY': 182, 'THB': 37,
            'HKD': 166, 'SGD': 970, 'GBP': 1640, 'VND': 0.054, 'AUD': 860}
FOREIGN = (('US', 'USD'), ('JP', 'JPY'), ('SG', 'SGD'), ('HK', 'HKD'),
           ('TH', 'THB'), ('VN', 'VND'), ('GB', 'GBP'), ('FR', 'EUR'),
           ('CN', 'CNY'), ('AU', 'AUD'), ('DE', 'EUR'))

MERCHANTS = {
    'cafe': ['스타벅스', '투썸플레이스', '커피빈', '이디야커피', '할리스커피'],
    'food': ['배달의민족', '쿠팡이츠', '맥도날드', 'BBQ치킨', '교촌치킨', '롯데리아',
             '파리바게뜨', '서브웨이', '김밥천국', '굽네치킨'],
    'transport': ['티머니', '카카오T', '코레일', 'GS칼텍스'],
    'shopping': ['쿠팡', '올리브영', '다이소', '이마트', '홈플러스', '무신사', 'SSF샵', '와디즈'],
    'online': ['네이버쇼핑', '유튜브프리미엄', '구글', '애플앱스토어', 'Play스토어', '메가스터디'],
    'bills': ['한국전력', 'SK텔레콤', 'KT', 'LG유플러스', '아파트관리비', '건강보험공단'],
    'travel': ['대한항공', '아시아나항공', '제주항공', '호텔신라', '아고다', '야놀자'],
    'medical': ['연세의원', '서울아산병원', '우리동네약국', '굿닥'],
    'game': ['넥슨', '넷마블', '엔씨소프트', '스팀'],
    'entertainment': ['CGV', '메가박스', '넷플릭스', '웨이브', '멜론'],
    'crypto': ['업비트', '빗썸', '고팍스', '바이낸스'],
}
KR_CITIES = ['서울', '부산', '대구', '인천', '광주', '대전', '수원', '제주', '울산', '고양']
CATEGORY_AMOUNT = {
    'cafe': (3500, 12000), 'food': (5000, 45000), 'transport': (1250, 25000),
    'shopping': (8000, 120000), 'online': (5000, 350000), 'bills': (15000, 600000),
    'travel': (40000, 2500000), 'medical': (5000, 500000), 'game': (3000, 150000),
    'entertainment': (8000, 60000), 'crypto': (100000, 5000000),
}
CHANNELS = (('POS', 30), ('APP', 22), ('WEB', 20), ('CPS', 10),
            ('ATM', 8), ('ARS', 5), ('KIOSK', 3), ('OTC', 2))
HIGH_RISK_CHANNELS = ('APP', 'WEB', 'CPS', 'ATM')

SERVICES = {
    'auth': [('GET', '/api/v1/auth/session'), ('POST', '/api/v1/auth/login'),
             ('POST', '/api/v1/auth/token'), ('POST', '/api/v1/auth/mfa/verify')],
    'user': [('GET', '/api/v1/users/{id}'), ('PUT', '/api/v1/users/{id}/preferences'),
             ('GET', '/api/v1/users/{id}/preferences'), ('GET', '/api/v1/users/{id}/orders')],
    'catalog': [('GET', '/api/v1/products'), ('GET', '/api/v1/products/{id}'),
                ('GET', '/api/v1/categories')],
    'search': [('GET', '/api/v1/search'), ('GET', '/api/v1/search/suggest')],
    'payment': [('POST', '/api/v1/payments'), ('GET', '/api/v1/payments/{id}'),
                ('POST', '/api/v1/payments/{id}/refund'), ('POST', '/api/v1/payments/{id}/risk')],
    'checkout': [('POST', '/api/v1/checkout'), ('GET', '/api/v1/cart'),
                 ('POST', '/api/v1/cart/items'), ('POST', '/api/v1/orders'),
                 ('GET', '/api/v1/orders/{id}')],
}
SERVICE_LATENCY = {'auth': (60, 400), 'user': (20, 180), 'catalog': (25, 200),
                   'search': (40, 320), 'payment': (120, 900), 'checkout': (150, 950)}
STATUSES = ((200, 78), (201, 3), (204, 3), (301, 3), (304, 5), (400, 2),
            (401, 3), (403, 2), (404, 5), (422, 2), (429, 2))
ERROR_CODE = {400: 'bad_request', 401: 'unauthorized', 403: 'forbidden',
              404: 'not_found', 422: 'unprocessable', 429: 'rate_limited',
              502: 'upstream_timeout', 503: 'service_unavailable', 504: 'gateway_timeout'}
USER_AGENTS = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
    'okhttp/4.12.0',
    'Googlebot/2.1 (+http://www.google.com/bot.html)',
    'curl/8.7.1',
)
REFERRERS = ('https://www.shop-example.kr/', 'https://m.shop-example.kr/',
             'https://www.google.com/', 'https://pf.kakao.com/', None)

AUDIT_ACTIONS = (('LOGIN', 30), ('LOGOUT', 18), ('CONFIG_READ', 16),
                 ('CONFIG_WRITE', 9), ('MFA_VERIFY', 5), ('PASSWORD_RESET', 5),
                 ('ROLE_CHANGE', 5), ('EXPORT', 4), ('DELETE', 4), ('API_KEY_ROTATE', 4))
PRIVILEGED_ACTIONS = {'ROLE_CHANGE', 'CONFIG_WRITE', 'DELETE', 'API_KEY_ROTATE'}
ROLES = ('viewer', 'operator', 'auditor', 'admin')
FAIL_REASONS = {
    'LOGIN': ['wrong_password', 'unknown_device', 'rate_limited', 'mfa_timeout', 'expired_token'],
    'PASSWORD_RESET': ['unverified_identity', 'rate_limited'],
    'CONFIG_WRITE': ['permission_denied', 'invalid_value'],
    'ROLE_CHANGE': ['insufficient_scope', 'target_not_found'],
    'EXPORT': ['disabled_by_policy', 'approval_pending'],
    'DELETE': ['protected_resource'],
    'API_KEY_ROTATE': ['key_in_use', 'approval_pending'],
    'CONFIG_READ': ['no_such_key'], 'LOGOUT': ['no_active_session'],
    'MFA_VERIFY': ['otp_expired', 'otp_replay'],
}
AUDIT_TARGETS = {'LOGIN': ('session',), 'LOGOUT': ('session',),
                 'CONFIG_READ': ('app-config', 'policy'), 'MFA_VERIFY': ('session', 'mfa-device'),
                 'PASSWORD_RESET': ('account',), 'CONFIG_WRITE': ('app-config', 'policy', 'payment-rule'),
                 'ROLE_CHANGE': ('account',), 'EXPORT': ('report', 'data-export'),
                 'DELETE': ('index', 'account', 'api-key'), 'API_KEY_ROTATE': ('api-key',)}
IDP = ('internal', 'sso-okta', 'gov-cert', 'mfa-device')

CATALOG = (
    ('전자기기', '삼성', '갤럭시 S24', (1100000, 1500000), ['색상: 블랙', '색상: 크림', '색상: 블루']),
    ('전자기기', '애플', '아이폰 15', (1200000, 1600000), ['색상: 블랙', '색상: 화이트']),
    ('전자기기', 'LG전자', 'LG 그램 17', (1600000, 2200000), ['RAM 16GB', 'RAM 32GB']),
    ('전자기기', '삼성', '갤럭시 탭 S9', (800000, 1100000), ['Wi-Fi', '5G']),
    ('전자기기', '애플', '에어팟 프로 2', (320000, 340000), []),
    ('전자기기', '삼성', '갤럭시 스마트워치', (250000, 450000), ['40mm', '44mm']),
    ('패션', '무신사', '오버핏 후드집업', (39000, 89000), ['M', 'L', 'XL']),
    ('패션', '지오다노', '맨투맨', (19900, 29900), ['S', 'M', 'L']),
    ('패션', '나이키', '에어포스 1', (129000, 139000), ['230mm', '240mm', '250mm', '260mm']),
    ('패션', '에이블리', '와이드 팬츠', (12900, 34900), ['S', 'M', 'L']),
    ('뷰티', '올리브영', '선크림 SPF50', (12000, 28000), ['기획세트', '단품']),
    ('뷰티', '라네즈', '수분 크림', (22000, 35000), ['50ml', '75ml']),
    ('뷰티', '인셀덤', '기초 5종 세트', (60000, 120000), []),
    ('식품', '배민', '한우 불고기 세트', (45000, 98000), ['2인분', '3인분']),
    ('식품', '이마트', '올리브오일 500ml', (12000, 26000), []),
    ('가구/홈', '다이소', '수납 정리함', (2000, 8000), ['블랙', '화이트']),
    ('가구/홈', '이케아', '책상 120cm', (99000, 199000), ['오크', '화이트']),
    ('가구/홈', '쿠팡', '뽀송 타올', (9900, 19900), ['10매', '20매']),
    ('도서', '교보문고', '데이터 엔지니어링', (25000, 45000), ['양장본']),
    ('도서', '예스24', '소설 세트', (20000, 35000), ['전 3권']),
    ('생활', '다이슨', '슈퍼소닉 드라이어', (350000, 550000), ['블루', '로즈골드']),
    ('생활', '필립스', '전기면도기', (150000, 320000), []),
)
KR_REGIONS = ['서울 강남구', '서울 마포구', '서울 송파구', '경기 성남시', '경기 수원시', '경기 고양시',
              '인천 연수구', '부산 해운대구', '대구 수성구', '대전 유성구', '광주 광산구', '제주 제주시']
PAY_METHODS = (('card', 55), ('naverpay', 15), ('kakaopay', 15), ('tosspay', 10), ('bank', 5))
PROMOS = ('NEWYEAR2026', 'SPRING26', 'WELCOME10', 'MEMBER_10PCT')
ORDER_STATUS = (('paid', 38), ('packed', 20), ('shipped', 25), ('delivered', 8),
                ('created', 4), ('returned', 2), ('cancelled', 3))
DELIVERY = (('roket', 35), ('parcel', 45), ('quick', 15), ('pickup', 5))

METRICS = {
    'http.server.request.duration': ('ms', ('gateway', 'checkout', 'catalog', 'search'), (30, 900)),
    'http.server.errors': ('rate/sec', ('gateway', 'checkout'), (0, 12)),
    'jvm.gc.pause': ('ms', ('checkout-backend', 'catalog-backend', 'user-backend', 'search-backend'), (3, 120)),
    'heap.usage.percent': ('percent', ('checkout-backend', 'catalog-backend', 'search-backend'), (35, 85)),
    'db.pool.connection.active': ('connections', ('checkout-backend', 'user-backend', 'search-backend'), (4, 60)),
    'queue.duration': ('ms', ('worker', 'worker-image', 'worker-mail'), (5, 300)),
    'node.network.connections': ('connections', ('gateway', 'infra-lb'), (200, 5000)),
}
ENVS = (('prod', 60), ('stage', 25), ('dev', 15))
REGIONS = (('ap-northeast-2', 70), ('us-east-1', 30))
HISTOGRAM_BOUNDS = [10, 50, 100, 500, 1000]
SERVICE_VERSIONS = ('1.4.2', '1.5.0', '2.0.0', '2.3.1')


# ------------------------------------------------------------------ tooling

def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def parse_start(text):
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('--start-date must include Z or a timezone offset')
    return value.astimezone(timezone.utc)


def pick_weighted(rng, pairs):
    total = sum(weight for _, weight in pairs)
    pick = rng.randrange(total)
    for value, weight in pairs:
        pick -= weight
        if pick < 0:
            return value
    return pairs[-1][0]


def budget_for(config, index, indices=DEFAULT_INDICES, weights=DEFAULT_WEIGHTS):
    total = config['total_target_source_bytes']
    if index == indices[2]:
        return total - total * 50 // 100 - total * 32 // 100
    return total * weights[indices.index(index)] // 100


def rng_for(config, index):
    salt = int.from_bytes(hashlib.sha256(f'{config["seed"]}:{index}'.encode()).digest()[:8], 'big')
    return random.Random(salt)


def prune_none(value):
    """Recursively remove None leaves so strict mappings never receive nulls."""
    if isinstance(value, dict):
        return {key: prune_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [prune_none(item) for item in value]
    return value


def pick_timestamp(rng, start, days):
    hour = pick_weighted(rng, tuple((hour, HOUR_WEIGHTS[hour]) for hour in range(24)))
    offset = (rng.randrange(days) * 86400 + hour * 3600 + rng.randrange(60) * 60 + rng.randrange(60))
    return (start + timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z')


def src_ip(rng):
    return f'49.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}'


def client_ip(rng):
    prefix = rng.choice(('49.1', '118.33', '211.115', '203.244', '125.131', '66.249', '52.78', '35.24'))
    return f'{prefix}.{rng.randrange(256)}.{rng.randrange(1, 255)}'


# ------------------------------------------------------------ per-index docs

def transaction(rng, category, i):
    high = category < 5           # fraud: mostly overseas, ~35% domestic KRW
    medium = 5 <= category < 10   # caution: cash-out / new device
    overseas = (rng.random() < 0.65) if high else (category % 13 == 0 and rng.random() < 0.8)
    channel = rng.choice(HIGH_RISK_CHANNELS) if high else pick_weighted(rng, CHANNELS)
    category_key = 'crypto' if high and rng.random() < 0.6 else pick_weighted(
        rng, tuple((key, 1) for key in CATEGORY_AMOUNT))
    low, high_amt = CATEGORY_AMOUNT[category_key]
    amount = rng.randrange(low, high_amt + 1)
    if high:
        amount = max(amount, 750000)
    country, currency = ('KR', 'KRW') if not overseas else rng.choice(FOREIGN)
    fx_currency = currency if currency != 'KRW' else None
    fx_rate = FX_RATES[currency] if currency != 'KRW' else None
    issuer_name, issuer_slug, issuer_bin = rng.choice(CARD_ISSUERS)
    risk_score = rng.randrange(850, 1001) if high else (
        rng.randrange(420, 700) if medium else rng.randrange(8, 220))
    merchant_name = rng.choice(MERCHANTS[category_key])
    if high:
        if currency != 'KRW':
            rule = 'RULE-FX-CRYPTO' if category_key == 'crypto' else 'RULE-FX-NEW-MERCHANT'
        elif category_key == 'crypto':
            rule = 'RULE-CRYPTO-CASH-OUT'
        else:
            rule = 'RULE-DEVICE-VELOCITY'
        decision, message = 'BLOCK', f'fraud rule matched ({rule}): {merchant_name} {category_key} blocked'
    elif medium:
        rule, decision, message = 'RULE-NEW-DEVICE', 'REVIEW', (
            f'review required: {merchant_name} {category_key} from new device')
    else:
        rule, decision, message = None, 'APPROVE', (
            f'card payment approved: {merchant_name} {category_key}')
    return {
        'transaction_id': f'TX{i:010d}',
        'device_id': f'dev-{rng.randrange(12000):06d}' if channel in ('APP', 'WEB', 'CPS') else None,
        'institution': issuer_name, 'card_brand': issuer_slug,
        'card_bin': issuer_bin + str(rng.randrange(1000)), 'approval_number': f'{rng.randrange(10000000):08d}',
        'channel': channel, 'currency': currency, 'country': country,
        'merchant_category': category_key, 'merchant_name': merchant_name,
        'merchant_city': rng.choice(KR_CITIES) if not overseas else '해외',
        'terminal_id': f'TER-{rng.randrange(1000):03d}', 'fx_currency': fx_currency, 'fx_rate': fx_rate,
        'risk_score': risk_score, 'amount': amount, 'is_fraud': high,
        'decision': decision, 'rule_id': rule}


def web_log(rng, category, i):
    failure = category < 5
    if failure:
        service = rng.choice(('payment', 'checkout'))
        method, endpoint = ('POST', '/api/v1/payments') if service == 'payment' else ('POST', '/api/v1/checkout')
        status = 502
        message = 'payment gateway timeout while contacting upstream'
        error_code = 'UPSTREAM_TIMEOUT'
    else:
        service = pick_weighted(rng, tuple((name, 1) for name in SERVICES))
        method, endpoint = rng.choice(SERVICES[service])
        status = pick_weighted(rng, STATUSES)
        message = f'{service} request completed'
        error_code = ERROR_CODE.get(status)
        if status >= 400:
            message = f'{service} request failed: {error_code}'
    path = endpoint.replace('{id}', str(rng.randrange(1, 9000)))
    if status == 200 and service in ('catalog', 'search') and rng.random() < 0.2:
        path += '?page=1&size=20'
    if failure:
        latency_ms = rng.randrange(1500, 12000)
    elif status >= 500:
        latency_ms = rng.randrange(1500, 9000)
    elif status < 400:
        low, high = SERVICE_LATENCY[service]
        latency_ms = rng.randrange(low, high + 1)
    else:
        latency_ms = rng.randrange(5, 400)
    user_agent = rng.choice(USER_AGENTS)
    if 'Googlebot' in user_agent or 'curl' in user_agent:
        device_type = 'bot'
    elif 'Mobile' in user_agent or 'iPhone' in user_agent or 'okhttp' in user_agent:
        device_type = 'mobile'
    else:
        device_type = 'desktop'
    return {
        'request_id': f'REQ{i:010d}', 'method': method, 'path': path,
        'endpoint': endpoint.replace('{id}', '<id>'), 'status': status,
        'latency_ms': latency_ms, 'bytes': rng.randrange(150, 240000),
        'user_agent': user_agent, 'device_type': device_type,
        'cache_hit': rng.random() < 0.55 if method == 'GET' else None,
        'referer': rng.choice(REFERRERS), 'protocol': rng.choice(('HTTP/1.1', 'HTTP/2')),
        'session_id': f'sess-{rng.getrandbits(64):016x}', 'service': service,
        'message': message, 'error_code': error_code}


def audit(rng, category, i):
    action = 'LOGIN' if category < 5 else pick_weighted(rng, AUDIT_ACTIONS)
    result = 'FAIL' if category < 5 else pick_weighted(
        rng, (('SUCCESS', 93), ('FAIL', 5), ('DENIED', 2)))
    reason, message = None, None
    if result != 'SUCCESS':
        reasons = FAIL_REASONS.get(action, ['unknown'])
        reason = rng.choice(reasons)
        message = f'audit {action.lower()} finished with {result.lower()}: {reason}'
    else:
        message = f'audit {action.lower()} finished with success'
    if category < 5:
        message = f'failed login from unfamiliar device: {reason}'
    role_from, role_to = None, None
    if action == 'ROLE_CHANGE':
        role_from, role_to = rng.choice(ROLES), rng.choice(ROLES)
    return {
        'event_id': f'AUD{i:010d}', 'actor': '', 'action': action, 'result': result,
        'target': rng.choice(AUDIT_TARGETS[action]), 'target_id': f'{rng.randrange(100000):06d}',
        'privileged': action in PRIVILEGED_ACTIONS, 'idp': rng.choice(IDP),
        'session_id': f'sess-{rng.getrandbits(48):012x}', 'reason': reason,
        'role_from': role_from, 'role_to': role_to, 'message': message}


def commerce(rng, i):
    storefront = rng.choice(('app', 'web'))
    items = []
    count = 1 + rng.randrange(3) + (1 if i % 9 == 0 else 0)
    for _ in range(min(count, 4)):
        category, brand, name, (low, high), options = rng.choice(CATALOG)
        option = rng.choice(options) if options else None
        quantity = pick_weighted(rng, ((1, 70), (2, 22), (3, 8)))
        price = rng.randrange(low, high + 1)
        items.append({'sku': f'SKU-{rng.randrange(1000):04d}', 'name': name,
                      'brand': brand, 'category': category, 'option': option,
                      'quantity': quantity, 'price': price})
    subtotal = sum(item['price'] * item['quantity'] for item in items)
    promo = rng.choice(PROMOS) if rng.random() < 0.25 else None
    discount = round(subtotal * rng.uniform(0.05, 0.2)) if promo else 0
    shipping_fee = 0 if subtotal >= 20000 else 3000
    total = subtotal - discount + shipping_fee
    ship_country, _ = rng.choice((('KR', 'KRW'),) * 9 + FOREIGN)
    region = rng.choice(KR_REGIONS) if ship_country == 'KR' else '해외 배송'
    method = pick_weighted(rng, PAY_METHODS)
    installments = None
    if method == 'card':
        installments = pick_weighted(rng, ((0, 60), (3, 20), (6, 12), (10, 5), (12, 3)))
    lat = round(rng.uniform(33.4, 38.6), 5) if ship_country == 'KR' else round(rng.uniform(30.0, 45.0), 5)
    lon = round(rng.uniform(126.0, 130.5), 5) if ship_country == 'KR' else round(rng.uniform(-5.0, 140.0), 5)
    return {
        'order_id': f'ORD{i:010d}', 'customer_id': f'cust-{rng.randrange(20000):06d}',
        'storefront': storefront, 'order_status': pick_weighted(rng, ORDER_STATUS),
        'total_amount': total,
        'shipping': {'country': ship_country, 'city': region,
                     'postal_code': f'{rng.randrange(10000, 99999)}',
                     'geo': {'lat': lat, 'lon': lon}},
        'items': items,
        'payment': {'method': method,
                    'issuer': rng.choice(CARD_ISSUERS)[0] if method == 'card' else None,
                    'installments': installments,
                    'masked_pan': f'****{rng.randrange(1000, 9999)}'},
        'promotion': {'code': promo, 'discount': discount},
        'delivery_method': pick_weighted(rng, DELIVERY), 'region': region}


def observability(rng, i):
    metric_name = pick_weighted(rng, tuple((name, 1) for name in METRICS))
    unit, services, (low, high) = METRICS[metric_name]
    service = rng.choice(services)
    minute_of_day = (i % 1440) / 1440.0
    ramp = 0.7 + 0.6 * minute_of_day if metric_name != 'heap.usage.percent' else 1.0
    value = rng.uniform(low, high) * ramp
    anomaly = i % 29 == 0
    if anomaly:
        value *= 5
    env = pick_weighted(rng, ENVS)
    region = pick_weighted(rng, REGIONS)
    return {
        'metric_name': metric_name, 'metric_value': round(value, 4), 'unit': unit,
        'host': {'name': f'app-{rng.randrange(1, 9):02d}',
                 'ip': f'10.20.{rng.randrange(1, 4)}.{rng.randrange(1, 255)}'},
        'service': {'name': service, 'version': rng.choice(SERVICE_VERSIONS)},
        'labels': {'env': env, 'region': region},
        'trace': {'span_id': f'span-{i:012d}', 'sampled': i % 3 != 0},
        'histogram': {'count': rng.randrange(5, 100), 'sum': round(rng.uniform(1, 10000), 2),
                      'bounds': HISTOGRAM_BOUNDS},
        'namespace': 'infra' if service in ('worker', 'worker-image', 'worker-mail', 'infra-lb') else 'app',
        'deployment': service, 'node_name': f'{service}-{rng.randrange(1, 7):02d}',
        'alert': {'severity': 'high' if anomaly else None, 'rule': 'latency-p99' if anomaly else None}}


# ---------------------------------------------------------------- main loop

def documents(config, index, indices=DEFAULT_INDICES, weights=DEFAULT_WEIGHTS):
    idx = indices.index(index)
    rng = rng_for(config, index)
    start = parse_start(config['start_date'])
    target, produced, i = budget_for(config, index, indices, weights), 0, 0
    payload = config.get('payload_bytes') or 0
    salt = int.from_bytes(hashlib.sha256(f'{config["seed"]}:{index}'.encode()).digest()[:8], 'big')
    while produced < target:
        category = i % 100
        doc = {'@timestamp': pick_timestamp(rng, start, config['days']),
               'document_id': f'{index}-{i:010d}', 'event_seq': i,
               'user_id': 'user-00042' if category < 10 else f'user-{rng.randrange(5000):05d}',
               'trace_id': f'trace-{i:010d}', 'src_ip': client_ip(rng),
               'tags': ['seed', 'training'], 'scenario': 'normal', 'message': '',
               'error_code': None}
        if idx == 0:
            doc.update(transaction(rng, category, i))
            if category < 5:
                doc['scenario'] = 'high-risk-payment'
            elif category < 10:
                doc['scenario'] = 'caution-velocity'
        elif idx == 1:
            doc.update(web_log(rng, category, i))
            if category < 5:
                doc['scenario'] = 'payment-timeout'
        elif idx == 2:
            detail = audit(rng, category, i)
            detail['actor'] = doc['user_id']
            doc.update(detail)
            if category < 5:
                doc['scenario'] = 'failed-login'
            elif category < 10:
                doc['scenario'] = 'privileged-change'
        elif idx == 3:
            doc.update(commerce(rng, i))
            if i % 17 == 0:
                doc['scenario'] = 'multi-item-order'
        else:
            doc.update(observability(rng, i))
            if i % 29 == 0:
                doc['scenario'] = 'anomaly-spike'
        if payload:
            doc['payload'] = ''.join(hashlib.sha256(f'{salt}:{i}:{j}'.encode()).hexdigest()
                                     for j in range((payload + 63) // 64))[:payload]
        doc = prune_none(doc)
        source = compact(doc)
        action = compact({'index': {'_index': index, '_id': doc['document_id']}})
        produced += len(source) + 1
        yield action, source
        i += 1