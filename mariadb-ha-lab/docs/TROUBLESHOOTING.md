# 문제 해결

## runtime / compose가 없다

`./lab.sh doctor`는 `podman info`, Compose version/config를 확인합니다. Fedora/RHEL 호스트라면 배포판에 맞는 Podman 및 podman-compose를 설치하고 Python 3가 있는지 확인하세요. Docker를 이미 쓰는 환경은 Docker Compose를 선택합니다. 한 번 만든 rootless 컨테이너를 갑자기 `sudo podman`으로 관리하려 하지 마세요. rootful과 rootless의 컨테이너/볼륨 저장소는 별개입니다.

Compose 구현이 너무 오래되어 profiles, build args, named networks를 처리하지 못하면 구현을 업데이트해야 합니다. 도구의 provider마다 지원 차이가 있으므로 실제 `doctor` 결과를 기준으로 판단하세요.

## 포트 충돌

새 기본 포트는 133xx/180xx입니다. `ss -ltnp`로 확인한 뒤 `.env`의 해당 PORT 값을 변경하세요. 노드/프록시 내부 포트나 SQL 파일까지 수정할 필요는 없습니다. 기존 컨테이너를 지우는 대신 충돌한 **새 랩의 호스트 포트**를 바꾸는 것이 우선입니다.

기존 컨테이너 생성 후 포트를 변경했다면 해당 서비스를 재생성해야 합니다. DB 재생성은 살아 있는 Primary와 quorum을 확인하며 한 노드씩 진행하세요. 전체 중단 후에는 반드시 `./lab.sh up`의 bootstrap 검사를 거칩니다.

## SELinux / Permission denied

새 랩의 config/init scripts는 이미지 COPY이고 DB는 named volume입니다. `./initdb:/docker-entrypoint-initdb.d` bind mount를 사용하지 않습니다. `setenforce 0`, `--privileged`, 무차별 chmod 777을 해결책으로 쓰지 않습니다.

그런 오류가 나오면 실제로 `standalone-original/`에서 예전 compose를 실행했는지 먼저 확인하세요. 원본 프로젝트는 의도적으로 수정하지 않고 보존했기 때문에 과거 bind-mount 동작도 그대로입니다. 새 파일 수정은 빌드에 반영되어야 합니다.

## 첫 노드가 Join 대기만 한다

직접 `podman compose up -d`를 실행하여 bootstrap 없이 세 노드를 띄웠을 수 있습니다. 중요 데이터가 없는 새 랩인지 확인하고 노드를 중지한 뒤 정상 런처를 사용하세요. `.env` 설정과 프로젝트 이름이 맞는지도 확인합니다. 이미 상태를 가진 볼륨에 `--wsrep-new-cluster`를 무조건 붙이지 마세요.

## Initialization marker is missing

공식 이미지의 로컬 system table 초기화가 끝나지 않은 볼륨입니다. 무시하고 cluster를 시작하지 않도록 막았습니다. healthy donor가 두 개 있으면 `./lab.sh rebuild NODE --confirm-rebuild`로 그 노드만 재가입시킵니다. 완전히 새 disposable 랩이면 오류를 해결한 후 명시적 reset으로 다시 만듭니다. 원본 업무 데이터를 연결하지 마세요.

## SST: Access denied / socat not found / wsrep provider not found

```bash
./lab.sh logs galera1 --tail 200
./lab.sh logs galera2 --tail 200
./lab.sh logs galera3 --tail 200
```

donor와 joiner 둘 다 읽습니다. 이미지 빌드에는 Galera provider, mariadb-backup, socat, Python health와 driver 설치/존재 검사가 포함되어 있습니다. 같은 이미지가 모든 노드에 쓰이는지 확인하세요. SST 계정은 donor의 localhost에서 backup 권한을 사용합니다.

`.env` 비밀번호를 **초기화 뒤에만 수정**하면 기존 mysql 사용자 비밀번호가 자동으로 바뀌지 않습니다. node health나 SST auth가 깨질 수 있습니다. 기존 .env 값을 복원하거나, 별도의 SQL 계정 변경 절차를 모든 필요 설정과 함께 수행해야 합니다. 이 랩은 비밀번호 자동 rotation을 제공하지 않습니다.

SST 로그는 컨테이너 출력 외에 datadir의 `mariadb-backup.*.log`에도 있을 수 있습니다. 필요하면 해당 노드에서 `podman exec mariadb-ha-galera3 ls -l /var/lib/mysql`로 확인합니다. project 이름을 변경했다면 컨테이너 이름도 맞추세요.

## WSREP has not yet prepared node for application use

`Non-Primary`, `Joining`, `Joined`, `Donor` 또는 아직 준비되지 않은 상태일 수 있습니다. `./lab.sh status`와 로그부터 봅니다. `wsrep_ready=OFF`인데 TCP 3306이 열렸다는 이유로 정상이라고 판단하지 않습니다. 프록시는 HTTP readiness가 200일 때만 신규 backend 후보로 사용합니다. failure threshold만큼 판정 지연은 있을 수 있습니다.

## 한 노드가 남아도 Primary다

정상 순차 종료는 장애 quorum 실습과 다릅니다. membership이 정상적으로 줄면 마지막 노드가 Primary를 유지할 수 있습니다. 두 노드 동시 도달 불가 실습은 `quorum-demo`로 수행합니다.

## pause 후 resume했는데 곧바로 healthy가 아니다

failure detection, component 재결합, 재동기화에는 과정이 필요합니다. `resume`은 OS 프로세스를 다시 진행시키는 명령이지 상태 동기화 완료를 뜻하지 않습니다. 로그와 `status`를 보며 기다리고 `verify`로 확인하세요. 단순히 급하게 bootstrap하여 UUID가 다른 클러스터를 만들지 마세요.

## 전체 중단 뒤 start galera1이 거절된다

정상입니다. `start NODE`는 살아 있는 Primary가 있을 때의 재합류 명령입니다. 전체 중단은 `up`을 사용합니다. `up`이 safe flag를 찾지 못하면 모든 노드를 중지한 상태에서 `recover`로 위치를 비교합니다. 한 노드라도 실행 중/paused면 offline recovery가 거절됩니다.

## 오류 1213, 1205, 2013

1213은 local deadlock 또는 Galera conflict/BF-abort 경로에서 나타날 수 있고, 1205는 lock wait timeout, 2013은 연결 유실 등과 관련됩니다. 코드 번호만 보고 같은 장애라고 뭉뚱그리지 마세요. local InnoDB 상태, wsrep conflict 카운터, 노드 readiness, 프록시 backend를 함께 봅니다.

연결 유실 뒤 COMMIT 성공 여부는 불명확할 수 있습니다. 같은 요청을 새로운 request_id로 무조건 재전송하면 중복 작업이 될 수 있습니다. 실습 workload는 같은 request_id receipt를 확인합니다. 성공 여부를 끝내 확인하지 못한 요청은 unresolved로 별도 보고하고 성공했다고 숨기지 않습니다.

## verify가 행 수 불일치라고 한다

`verify`는 합성 dataset의 초기 예상 행 수와 비교합니다. 직접 INSERT/DELETE/partition drop을 실습했다면 달라지는 것이 자연스럽습니다. 이때 초기 seed 검증 실패와 replication divergence를 구분하세요. 동일 시점의 노드별 값 비교, 실행한 변경 기록, 필요하면 합성 commerce_lab 명시적 재적재를 사용합니다.

## 메모리/디스크 부족

노드 3개의 전체 데이터/인덱스, binlog, gcache가 필요하고 restore를 켜면 복사본이 추가됩니다. `podman stats`, `df -h`, `podman system df`로 확인합니다. buffer pool 외 메모리도 소비합니다. 먼저 CloudBeaver 및 다른 Elasticsearch/JVM 랩을 중지하거나 `small` 데이터를 사용하세요. 임의로 durability 값을 낮추어 오류를 숨기지 마세요.

## 복원 실패

`backups/*.sql.gz.json`의 checksum과 dump 파일을 함께 보관하세요. `restore`는 별도 서버에만 적용되므로 실패했다고 원본 Galera에 즉시 같은 dump를 넣지 마세요. 복원 노드 로그, SQL 오류, 디스크 공간을 확인합니다. 제작 환경의 runtime 미검증 범위도 `VALIDATION.md`를 참고하세요.
