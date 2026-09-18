> **수정 전 검증 기록입니다.** 현재 수정본의 결과는 [FIXES-VALIDATION.md](FIXES-VALIDATION.md)와 `reports/validation/host-validation.json`을 확인하세요.

# 시나리오 확장 검증 범위

2026-09-17. **정적/모의 검사와 실제 MariaDB·컨테이너 통합 실행을 구분합니다.**

## 실제 수행

- `python3 -m unittest discover -s tests -v`: **139개 통과**. 기존 82개 + 신규 57개.
- 추가 검사는 CLI/도움말, 인수 범위, 소유권 표식, wsrep 꺼진 세션 거부, FILE/TABLE 설정 복구, 쿼리 오류 시 finally, 실제 데이터 보존 조건의 판정, 미완료 실행 기록/권한600, PID 취소 요청, 비정상 노드 사전 거부, quorum/정리 승인 거부를 포함합니다.
- 명시적인 FakeLab 모델로 SIGKILL/재가입 및 두 노드 pause/unpause 제어 흐름과 복구 기록 정리를 검사했습니다. **이 테스트의 SQL/노드 결과는 fake이며 실제 DB 측정값이 아닙니다.**
- Python 문법 컴파일, shell 문법 검사, 컨테이너 없이 동작하는 `scenario list`·`scenario report`·각 도움말을 검사했습니다.
- 기존 compose, 이미지 설정, `.env.example`, 원본 보관 폴더는 이번 확장에서 바꾸지 않았습니다.

실제 실행 로그는 `docs/scenario-static-tests.log`에 있습니다. 제품 실행 경로에는 가짜 결과를 반환하는 fallback이 없으며, DB 연결/worker가 실패하면 실패 또는 복구 필요로 기록합니다.

## 여기서 수행하지 않은 것

이 제작 환경에는 Podman/Docker/MariaDB 서버가 준비되어 있지 않아 다음은 **실행 미검증**입니다.

- MariaDB 11.8 실제 SQL 실행, mysql.slow_log 기록과 조회, InnoDB 파일 공간 변화.
- Galera TOI/복제/노드 crash 재가입/SST·IST, HAProxy 신규 쓰기 경로.
- 실제 rootless Podman의 pause/freezer 지원 및 SELinux 정책.
- 실제 노드의 SIGTERM·Ctrl+C 도중 worker 종료·전역 설정 복구.
- 호스트 성능, 실제 데이터 적재 속도, 반환되는 공간과 장애 전환 지연.

따라서 139개 통과가 위 런타임 검증까지 완료했다는 뜻은 아닙니다.

## 사용자 호스트에서 실제 검증

```bash
./lab.sh init
./lab.sh up
./lab.sh scenario demo --rows 5000 --hold 20
./lab.sh scenario report

# 추가 시나리오 전체. 기존 랩 노드가 의도적으로 중단/재시작됩니다.
RUN_DISRUPTIVE_TESTS=1 bash tests/scenario_acceptance.sh

# 별도 승인으로 quorum까지 포함
RUN_DISRUPTIVE_TESTS=1 RUN_QUORUM_TEST=1 bash tests/scenario_acceptance.sh
```

성공 시 `summary.json`에 실제 측정값이 저장됩니다. 실패 시 성공 수치를 만들어 채우지 않습니다. 복구 기록이 남으면 `./lab.sh scenario repair`부터 실행하세요. 초기 구동 실패는 기존 `docs/TROUBLESHOOTING.md`를 따르며 bootstrap을 임의로 강제하지 마세요.
