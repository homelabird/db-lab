# 07 · 제공 범위와 의도적인 제외

## 제공

기본 Redis 3대·Sentinel 3대 또는 Redis Cluster 3~100대·클라이언트 1대, 인증, 영속 설정/데이터, 상태/읽기/쓰기 검증,
합성 데이터 생성, 자료구조 실습, Master/Replica/Sentinel 중단, pause, 노드 전체 network 단절,
Replica 인증 오류, Sentinel 다수 상실 수동 절차, 고정/Sentinel 클라이언트 비교,
요청별 JSONL와 쓰기 결과 비교, 진단 수집, 별도 sandbox의 maxmemory/noeviction 및 RDB/MISCONF,
현재 Master RDB backup, sandbox에만 복원, 자동 기본 failover 시험을 제공합니다.

## 제공하지 않는 기능

웹 UI/대시보드, Prometheus/Grafana, Cluster 전용 클라이언트 라우팅과 장애조치 자동 검증,
운영용 ACL/TLS/비밀번호 rotation,
호스트 자체 장애 내성, 여러 물리 호스트의 quorum, 비대칭 network partition 정밀 재현,
운영 데이터 자동 복구, 손상 AOF 강제 수정, host/container OOM 강제 주입은 포함하지 않습니다.
네트워크 지연/패킷 손실을 특정 경로에만 주입하는 tc/proxy 구성도 포함하지 않습니다.

한 bridge에서 한 노드를 완전히 분리하는 실습은 구 Master에 일부 클라이언트만 남아 있는 split-brain
상황과 같지 않습니다. Sentinel은 분산 환경의 모든 데이터 손실 가능성을 제거하지 않습니다.

## 버전과 배포

Redis base image: `docker.io/library/redis:7.4.11-alpine` — 패치 버전 태그 고정.
Python client library: `redis==6.4.0` — 패키지 버전 고정.
Python base image: `docker.io/library/python:3.12-slim-bookworm` — minor/배포판 고정, patch와 image digest는 고정하지 않음.

“모든 의존성이 byte-for-byte 재현된다”거나 “현재 최신 버전이다”라고 주장하지 않습니다.
Redis 7.4 세대는 운영 기본 개념과 Sentinel 실습을 위한 선택입니다. 배포 시점의 이미지 유지/보안 상태는
사용 목적에 맞게 별도로 검토해야 합니다. 외부 Redis/Python/redis-py의 라이선스는 각각의 프로젝트를 따릅니다.

## 데이터와 부하

default seed는 사용자 2,000명과 관련 세션/집합/순위/이벤트를 생성합니다.
대용량 원본 데이터 파일을 ZIP에 넣지 않았고 실행 시 생성합니다. payload 옵션은 저장 값 크기이지
실제 RSS나 Redis 메모리 사용량 보장이 아닙니다.
시드에는 입력 크기 제한, workload에는 총 입력량 제한이 있습니다. 이는 호스트 보호를 위한 학습용 한도입니다.
