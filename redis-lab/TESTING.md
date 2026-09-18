# 검증 기록 · 2.0.0-operations

## 실제 실행 결과

| 검사 | 결과 | 의미 |
|---|---|---|
| 기존 1.1 회귀 검사 | 107 PASS | 기존 mock/구조 검사 재실행 |
| 전체 오프라인 검사 | 170 PASS, 실패/skip 0 | 기존 107 + 신규 63 |
| 신규 실제 로컬 TCP 검사 | 10 PASS (170개에 포함) | RESP 테스트 서버/echo 서버로 실제 socket 연결·지연/차단·부하 실행 |
| 셸 / Python 문법 | PASS | bash -n, sh -n, compileall |
| YAML/구성 안전 검사 | PASS (170개에 포함) | 확장 network/volume/ports/limits 구조 등 |
| `./ops.sh validate --yes` | **BLOCKED, exit 2** | Podman과 podman-compose 없음 |
| 실제 Redis/이미지/Podman 기동 | **미검증** | Redis 실행기도 없으며 소스 다운로드 DNS 실패 |

**로컬 TCP test double은 Redis가 아닙니다.** 170개 통과를 실제 jemalloc 단편화, BGSAVE, Sentinel 선출, Redis 명령 호환성, Podman cgroup, SELinux 검증으로 해석하지 마세요.

증거: `tests/evidence/offline-tests.txt`, `tests/evidence/validation.json`, `tests/evidence/ops-live-attempt.log`, `tests/evidence/ops-live-validation.json`.

`tests/validation-report.json` 등 루트 tests/ 아래 기존 파일은 1.1 수정판의 역사적 기록입니다. 이번 제작 기록은 **tests/evidence/**가 기준입니다. `TESTING-HA-REVIEW.md`도 이전 수정판 기록입니다.

## 현재 호스트에서 실행할 것

```bash
# 오프라인 검사. YAML 검사를 위해 PyYAML이 필요합니다.
bash tests/check.sh

# 실제 노드 빌드/기동 및 독립 perf 9개 실험
./ops.sh validate --yes
cat output/ops-live-validation.json

# 이미 모든 이미지를 빌드했고 base HA도 실행 중일 때
./ops.sh validate --yes --only --no-build

# 자동 perf 검증 밖의 세 항목
./ops.sh cpu --yes
./ops.sh replication --yes
./ops.sh ha-test --yes

# 기존 HA 전체 검증: sandbox 복원 데이터 교체에 주의
./lab.sh validate --yes --no-build
```

단편화, CPU throttling, 큰 키 지연 등은 플랫폼에 따라 목표 현상이 충분히 나타나지 않을 수 있습니다. 이를 skip/PASS로 숨기지 않고 **NOT_REPRODUCED (3)**로 반환합니다. active defrag 미지원 등은 **BLOCKED (2)**입니다.

기능 실패/데이터 검증 실패/복구 실패는 **FAIL (1)**입니다. JSON status와 exit code가 불일치하거나 이전 run의 보고서가 남으면 자동 validate는 실패 처리합니다. run 생성 시부터 RUNNING으로 기록합니다.

## 검사 범위

신규 검사는 RESP framing/binary/pipeline drain, 명령 재전송 금지, bounded workload, 집계 분류, 회수 prefix 제한, file lock, 복구 저널 선기록, 원복/재시작 판단, 안전하지 않은 CONFIG 변경 거부, HTML escaping, proxy 인증·유효기간·값 검증, 실제 TCP 지연·차단 등을 다룹니다.

CPU/replication 호스트 자동화와 실제 9개 Redis 실험 본문 전체를 mock으로 시뮬레이션한 것은 아닙니다. 특히 실제 Redis 의미론과 시간 의존 현상은 사용자 호스트에서 위 실구동 명령으로 확인해야 합니다. 이 패키지에는 해당 미검증 구간을 PASS라고 기록한 제작 보고서가 없습니다.
