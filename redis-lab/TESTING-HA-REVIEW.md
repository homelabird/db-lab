# 검증 결과와 한계 · 1.1.0-reviewed

## 판정 요약

**코드/회귀 검사: PASS. 실제 Redis + Podman 통합 실행: BLOCKED(미검증).**

2026-09-17에 원본 ZIP을 풀어 69개 기존 검사를 다시 실행했고 모두 통과했습니다.
그러나 그 검사들이 놓친 검증 로직 결함 3개를 별도 실행으로 재현했습니다.
수정 후에는 신규 회귀 검사 38개를 포함하여 **107개 통과, 실패 0, skip 0**입니다.

| 검사 층 | 실제 실행한 것 | 결과 |
|---|---|---|
| 원본 검사 | 69개 단위/구조 검사 | PASS |
| 수정판 shell/Python 문법 | bash -n, sh -n, compileall | PASS |
| 수정판 단위/회귀 검사 | 107개, Redis/Podman 호출은 mock | PASS |
| entrypoint 파일 조작 | 임시 디렉터리의 실제 sh 실행, 최종 Redis exec는 stub | PASS |
| YAML | PyYAML 로딩 및 서비스/볼륨/보안 구조 검사 | PASS |
| 수정판 통합 검증기 호출 | `./lab.sh validate --yes` | **BLOCKED, exit 2** |
| 이미지 pull/build, Redis 복제/선출, Podman 네트워크, SELinux, 실제 RDB 복원 | 실행하지 못함 | **NOT VERIFIED** |

작성 환경에는 `podman`, `podman-compose`, `docker`, `redis-server`, `redis-cli`가 없습니다.
Redis 소스 다운로드는 DNS 조회 실패로 불가능했고 Python 의존성 설치도 실패했습니다.
**정적 검사와 mock 통과를 실제 Redis 장애조치 성공이라고 해석하면 안 됩니다.**
배포판·Podman provider·cgroup·SELinux·이미지 빌드에 따른 문제는 사용자 호스트의 실구동 결과로 판정해야 합니다.

## 증거 파일

- `tests/offline-test-results.txt`: 수정판 전체 검사 출력.
- `tests/validation-report.json`: 실행 수와 검증 층별 요약.
- `tests/original-defect-reproduction.json`: 원본의 거짓 성공/오분류/이전 보고서 잔존 재현.
- `tests/live-validation-attempt.json`: 이 작성 환경에서 통합 검증 명령을 호출한 실제 결과(BLOCKED).
- `tests/live-validation-attempt.log`: 해당 명령 출력. exit code 2.
- `CHANGELOG.md`: 결함별 수정 내용과 검증 방법.

패키지의 `tests/`에 들어 있는 보고서는 **제작 시점의 기록**입니다.
사용자가 실행한 최신 결과는 `output/live-validation.json`에 새로 기록됩니다.

## 사용자 호스트에서 전체 실구동 검사

호스트의 Podman과 podman-compose를 준비하고 다음 명령을 실행합니다.

```bash
chmod +x lab.sh
./lab.sh validate --yes
cat output/live-validation.json
```

이 명령은 이미지 빌드/기동을 포함합니다. **현재 Master 종료, Sentinel 종료, 인증 오류를 실제로 주입하며,
redis-sandbox 데이터를 백업본으로 교체합니다.** 필요한 sandbox 데이터는 먼저 내보내세요.
업무 시스템에 연결하지 말고 이 실습에서만 사용하세요. 실행 중 다른 터미널에서 장애를 추가하지 마세요.

이미 수정판 이미지를 빌드한 후 재검사할 때만 다음을 사용합니다.

```bash
./lab.sh validate --yes --no-build
```

### 검사하는 11단계

1. Podman/Compose, 소유 라벨, 네트워크, 환경 일치 사전 점검.
2. 7개 컨테이너 기동, Master 1/Replica 2/Sentinel 3, 실제 SET/WAIT 2/GET.
3. 기본 자료구조 명령과 합성 데이터 생성.
4. 계속 쓰고 있는 클라이언트를 유지한 채 Master 종료 → 새 Master의 쓰기 재개 → 원래 노드 복구 → run ID별 데이터 검증.
5. Replica 인증을 틀리게 설정하여 실제 링크 단절을 관찰하고 복구.
6. Sentinel 2개 종료 시 NOQUORUM을 확인. 정상 Master의 쓰기는 계속 가능한지 확인한 뒤
   Master를 종료하여 정해진 관찰 구간 동안 승격되지 않는지 검사. 이후 전체 복구.
7. 독립 sandbox의 maxmemory 쓰기 거부 및 복구.
8. sandbox의 RDB 저장 실패/MISCONF 및 복구.
9. RDB 백업 → redis-check-rdb → 독립 sandbox 복원 → marker/AOF 검사.
10. clean down/up 후 데이터와 Master 역할 보존.
11. 클라이언트 결과와 진단 로그 내보내기.

### 결과 해석

| status | 종료 코드 | 의미 |
|---|---:|---|
| PASS | 0 | 그 실행에서 모든 단계의 조건을 통과 |
| FAIL | 1 | 실행한 단계에서 오류/검증 실패가 발생; 로그를 확인 |
| BLOCKED | 2 | 실행기/사전 조건 부족으로 실구동 검증을 시작하지 못함 |
| RUNNING | 해당 없음 | 실행 중 또는 강제 중단; 성공으로 판단하지 않음 |

단계별 로그와 별도 report는 `output/live-날짜-고유ID/`에 저장됩니다.
실패해도 이전 성공 보고서를 최신 결과로 남겨두지 않습니다.
`PASS`도 모든 종류의 네트워크 장애, 운영 부하, 호스트 장애, 무손실을 보장하지 않습니다.

## 기존의 개별 실습/검증 명령도 유지

```bash
./lab.sh up
./lab.sh status
./lab.sh demo
./lab.sh test
```

`test`는 현재 Master를 실제로 종료하고, 새 Master의 기준 데이터와 원래 노드의 Replica 복귀를 검사합니다.
성공/실패와 별도 복구 오류를 `output/failover-test.json`에 저장합니다.
상세한 대응 절차는 `docs/`를 참고하세요.

## 오프라인 검사 재실행

```bash
bash tests/check.sh
```

Python 표준 라이브러리로 대부분 실행 가능합니다. PyYAML이 없으면 YAML 검사 10개는 skip됩니다.
제작 시에는 PyYAML을 사용할 수 있어 107개 모두 실행했습니다.
mock 단위 테스트는 실제 redis-py 접속, Redis parser, Podman provider, volume 소유권을 검증하지 않습니다.

## 자동 통합 검사 밖의 항목

`pause`와 `isolate`는 rootless/cgroup/network backend에 따른 편차가 있어 위 11단계에 넣지 않았습니다.
해당 명령은 `docs/03-sentinel.md` 및 `docs/06-operations.md` 절차로 별도 실습하세요.
실제 호스트 장애, 부하 성능, TLS/세분화 ACL, 재해 복구 검증도 이 패키지의 PASS 범위가 아닙니다.
