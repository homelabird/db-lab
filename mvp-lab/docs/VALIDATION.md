# MVP 추가 작업과 검증 범위

기준일: 2026-09-18. 입력: 대화에서 제공된 `db-lab-remediated.zip`.
이번 결과는 **새 코드가 들어 있는 프로젝트 사본**입니다. 사용자 호스트·기존 DB·클러스터에 접속하거나 배포하지 않았습니다.

## 구현된 것

별도 `mvp-lab/`에 실제 DB 드라이버를 사용하는 Python API/worker, HTML UI, 여섯 컨테이너 Compose,
주문+outbox 트랜잭션, ACK 후 전송 표시, ES 반영 후 수동 offset commit, 캐시 우회, 버전 기반 재처리를 추가했습니다.
세 개의 설정/대상 교체 모드와 컨테이너 중단·복원·재생성, 비파괴 검색 재구축, 실제 HTTP smoke 명령도 포함합니다.

기존 파일 중 수정한 것은 루트 `all.sh`와 `README.md`뿐입니다. 기존 349개 파일 중 나머지 347개는 입력 ZIP과
바이트 단위로 같으며, 기존 네 DB 실습 및 Helm의 기능·설정·데이터 경로를 변경하지 않았습니다.
새 기능은 일반 `all.sh up/down`의 기본 실행 대상에 포함하지 않았습니다.

## 이번 환경에서 실행한 검증

| 검증 | 결과 | 의미 |
|---|---:|---|
| 새 MVP unittest | **109개 통과** | 업무 로직, 예외, 드라이버 호출 계약, SQL DML/rollback, HTTP, 제어 명령, YAML |
| 기존 루트 unittest | **91개 통과** | 기존 루트 라우팅·보호 장치 및 차트 subset 계약 회귀 |
| 이번에 재실행한 unittest 합계 | **200개 통과** | 기존 하위 DB 553개를 이번 실행 수에 다시 더하지 않음 |
| 새 MVP Python 구문 | **15개 파일 통과** | AST 구문 검사, DB 드라이버 실제 실행 검사 아님 |
| Bash 구문 | **2개 파일 통과** | 변경 all.sh와 신규 lab.sh |
| UI JavaScript 구문 | **1개 통과** | Node 구문 검사 |
| Chromium UI | **7개 체크 통과** | 정적 HTML/JS + 테스트 전용 Python 바인딩, 모의 저장소 |
| 실제 엔진 사전 검사 | init 0 / doctor **1** | 엔진 부재를 정상 기동으로 오인하지 않고 실패 반환 |

MVP에는 제어기·HTTP·메시지 전달을 위한 fake 객체 테스트가 있습니다. SQL DML 일부는 실제 저장소 클래스에 SQLite
테스트 어댑터를 연결해 검사합니다. 이 어댑터는 placeholder와 일부 구문을 변환하고 `FOR UPDATE`를 제거하므로,
**MariaDB 구문·격리 수준·행 잠금·영속성 시험으로 해석하면 안 됩니다.**

HTTP 테스트는 실제 loopback 소켓을 사용하지만 DB 의존성은 테스트용 메모리 저장소입니다.
Chromium의 loopback 페이지 탐색은 이 작업 환경의 정책으로 차단됐습니다. 정책을 변경하지 않고 로컬 HTML을 직접 렌더링한 뒤
명시적인 테스트 바인딩으로 주문·상태 변경·캐시 우회·검색·진단·목록·모바일 화면을 점검했습니다.
따라서 UI 통과 역시 브라우저→실 DB 통합 검증이 아닙니다. 미리보기는 이러한 모의 데이터로 만든 화면입니다.

기존 루트 테스트의 첫 호출은 도구 실행 시간 제한으로 중간에 종료됐습니다. 이후 더 긴 제한의 별도 현재 작업 실행에서
91개 전체가 통과했습니다. 차트 테스트는 기존 Go 템플릿 subset이며 Helm 실행으로 집계하지 않았습니다.
초기 패키징 검사에서 UI 실행이 만든 Python bytecode 캐시를 발견해 제거했으며 배포 ZIP에는 포함하지 않습니다.

## 실행하지 못한 것

Docker, Podman, Compose provider, Helm, kubectl이 이 환경에 없었습니다. 네트워크 DNS 제약으로 패키지 설치도 수행하지 않았습니다.
**실제 이미지 빌드/pull, 여섯 컨테이너 기동, 드라이버 대 실제 DB 연결, 실제 Kafka 전달/복구,
데이터 볼륨 보존, 다른 CPU 아키텍처 및 엔진 버전 호환성은 검증 대기입니다.**

실제 이미지 digest, 취약점 검사, 성능 수치, HA/RTO/RPO 결과를 생성하지 않았습니다.
UI의 시간·주문 ID나 모의 테스트의 수행 시간도 운영 지연 또는 DB 성능 수치가 아닙니다.

## 사용자 환경에서 완료할 최소 인수 시험

```bash
./all.sh mvp init
./all.sh mvp doctor
./all.sh mvp up
./all.sh mvp smoke
```

먼저 새 데이터로 위 경로를 통과시킵니다. 다음 순서로 각각 실제로 확인해야 합니다.

1. Redis 중단 후 상세 SQL 우회, 복원 후 다시 cache HIT.
2. Kafka 중단 중 새 주문 저장/outbox 증가, 복원 후 같은 주문의 검색 반영.
3. ES 중단 중 검색 503/consumer lag, 복원 후 누락 없이 최신 version 반영.
4. MariaDB 중단 중 신규 주문 거부와 제한적인 기존 캐시·검색 성공의 차이.
5. worker만 중단했을 때 네 DB의 연결 상태와 실제 처리 상태의 차이.
6. redis-spare / bad-db-password / fresh-search를 각각 적용·복원하고 기존 데이터 보존 확인.
7. 동일 볼륨을 사용하는 컨테이너 재생성 후 주문과 검색 상태 비교.

각 실험은 처음에 정상 기준을 잡고, 한 가지 조건만 바꾸고, 같은 주문 ID/버전으로 복구까지 확인합니다.
`smoke`는 한 주문의 실제 경로 확인이며 부하·HA·내구성 인증이 아닙니다.

## 증거 묶음

별도 `db-lab-mvp-evidence.zip`에 `mvp-tests.log`, `root-tests.log`, `root-result.json`,
`syntax-and-runtime.json`, `runtime-preflight.log`, UI 결과·미리보기·재현 스크립트,
기준 ZIP 비교 및 최종 소스 manifest를 포함합니다. 이번 실행과 이전 remediation 보고서의 결과를 섞어서 읽지 마세요.
