# v12 후속 수정 — MVP 대상 확인·준비 상태·실제 도구 검사

기준: `db-lab-fixed-20260921.zip`의 v11. v11의 Redis/Kafka 실행 경로 수정을 유지하며
MVP의 시작·검사 경로를 보강했다. 이 문서의 기능 설명은 코드 변경 사항이며,
Docker/Podman 실기동을 완료했다는 뜻이 아니다.

## 1. 이번 변경

### HTTP 쓰기 전에 대상 확인

이전 `mvp smoke`는 `.env`의 loopback 포트에 바로 주문을 POST했다. 같은 포트를 다른
실습이 사용하거나 원격 엔진을 선택한 경우, 엉뚱한 로컬 API에 요청할 여지가 있었다.
이제 제어기는 저장된 프로젝트·엔진 바인딩을 검사하고, API 컨테이너가 정확히 하나인지,
소유 라벨이 일치하는지, 실행 중인지, 오직 설정된 `127.0.0.1:API_PORT`에만 공개됐는지 확인한다.
그 뒤 probe가 `GET /api/study/info`에서 application, 정수 study_api=2, **정확한 프로젝트명**을
검사하고 나서 주문을 쓴다. 요청은 프록시·리다이렉트를 사용하지 않는다.

`smoke`는 lifecycle과 같은 로컬 lock을 쓰며 진행 중인 장애 기록이 있으면 쓰지 않는다.
probe의 기본 요청 예산은 60초이고, controller는 probe 프로세스에 별도로 90초 상한을 둔다.
HTTP 읽기에는 1MiB 응답 제한이 있다. 주문을 쓴 뒤 실패/시간 초과가 발생하면 이미 저장된
합성 주문이 남을 수 있다. 실패를 복구한다며 데이터를 지우지 않는다.

HTTP identity는 **다른 실습에 실수로 접근하는 것을 줄이는 확인 절차**이지 인증이 아니다.
다른 클라이언트의 엔진 변경과 관측 사이의 경쟁 상태까지 막는 분산 잠금도 아니다.
직접 `tools/probe.py`를 실행하면 HTTP identity만 확인하며 controller의 엔진 검사는 하지 않는다.
공식 진입점은 `bash all.sh mvp smoke`다.

### 실행 중과 준비 완료 분리

API의 Compose healthcheck를 `/health/live`에서 `/health/ready`로 바꿨다. 이는 SQL 접근
준비 상태이며, 네 가지 DB 전체나 종단 간 메시지 전달을 한 번에 보증하는 검사가 아니다.

worker에는 두 처리 루프의 상태 파일을 확인하는 healthcheck를 추가했다.
전송 루프는 outbox 조회/전송이 성공한 뒤, 소비 루프는 해당 토픽 파티션을 할당받고
브로커에 캐시를 쓰지 않는 offset 조회(timeout 2초)가 성공하거나, 검색 반영과 offset commit이
성공한 뒤 상태를 갱신한다. 유휴 상태의 브로커 조회는 최대 5초마다 한 번 수행한다.
두 루프 모두 최근 성공 상태여야 준비 완료다. 오류/종료 시 상태를 무효화하고,
30초 넘게 갱신되지 않은 상태도 거부한다.

상태 파일은 `/tmp/db-lab-worker-health.json`이며 mode 0600, 원자적 교체를 사용한다.
PID와 `/proc/PID/stat`의 프로세스 시작 시점을 함께 검사해 종료된 프로세스의 기록이나
재시작 이전 기록으로 정상 판정하지 않는다. healthcheck는 파일을 읽기만 한다.
이 검사는 **두 루프의 최근 진행 상태**다. 유휴 소비자는 기존 할당만 믿지 않고 브로커 접근을
재확인하지만, 검색 내용의 정확성이나 복제·내구성을 보증하지는 않는다. 종단 간 확인은
`diagnose`와 `smoke`/`verify`로 분리한다.

컨테이너 상태 수집기도 worker에 healthcheck가 없으면 더 이상 ready로 간주하지 않는다.
기존 이미지에는 새 모듈이 없으므로 수정 적용 후 앱 이미지를 다시 빌드해야 한다.

### 기동 실패와 진단 실패를 숨기지 않음

로컬 Docker의 초기화 반복 중 admin 초기화가 실패하더라도, 남은 실행 예산 내에서
컨테이너 관측 결과를 `mvp-lab/reports/startup/startup-*.json`에 기록한다.
컨테이너가 healthy여도 schema/topic/index 초기화가 실패했다면 기동 완료로 처리하지 않는다.
admin 성공 이후 readiness를 기다릴 때 초기화 명령을 반복 실행하지 않는 동작은 유지한다.
환경 변수, 원시 stderr/stdout, DB 암호를 기동 증적에 넣지 않는다.

`mvp diagnose`는 대상 확인 후 읽기 전용 GET을 수행한다.
응답의 네 의존 서비스별 reachable 값과 전체 값의 일관성을 검사하고,
하나라도 접근할 수 없으면 출력은 보존하되 **exit 1**을 반환한다.
예전처럼 HTTP 200이면 진단 성공으로 처리하지 않는다. exit 0도 HA/내구성 보증은 아니다.

### 실제 Helm 검사 경로 추가

자동 품질 CI에 `helm-render` job을 추가했다. 공식 Helm v3.22.0 Linux amd64 파일을
고정 SHA-256으로 검사하고 설치한 후 **실제** `helm lint --strict`와 `helm template`을 실행한다.
도구 설치나 검사가 실패하면 job을 실패 처리하며, 가짜 렌더러로 대체하지 않는다.
출력 로그를 artifact로 보존한다. 이 작업에서 GitHub Actions job 자체를 실행한 것은 아니다.

검사 스크립트는 default와 배포한 프로필 6개(full, kafka-only, mariadb-only, kafka,
mariadb, distributed), 두 release 이름의 selector 계약을 확인한다.
또한 잘못된 ZooKeeper 조합, 미구현 외부 Kafka, 동의 없는 legacy ES/복구,
새 bootstrap과 복구 동시 지정, 잘못된 최상위 옵션 등 6가지 입력을 실제 Helm이
의도한 이유로 거부해야 통과한다. manifest 검사이지 Kubernetes admission/실기동 검사는 아니다.

기존 Ansible 실제 도구·로컬 fixture job은 유지했다. Ansible을 모방한 실행 결과를 추가하지 않았다.

## 2. 적용 및 실행

기존 데이터를 사용한다면 **기존 폴더에 v11 → v12 패치를 적용**한다.
패치 적용 전에 로컬 변경을 보관하고, `--check`가 실패하면 충돌을 검토한다.
`.env`, `.state`, `.lab`, DB 볼륨을 지우거나 새 ZIP의 초기값으로 교체하지 않는다.

```bash
# 패치 파일을 기존 프로젝트 상위 디렉터리에 둔 경우
# 반드시 기존 프로젝트 루트에서 실행한다.
git apply --check ../db-lab-v11-to-v12-20260921.patch
git apply ../db-lab-v11-to-v12-20260921.patch

bash all.sh mvp doctor
bash all.sh mvp up              # 앱 재빌드·필요한 컨테이너 재생성/기동; 볼륨 삭제 없음
bash all.sh mvp inspect-runtime # 로컬 Docker 전용, 읽기 전용 컨테이너 관측
bash all.sh mvp diagnose        # 읽기 전용; 의존 서비스 장애면 exit 1
bash all.sh mvp smoke           # 합성 주문을 생성하는 종단 간 검사
```

`up`은 컨테이너를 시작하거나 재생성할 수 있어 일시 중단이 발생할 수 있다. 진행 중인 의도적
장애 실습은 기존 복구 명령으로 먼저 마무리한다. 실패한 `doctor`를 무시하고 다음 명령을
계속 실행하지 않는다. 기존 프로젝트 폴더의 위치와 Compose project 이름도 유지한다.

오래된 설치에 engine-target 바인딩이 없으면 선택한 엔진/호스트를 직접 확인한 다음 기존 명령
`bash all.sh mvp bind-target --yes`를 한 번 사용한다. **불일치하는 기존 바인딩을 지워서
재등록하는 자동 복구는 하지 않는다.**

`smoke`와 `diagnose`는 검증된 로컬 엔진에서 실행해야 한다. 원격 엔진은 거부한다.
원격 실습 서버에서는 해당 서버에 SSH로 접속해 명령을 실행하고, PC 브라우저는 기존 SSH
터널로 접근한다. Podman의 경우 `podman info --format json`이 `host.serviceIsRemote=false`를
명시하는 로컬 환경이어야 HTTP 명령을 허용한다. 해당 필드가 없는 버전은 안전하게 거부한다.
Podman의 `up`은 여전히 admin 초기화까지만 판정하며, 로컬 Docker처럼 컨테이너 health 전체를
인증하지 않는다. 이를 성공한 실기동 검사로 해석하면 안 된다.

## 3. 검사 재현 명령과 범위

```bash
python -m pip install -r scripts/requirements-checks.txt
python scripts/quality.py offline --output .quality/v12-offline

# 실제 Ansible과 Helm이 설치된 호스트에서만 도구 검사가 수행된다.
python scripts/quality.py tools --output .quality/v12-tools
python scripts/quality.py release --output .quality/v12-release
```

offline 테스트에는 실제 로컬 HTTP/파일/자식 프로세스 검사와 명시적인 DB·엔진 test double이
함께 들어 있다. 이를 실제 MariaDB/Kafka/Redis/Elasticsearch 연결 검증으로 세지 않는다.
실제 도구가 없으면 tools는 `BLOCKED` 및 exit 127이다. 어떤 skip도 통과로 승격하지 않는다.

이번 배포의 정확한 테스트 수, 실행 로그, 패치·데이터 파일 보존 확인은 함께 제공하는
외부 검증 보고서와 로그 ZIP에 기록한다. 과거 문서의 테스트 수는 당시 기록이다.

## 4. 남은 실환경 확인

이 작업 환경에는 Docker/Podman과 Helm/Ansible이 없으며, Helm 바이너리 다운로드도 실패했다.
따라서 실제 이미지 pull/build, 6개 컨테이너의 기동, DB 간 전달, 장애 후 복구,
Helm 실제 렌더링, Ansible 실제 playbook 실행은 **미검증**이다.
CI에 실제 도구 명령을 연결한 것과 CI 실행 성공은 구분한다.
이 수정은 Docker만으로 기존 Kafka/Redis HA 랩을 지원하도록 전환하는 작업이 아니다.

## 5. 대조한 공식 명세

- Compose의 running과 readiness 차이: https://docs.docker.com/compose/how-tos/startup-order/
- Podman info JSON 및 serviceIsRemote: https://docs.podman.io/en/latest/markdown/podman-info.1.html
- Helm v3.22.0 및 Linux amd64 SHA-256: https://github.com/helm/helm/releases/tag/v3.22.0
- Helm lint: https://helm.sh/docs/helm/helm_lint/
- Confluent Consumer assignment / get_watermark_offsets: https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html

공식 문서는 구현 계약을 대조하는 데 사용했으며, 사용자의 엔진에서 실행됐다는 증거는 아니다.
