# DB 시뮬레이션 v3 — 추가 구현 및 검증 보고서

**작성일: 2026-09-19 · 입력: db-lab-simulation-v2.zip · 출력: db-lab-simulation-v3.zip**
입력 SHA-256: `8570137838e4c1eb7aa99c68ad8b917bf33eb7a08450837a51a63a2710738afc`

## 결론

미구현 항목 중 네트워크 송신 지연/손실, 별도 Redis의 저장 경로 고갈/OOM, 애플리케이션 논리 백업·격리 복원, 다른 MariaDB 이미지로의 논리 호환성 시험, 두 행의 역순 잠금 실습을 추가했습니다. **기본 6개 컨테이너를 유지하며 원본 DB의 영속 볼륨은 복원·자원 실습의 대상에 넣지 않습니다.** 네트워크 실습은 원본 MVP MariaDB의 network namespace를 일시 변경하고, deadlock 실습은 원본에 합성 주문 두 개를 생성합니다.

**전체 호스트 테스트 910개와 기존 V01~V08 독립 재검사 19개가 통과했습니다. 실제 이미지 빌드·기동·패킷 손실·ENOSPC·OOM·InnoDB deadlock·다른 이미지 복원은 수행하지 못했습니다.** 테스트는 mock/계약/일부 loopback HTTP 검사이며 실제 DB 결과로 바꾸어 표시하지 않습니다.

사용자의 호스트나 외부 DB에는 접속하지 않았습니다. 대화에 첨부된 ZIP을 별도 사본에서 수정했습니다. 이전 원본 ZIP은 그대로 보존합니다.

## 1. 실제 반영한 범위

| 추가 기능 | 구현 | 합격 기준 / 한계 |
|---|---|---|
| db-network-delay | Linux tc netem, MariaDB eth0 송신 180ms+jitter20ms | 처리 패킷과 클라이언트 p50 증가, 복구 후 주문 필드 대조. SQL만 선별하는 지연 아님 |
| db-network-loss | 같은 송신 큐의 10% 확률 loss | qdisc drops>0와 복구·최종 주문 일치. HTTP 오류가 반드시 발생하는 것은 아님 |
| backup-restore | orders/outbox를 한 InnoDB consistent snapshot으로 JSON 저장, 같은 이미지의 새 DB에 복원 | 전체 행 checksum과 생성·멱등성·상태 변경 canary. 물리/전체 서버 백업 아님 |
| upgrade-restore | 미리 내려받은 숫자 버전 MariaDB 이미지의 새 DB로 동일 snapshot 복원 | 행 일치+canary+source/target image ID·server version. in-place 업그레이드/다운그레이드 아님 |
| redis-disk-full | 별도 Redis AOF를 16MiB tmpfs에 저장하여 제한된 ENOSPC 관찰 | AOF write err+서버 ENOSPC 로그. 호스트 디스크 고갈/하드웨어 I/O 고장 아님 |
| redis-oom | 별도 Redis의 64MiB cgroup memory/swap 한도 | OOMKilled=true+ExitCode137+종료. 연결 오류나 Redis eviction만으로 통과 불가 |
| deadlock | 합성 주문 2개, 두 연결의 역순 FOR UPDATE | 1213 victim+survivor, 양쪽 rollback, 주문 내용 불변. 1205만으로 통과 불가 |

기존 `simulate`는 11개에서 **13개**, 신규 `drills`는 **5개**입니다. `drills prepare`는 netem helper만 별도로 빌드합니다. 기본 Compose, 일반 앱 Containerfile, 기본 DB 이미지 설정, 기존 네 HA 실습/Helm 서비스 코드는 수정하지 않았습니다.

## 2. 백업·복원의 정확한 범위

한 연결에서 UTC, REPEATABLE READ, `WITH CONSISTENT SNAPSHOT, READ ONLY`를 사용합니다. `orders`의 멱등성 키와 `outbox`의 seq/event_id/payload/created_at/sent_at도 보존합니다. 두 테이블 외 다른 테이블이 있거나 InnoDB가 아니면 부분 백업으로 성공 처리하지 않고 거부합니다.

한 테이블 5,000행, outbox payload 합계 4MiB, snapshot JSON 16MiB 상한을 둡니다. 큰 LONGTEXT를 전부 읽기 전에 같은 snapshot에서 건수·바이트 예산을 확인합니다. checksum 검증 뒤 **빈 폐기용 schema**에만 parameterized INSERT를 수행합니다. 기존 schema에는 DROP/덮어쓰기를 하지 않습니다.

복원 DB는 network none, 512MiB tmpfs datadir, 메모리768MiB입니다. client는 이 DB의 loopback namespace만 공유하며 원본 네트워크/볼륨은 공유하지 않습니다. 행 checksum 이후 새 주문 생성/동일키 재요청/paid 변경/조회가 성공해야 통과입니다. 복원된 outbox를 원본 Kafka에 다시 발행하지 않습니다.

이 방식은 사용자·권한·루틴·trigger·binlog·PITR·물리 데이터 파일·서버 설정·삭제 이후 AUTO_INCREMENT high-water mark의 백업이 아닙니다. tmpfs clone이므로 저장 매체 재시작 내구성, RTO/RPO, 전 DB 분산 스냅샷을 검증하지 않습니다. 이전 JSON을 `--snapshot`으로 복원할 수 있지만 원본을 과거 시점으로 돌리는 기능은 아닙니다.

## 3. 실행 대상과 정리 보호

로컬 Docker Linux+Compose v2, 기존 engine pin과 project identity를 요구합니다. 원격 Docker/Podman 자동 장애 조작은 차단합니다. API의 실제 SQL_HOST/database/user/STUDY_PROJECT도 예상 MVP와 일치하는지 검사합니다. 새로운 일반 운영 플랫폼이나 Docker socket을 마운트한 감시 서비스는 만들지 않았습니다.

네트워크 helper는 대상 컨테이너 network namespace에만 붙고 cap-drop ALL+NET_ADMIN을 사용합니다. host network, privileged, host PID, 호스트 마운트, iptables, modprobe는 사용하지 않습니다. 기존 qdisc/다중 네트워크를 덮어쓰지 않습니다. 예약 handle `7a11:`만 제거합니다. helper 자체 lease는 CLI 종료 후에도 helper가 살아 있을 때 동작하지만 helper SIGKILL/호스트 정지까지 보장하지 않습니다.

폐기용 resource는 원본 Redis/MariaDB와 같은 실제 image ID를 재사용하고, 후보 버전은 명시적으로 선택·pull한 숫자 태그만 받습니다. 자원 실습은 Docker의 memory/swap 제한과 총 RAM 6GiB 이상을 검사합니다. **총량은 여유 메모리 인증이 아닙니다.** 여러 실습을 동시에 실행하거나 host memory가 이미 부족하면 제한된 실습도 영향을 줄 수 있습니다.

컨테이너 생성 전에 복구 marker를 기록합니다. 정리 전에 engine/project/run/name/container ID/image ID/network/tmpfs/memory/mount를 확인합니다. 원본 named volume이나 bind mount가 연결된 컨테이너는 자동 정리하지 않습니다. 정리는 소유한 컨테이너의 stop/rm뿐이며 rm -v/-f, volume 삭제, prune은 사용하지 않습니다. 임시 tmpfs의 내용은 정리 시 사라집니다.

실패 시 남은 marker는 일반 up/down/experiment를 막고 recover를 요구합니다. 잠금 획득 뒤에도 marker를 다시 검사합니다. 새 실행의 preflight가 실패했다고 이전 실행의 marker를 자동 정리하지 않습니다. SIGTERM/일반 예외에서는 정리를 시도하지만 SIGKILL/호스트 종료 시 수동 복구가 필요할 수 있습니다. 기존 stop/pause에 외부 watchdog을 추가한 것은 아닙니다.

## 4. 검증 결과

| 묶음 | 테스트 수 | 결과 |
|---|---:|---|
| 루트 | 91 | 통과 |
| MVP | 266 | 통과: 기존199+신규67 |
| Elasticsearch | 77 | 통과 |
| Kafka | 94 | 통과 |
| MariaDB | 198 | 통과 |
| Redis | 184 | 통과 |
| **전체 호스트 회귀** | **910** | **실패·skip 없음** |
| 독립 V01~V08 및 기존 흐름 | 19 | 통과, 전체 회귀와 별도 집계 |

신규67개는 snapshot19, netem16, disposable/deadlock/resource32개입니다. snapshot 검사는 가짜 cursor/connection, deadlock은 명시적 1213 double, netem은 mock tc, Docker 조작은 mock/stub입니다. 실제 Redis 압력을 가한 시험은 아닙니다. 기존 루트의 chart 계약은 제한된 Go template 실행이며 실제 Helm 검사로 계산하지 않습니다.

정적 검사: **Python92개, Bash55개, 그중 POSIX sh5개, 일반 YAML24개, UI JavaScript1개**가 통과했습니다. Helm template11개는 일반 YAML 파싱에서 제외했습니다. 실제 Compose config/Helm 렌더링과 구문 파싱은 다릅니다.

호스트 Python은 3.13.5이고 앱 이미지의 Python3.12에서 패키지를 설치해 검증한 것은 아닙니다. 여러 번 수행한 동일 테스트를 합산하지 않습니다. 최종 전체 결과는 `full-results.json`, 독립검사는 `extended-results.json`, 정적/명령 결과는 `static-cli-results.json`이 기준입니다.

### 실제 실행한 CLI 점검

| 명령 | 결과 |
|---|---|
| simulate list / drills list | 성공: 13개/5개 경로 제공 |
| simulate plan db-network-loss --seed42 두 번 | 동일 결과 |
| mvp init 두 번 | 기존 .env 내용 보존 |
| mvp doctor | 종료1: 엔진 없음 |
| drills run backup-restore --yes | 종료1: 엔진 없음, 가짜 복원 결과 생성 안 함 |
| drills prepare --yes | 종료1: 엔진 없음 |
| drills run redis-oom, --yes 없음 | 종료2: 동의 누락 거부 |
| scripts/test-runtime-mvp.sh --yes | 종료127: Docker 없음, 실제 인수 시험 미실행 |
| scripts/test-helm.sh | 종료127: Helm 없음 |

검사 환경에서 Docker/Podman/Helm/kubectl/ShellCheck가 없었습니다. Docker registry 연결도 DNS 실패(curl6)였습니다. iproute2 `tc` 바이너리는 있지만 NET_ADMIN 없는 이 환경에서 network qdisc를 변경하지 않았습니다. 기존 프로세스/커널/호스트 디스크에 압력을 가하지 않았습니다.

### 검사 중 수정한 것

초기 netem 시험1개에서 mock lab의 config 값이 실제 문자열이 아니어서 TypeError가 났습니다. 테스트 fixture를 실제 MVP project 문자열로 고친 뒤 16개 통과를 확인했습니다. 처음 실패 로그와 최종 로그를 모두 증거에 남겼으며, DB 동작 실패를 숨긴 사례로 집계하지 않습니다. 초기 부분 suite 실행 수는 최종910개에 중복 포함하지 않습니다.

초기 패키지 대조에서는 .gitignore 때문에 Git 기반 목록에서 기존 보고 자료6개가 빠질 수 있음을 발견했습니다. 원본 ZIP의 전체 파일 목록과 합쳐서 재패키징했고 기존 자료6개도 그대로 보존했습니다. 최종 배포는 원본384개에서 삭제 없이 신규13개를 더한397개입니다. 기존6개 파일 변경과 신규13개, 총19개 파일에 차이가 있습니다.

## 5. 실제 환경 인수 순서

실습 데이터만 있는 원래의 로컬 MVP에서 전체 소스를 반영하고 API/worker를 다시 빌드해야 합니다. `.env`/`.state`/볼륨을 보존하세요.

```bash
bash ./all.sh mvp doctor
bash ./all.sh mvp up
bash ./all.sh mvp smoke
bash ./scripts/test-runtime-mvp.sh --yes
```

마지막 스크립트는 준비된 스택을 대상으로 smoke → baseline → helper build → 네트워크2개 → 논리복원 → resource2개 → deadlock을 순서대로 실행합니다. 합성 데이터 쓰기·일시적 장애·임시 DB 생성을 포함하므로 `--yes`가 필수입니다. 실패 또는 inconclusive에서 중단하고 로그를 남깁니다. 원본 자동 초기화·볼륨 삭제·자동 전체시작으로 문제를 숨기지 않습니다. 후보 이미지 시험은 별도 명시적으로 실행합니다.

## 6. 잔여 작업

**실제 Docker/DB 인수 시험은 여전히 남아 있습니다.** 새 코드가 모든 엔진·아키텍처에서 실행됐다고 주장하지 않습니다. netem kernel/capability, 고정 이미지의 entrypoint/healthcheck/tmpfs 권한, Redis AOF/서버 로그, 실제 OOMKilled, MariaDB snapshot·1213, candidate restore를 현장에서 확인해야 합니다.

기능적으로도 다중 호스트/복제 HA, 실제 디스크 장치 I/O 장애, in-place 메이저 업그레이드·다운그레이드, 물리 백업·PITR, poison-event/DLQ, 재고·결제 정합성, 기존 모든 장애의 외부 watchdog, ES legacy 현대화는 별도 작업입니다. 이번에는 파괴 위험을 줄인 범위부터 구현했으며 모든 잔여 항목이 완료됐다고 표시하지 않습니다.

## 7. 파일과 근거

`mvp-lab/docs/ADVANCED-DRILLS.md`에 전체 명령·안전 경계가 있습니다. 주요 새 파일은 snapshot.py, deadlock_drill.py, redis_pressure.py, netem_agent.py, tools/advanced.py, tools/netem.py입니다. 전체 증거 ZIP에는 최종 suite 로그, 독립검사, 초기 실패 로그, 정적·CLI 결과, source diff/hash와 archive integrity가 포함됩니다.

공식 참고: Docker run/resource/tmpfs, MariaDB START TRANSACTION, iproute2 netem 소스. 정확한 링크는 가이드 끝에 있습니다. 문서 확인은 해당 이미지 실기동 증거가 아닙니다.
