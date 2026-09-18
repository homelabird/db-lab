# 수정 내용 및 실행 검증 보고서

검증일: 2026-09-17. 대상: 사용자가 첨부한 `mariadb-ha-lab-scenarios(1).zip`의 `mariadb-ha-lab`.

## 먼저 확인할 결론

**코드를 수정했고, 실제 Bash/자식 프로세스 실행을 포함한 호스트 검증 182개가 통과했다. 그러나 실제 MariaDB/Galera 컨테이너 실행까지 검증한 파일은 아니다.**

이 작업 환경에는 Podman, Docker, MariaDB 서버 실행 파일이 없다. 실제 통합 테스트 진입점도 실행했지만, 사전 검사에서 종료 코드 **2 / BLOCKED**로 종료했다. DB 실행 결과, SST 성공, 장애 전환 시간, 쿼리 성능 수치는 만들어 넣지 않았다. 실제 컨테이너용 테스트는 명시적으로 승인된 실습 호스트에서 실행하도록 동봉했다.

첨부 원본의 기존 테스트는 139개가 통과했다. 다만 이들은 실 DB 테스트가 아니며 아래 결함들을 검출하지 못했다. 이번에 선정한 **22개 회귀 검증 조건을 원본에 그대로 적용했을 때 실패 18개 + 오류 4개**가 발생했다. 이는 22개의 서로 다른 DB 장애를 재현했다는 뜻이 아니라, 수정 대상 동작을 확인하는 22개의 테스트 조건이다. 수정본의 전체 182개 테스트에는 이 조건들이 포함되어 있으며 모두 통과했다.

## 실제 확인 결과

| 검증 | 결과 | 근거와 한계 |
|---|---|---|
| 쉘 문법 | 9개 모두 통과 | 각 파일에 실제 `bash -n` 실행. 보관용 standalone 스크립트 2개 포함 |
| Python 문법 | 14개 모두 통과 | 실제 Python 컴파일 검사 |
| 전체 단위·회귀 검사 | 182개 통과, 실패·오류·건너뜀 0개 | `reports/validation/host-tests.log` |
| 실제 Bash 실행 경로 | 통과 | 초기화 성공/실패, SIGTERM, 부분 초기화 차단, 토큰 소비, 모드 검증. DB 명령은 명시적인 테스트 대역 |
| Compose 호출 경로 | 통과 | `Lab.offline → Lab.comp → subprocess.run`을 실제 실행. Compose 제공자는 이름 충돌/TTY 조건을 재현하는 명시적인 대역 |
| ShellCheck | 미실행 | 실행 파일이 설치되어 있지 않음. Bash 문법 검사와 동일한 검사라고 표기하지 않음 |
| 실제 이미지 빌드·DB 초기화·3노드 동작·SST·쿼리·장애 복구 | **BLOCKED / 미검증** | Podman/Docker 미설치. `reports/real-acceptance/latest.json`의 `steps`는 빈 목록 |

호스트 검증의 `status=PASS`는 호스트 검사 범위에만 해당한다. 실제 DB 검증의 결과 파일은 별도이며 이 배포본에는 `BLOCKED`로 기록되어 있다.

## 수정한 문제

### 1. 중지된 노드와 임시 컨테이너 이름이 충돌할 수 있는 경로

원본의 offline 명령은 `compose run --rm --no-deps ... galera1`처럼 실행하면서 서비스에 고정된 `container_name`을 두고 있었다. 확인한 podman-compose upstream 구현은 `--name`을 주지 않으면 서비스의 `container_name`을 임시 컨테이너에 사용할 수 있다. 기존 DB 컨테이너가 정지된 채 남아 있을 때 상태 확인·복구·재구축 명령이 이름 충돌로 중단될 수 있다.

수정: 모든 Compose `run` 호출에 프로젝트 범위의 난수 임시 이름과 `-T`를 추가했다. 기존 DB 이름을 재사용하지 않는다. 사용자가 명시한 임시 이름은 보존한다. `--rm`을 유지한다.

이는 특정 제공자/버전의 경로에 대한 수정이며, 사용자의 실제 Podman 로그가 없는 상태에서 유일한 현장 원인으로 확정한 것은 아니다.

### 2. `.env.example`을 복사하면 `init`이 비밀번호를 만들지 않음

원본은 `.env`가 있으면 내용 확인 없이 반환했다. 일반적인 `cp .env.example .env` 이후 `bash lab.sh init`을 실행해도 `GENERATE_WITH_LAB_INIT`이 남아 다른 명령이 실패했다.

수정: 비어 있거나 미생성인 비밀번호만 채우고 유효한 기존 비밀번호는 유지한다. 저장 전에 전체 설정을 검사한다. 중복 키·필수 포트 누락은 명확한 오류로 반환한다. 파일은 생성 시점부터 0600으로 만들고 기존 파일도 권한을 보정한다. 이미 `.state/identity.json`이 있는 실습의 비밀번호가 유실된 경우에는 새 값으로 바꾸지 않고 원래 `.env` 복원을 요구한다.

### 3. 이미지 태그가 있으면 변경된 코드가 빌드되지 않음

원본 `up`은 이미지 태그의 존재만 확인하여 소스를 수정해도 기존 이미지를 계속 사용할 수 있었다.

수정: 빌드 입력의 내용 해시와 이미지 ID를 `.state/builds.json`에 저장하고 비교한다. 수정된 소스는 다시 빌드한다. 빌드 기록이 없거나 소스가 바뀐 상태에서 DB가 실행 중이면 안전한 중단·재시작 절차를 요구한다. 중지된 노드를 시작할 때는 `--force-recreate`로 컨테이너만 교체하고 named volume은 유지한다. 실행 중인 DB를 `start` 명령으로 임의 재생성하지 않는다.

**명시적인 `build` 이후에도 실행 중인 기존 컨테이너 자체가 바뀌는 것은 아니다. 수정본 적용은 반드시 `down → build → up` 순서를 사용한다.**

### 4. 컨테이너 엔진 오류를 “컨테이너 없음”으로 오판

원본은 `inspect`가 비정상 종료하면 원인에 관계없이 absent로 처리했다. 소켓 접근 거부나 엔진 장애도 노드 부재로 판단할 수 있었다.

수정: 실제 “존재하지 않는 컨테이너/객체” 오류만 absent로 처리한다. 다른 엔진/권한 오류는 작업을 중단한다.

### 5. 시작·초기화 종료 처리가 불명확

`LAB_MODE` 오타가 독립 DB 모드처럼 처리되고 health 함수도 SQL만 살아 있으면 정상으로 판단할 수 있었다. 또한 추가 mariadbd 인자를 받으면서 무시하고, 초기화 중 SIGTERM의 종료 처리가 명확하지 않았다.

수정: `galera/standalone`만 허용하고 알 수 없는 모드는 실패시킨다. 지원하지 않는 추가 데몬 인자는 조용히 무시하지 않고 오류를 반환한다. 초기화 중 INT/TERM은 각각 130/143으로 종료하며 자식 프로세스를 정리한다. 초기화 성공 전에 완료 마커를 기록하지 않는다. 일반 명령 전달은 유지한다.

### 6. 프록시·대시보드 준비 전에 Ready 표시

수정: 노드 3개의 준비 상태 외에도 실제 tools SQL 접속/readonly 거부 검사, 대시보드 API와 HAProxy stats HTTP 준비 확인 후 완료를 표시하도록 바꾸었다. 이 동작의 실 네트워크 종단 검증은 런타임 미설치로 아직 수행하지 못했다.

### 7. 시드 실패 후 기존 complete 마커가 남을 수 있음

수정: 스키마 삭제·재생성 전에 manifest를 loading으로 기록한다. DDL 또는 데이터 생성 실패 시 기존 complete 상태로 오인하지 않도록 순서를 수정했다. DDL 실패를 주입하는 회귀 검사로 순서를 확인했다.

### 8. 부하·충돌 실습이 잘못된 결과를 성공으로 끝낼 수 있음

원본 충돌 명령은 양쪽 COMMIT 결과와 카운터 차이를 출력만 했다. 양쪽 COMMIT이 성공하거나, 기대한 충돌과 다른 오류가 발생해도 성공 종료할 수 있었다.

수정: COMMIT 1건 성공, 충돌 오류 1213 1건, 카운터 증가량 1을 모두 요구한다. 나머지는 실패로 종료한다. 접속 후 SQL probe가 실패한 연결과 두 번째 연결 생성 실패 시 첫 연결도 정리한다.

### 9. 장애 시나리오 판정 보강

장애 유지 중 생존 노드가 응답한 신규 쓰기가 있어야 장애 전환 성공으로 처리한다. quorum 테스트는 non-Primary 관찰뿐 아니라 해당 상태에서 writer 요청 거부도 요구한다. 생존 노드가 아닌 장애 대상의 응답을 성공이라고 보고하는 경우, non-Primary인데 쓰기 거부가 관찰되지 않는 경우를 회귀 검사에 추가했다.

노드 간 데이터 관찰은 스키마 소유권 검사 이전부터 `wsrep_sync_wait=1`을 적용해 아직 반영되지 않은 데이터/스키마를 읽을 수 있는 경로를 줄였다. offline 복구는 일반 entrypoint를 우회하므로 `/run/mysqld` 생성과 소유권 처리를 추가했다. 이 SST/복구 변경은 실제 DB에서 추가 확인해야 한다.

### 10. 검증 도구 보강

`tests/validate.sh`는 Bash/Python 문법, 단위·회귀 검사 결과를 JSON과 로그로 기록한다. ShellCheck가 없으면 미실행으로 명시한다. `--require-shellcheck`를 주면 미설치도 실패로 처리한다.

`tests/run-real.sh`는 실제 런타임과 SQL만 호출하는 통합 검사이다. 테스트 대역을 사용하지 않는다. 승인 누락/런타임 부재는 2/BLOCKED, 테스트 실패는 1/FAIL, 요청한 모든 단계 성공은 0/PASS이다. 실패 시 상태 및 노드 로그를 수집하며 자동 reset/prune/volume 삭제를 하지 않는다.

## 기존 실습을 유지하며 수정본 적용

먼저 기존 프로젝트의 `.env`, `.state/`, `backups/`를 보관한다. 컨테이너 데이터는 named volume에 있으므로 기존 프로젝트 이름과 컨테이너 엔진을 바꾸지 않는다. 다른 디렉터리에서 같은 프로젝트를 동시에 조작하지 않는다.

기존 폴더의 코드를 이 수정본으로 교체한다. 배포 ZIP에는 `.env`, `.state`, 실제 백업 및 DB 볼륨 데이터가 들어 있지 않다. `standalone-original/`의 원본 파일은 내용 변경 없이 보존했다. ZIP 해제 프로그램이 실행 권한을 잃어도 아래와 같이 `bash`로 실행할 수 있다.

```bash
# 수정된 코드가 놓인 기존 실습 폴더에서 실행
bash lab.sh init
bash lab.sh down
bash lab.sh build
bash lab.sh up
bash lab.sh verify
```

`reset`이나 `down -v`는 수정본 적용에 필요하지 않다. `.env` 비밀번호를 새로 생성해 기존 DB 비밀번호와 다르게 바꾸지 않는다. 이미 비정상 종료되어 모든 노드의 seqno가 불명확한 경우 안전한 bootstrap 검사가 작업을 거부할 수 있다. 그 경우 데이터 삭제 대신 `bash lab.sh recover`로 복구 위치부터 확인한다. 검증되지 않은 임의 노드 강제 bootstrap은 추가하지 않았다.

## 검사를 다시 실행하는 방법

### DB를 건드리지 않는 호스트 검사

```bash
bash tests/validate.sh
# ShellCheck 설치 및 실행까지 필수로 요구하려면:
bash tests/validate.sh --require-shellcheck
```

결과: `reports/validation/host-validation.json`, `host-tests.log`.

### 실제 DB·장애·SQL 검사 — 폐기 가능한 실습 복제본에서만 실행

**아래 명령은 commerce_lab 합성 데이터와 restore 데이터를 교체하고, DB 노드를 중단·재구축한다. 업무 DB나 보존해야 하는 실습 데이터에는 실행하지 않는다.**

```bash
RUN_DISRUPTIVE_TESTS=1 bash tests/run-real.sh --suite all
```

기본 all 순서: 설정 확인 → 기존 컨테이너 안전 중단 → 실제 이미지 빌드 → 3노드 기동/HTTP/SQL 검사 → tiny 데이터셋 → 복제·행 수·읽기 전용 권한 → 부하/충돌 → writer 강제 종료 → 슬로우쿼리 생성·개선 → 단편화 생성·개선 → 잠금 대기 → 노드 pause 복구 → 한 노드 재구축 → 전체 정상 중단·재기동 → 백업·격리 복원 → 최종 검증.

전체 crash/quorum은 별도 승인 옵션으로 실행한다. 선택하지 않으면 보고서에 NOT_RUN으로 남는다.

```bash
RUN_DISRUPTIVE_TESTS=1 bash tests/run-real.sh --suite all --quorum --crash-recovery
```

결과: `reports/real-acceptance/<실행별디렉터리>/result.json`, 단계별 `.log`, `reports/real-acceptance/latest.json`. 실패 시 `failure-*.log`를 함께 확인한다. 중단 기록이 남은 장애 시나리오는 먼저 `bash lab.sh scenario repair`를 실행한다. 실습 후 `bash lab.sh down`으로 볼륨을 유지한 채 중단한다.

`bash tests/acceptance.sh`는 새 검사기의 core 호환 진입점이다. `RUN_DISRUPTIVE_TESTS=1` 승인 조건은 동일하다.

## 수정 전/후 비교 재현

`reports/validation/baseline-regressions.log`는 원본 프로젝트를 대상으로 한 실제 실패 기록이다. `tests/baseline_cases.txt`에 같은 22개 대상이 있다. 원본 디렉터리를 따로 보존한 경우 다음처럼 실행할 수 있다. 이 검사는 호스트 테스트이며 DB를 실행하지 않는다.

```bash
mapfile -t cases < tests/baseline_cases.txt
LAB_TEST_PROJECT_ROOT=/absolute/path/to/original/mariadb-ha-lab \
  python3 tests/test_regressions.py "${cases[@]}"

# 같은 조건을 현재 수정본에 적용: 통과해야 한다.
python3 tests/test_regressions.py "${cases[@]}"
```

## 근거와 기록

`reports/validation/original-tests.log`: 원본에 포함된 기존 검사 139개 결과.

`reports/validation/baseline-regressions.log`: 원본에서 검출한 22개 회귀 조건 실패.

`reports/validation/host-validation.json`, `host-tests.log`: 수정본의 최신 전체 호스트 검사.

`reports/real-acceptance/latest.json`: 실제 통합 검사 시도 결과. 배포 시점에는 BLOCKED.

압축 후 별도 경로에 다시 해제해 전체 체크섬과 호스트 검사를 확인했다. 이 과정에서 테스트 대역 초기화의 짧은 6초 타임아웃이 한 차례 발생해, 검사 대기 여유를 20초로 늘리고 자식 프로세스 그룹 정리 및 테스트 대역의 신호 처리 등록 순서를 보강했다. 이 현상을 실 DB 장애로 해석하지 않았다.

`docs/changes.patch`: 원본 대비 실행 코드·테스트 변경 내역. `SHA256SUMS`: 현재 배포 파일 체크섬.

기존 `docs/VALIDATION.md`, `SCENARIOS-VALIDATION.md` 및 이전 정적 검사 로그는 과거 기록이다. 현재 범위는 이 문서와 최신 JSON을 기준으로 한다.

구현 확인에 사용한 upstream:

- Podman Compose wrapper: https://docs.podman.io/en/latest/markdown/podman-compose.1.html
- podman-compose `compose_run_update_container_from_args`: https://github.com/containers/podman-compose/blob/main/podman_compose.py
- MariaDB 11.8 image entrypoint: https://github.com/MariaDB/mariadb-docker/blob/master/11.8/docker-entrypoint.sh
- MariaDB Galera mariadb-backup SST: https://mariadb.com/docs/galera-cluster/high-availability/state-snapshot-transfers-ssts-in-galera-cluster/mariadb-backup-sst-method

Upstream 코드를 확인한 것과 해당 컨테이너에서 직접 실행한 것은 구분했다.
