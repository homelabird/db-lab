# 장애 실습 기록지

## 회차

날짜/시각:
시나리오:
초기 Master:
초기 Replica:
Sentinel 3대 상호 발견과 CKQUORUM 결과:
클라이언트 모드 / RUN_ID:

## 장애 발생과 판단

실행한 장애 주입 명령:
오류가 시작된 시각:
Redis 컨테이너/프로세스 상태:
주요 Redis 로그:
Sentinel의 sdown/odown/선출/switch-master 이벤트:
추정 원인과 그 근거:
배제한 다른 원인:

## 대응과 복구

실행한 조치:
새 Master:
쓰기 성공이 재개된 시각:
기존 Master 복귀 후 역할:
Replica 링크/동기화 완료 여부:
Sentinel 감시/quorum 정상 여부:

## 데이터와 회고

verify의 acknowledged_present:
verify의 acknowledged_missing_or_changed:
uncertain_but_present / uncertain_absent_or_changed:
재시도 시 중복 가능성:
백업/복원 검증 여부:
운영에서 이 조치를 그대로 적용하면 위험한 이유:
다음에 추가 확인할 지표:
