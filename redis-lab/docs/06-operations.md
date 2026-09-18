# 06 · Podman, 권한, 재기동 문제 해결

## 기동 실패 시 먼저

```bash
./lab.sh doctor
podman ps -a --filter label=io.redis-sentinel-lab.id=rslab
./lab.sh logs redis-1 --tail 100
./lab.sh logs sentinel-1 --tail 100
./lab.sh logs lab-client --tail 100
./lab.sh collect
```

Container Created/Exited, 이미지 pull 실패, application 인증 오류를 구분합니다.
Podman 자체가 준비되지 않았으면 먼저 `podman info`의 오류를 해결해야 합니다.
Compose config 검사 통과는 컨테이너 실행이나 자동 failover 성공과 동의어가 아닙니다.

## 같은 사용자로 일관되게 실행

rootless Podman은 사용자별로 이미지·컨테이너·볼륨 저장소가 다릅니다.
`sudo podman ps`에는 보이는데 `podman ps`에는 안 보이는 상황에서 전체 데이터를 지우지 마세요.
프로젝트를 띄운 사용자 계정에서 계속 실행하세요.

## 포트 및 network namespace

모든 Redis는 각자의 네트워크 namespace에서 6379를 사용합니다.
Sentinel도 각자의 namespace에서 26379를 사용합니다.
wrapper는 `podman-compose --in-pod=false`를 지정하고 compose.yaml에도 `x-podman.in_pod: false`를 둡니다.
임의로 같은 network namespace나 host network로 합치면 포트 충돌과 실습 전제가 달라질 수 있습니다.

외부 port publish를 하지 않아 `localhost:6379`로는 접근되지 않습니다.
`cli`나 컨테이너 안의 클라이언트를 사용하세요. 외부 접근을 위해 포트만 임의로 매핑하면
Sentinel이 알려주는 내부 주소에 외부 클라이언트가 접근하지 못하는 문제가 남습니다.

## subnet 겹침

```bash
./lab.sh init
cat .env
# 최초 up 전에 LAB_SUBNET과 8개 IP를 모두 수정합니다.
./lab.sh doctor
```

기본 10.89.77.0/24가 다른 네트워크에 사용 중이면 충돌 검사가 실패합니다.
예를 들어 미사용임을 확인한 10.90.88.0/24로 바꾸면 .11/.12/.13/.21/.22/.23/.30/.40 주소도 같은 대역으로
바꿔야 합니다. 실제 사용 여부는 호스트/VPN 네트워크를 기준으로 확인하세요.

## SELinux와 쓰기 권한

실행 중 쓰는 파일은 named volume 안에 있습니다. 설정 템플릿은 이미지에 COPY합니다.
공유 bind mount를 여러 Sentinel이 덮어쓰는 구조가 아니며 SELinux를 끄지 않습니다.

Redis 이미지의 공식 entrypoint가 /data 소유권을 정리하고 redis 사용자로 Redis를 실행합니다.
클라이언트는 UID 10001로 실행되며 /results는 해당 사용자용 볼륨입니다.
로그 Permission denied가 있으면 `podman volume inspect`와 컨테이너 UID/volume 상태를 확인하세요.
`chmod 777`이나 `setenforce 0`을 기본 해결책으로 쓰지 않습니다.

## runtime 설정 보존

| 위치 | 의미 |
|---|---|
| config/*.template | 새 볼륨을 초기화할 때만 사용하는 템플릿 |
| /data/state/redis.conf | Redis 실행 중 설정, Sentinel의 재편 결과 보존 |
| /data/state/sentinel.conf | Sentinel별 고유 ID, 발견 노드, Master 전환 상태 보존 |
| /data/db | RDB/AOF 데이터 |
| /results | 클라이언트 JSONL 로그·검증 결과 |

템플릿이나 REDIS_MAXMEMORY 값을 바꾼 뒤 컨테이너만 재시작해도 **기존 runtime config는 덮어쓰지 않습니다**.
이 동작은 실패가 아니라 이전 장애조치 상태를 지키기 위한 정책입니다.
현재 설정을 바꾸는 실습에서는 `CONFIG SET`을 사용하고, 재시작 후에도 유지하려면 해당 노드에
`CONFIG REWRITE`를 실행하세요. 모든 노드에 자동 적용되는 설정은 아닙니다.

비밀번호/IP 변경은 별도 마이그레이션을 자동 지원하지 않습니다.
기존 설정을 복원해 계속 사용하거나, 백업/결과를 내보내고 이전 .env로 `reset --yes` 한 뒤 새 설정으로 시작합니다.
볼륨이 남은 상태에서 .env를 지우고 새 비밀번호를 생성하면 인증 불일치로 시작하지 못하도록 막습니다.

## pause/isolate 미지원

일부 rootless/cgroup/network backend 환경에서는 pause나 live network disconnect/connect가 제한될 수 있습니다.
실행 오류가 발생하면 해당 기능이 성공했다고 판단하지 않습니다. `faults` 기록은 복구 경로를 남기기 위해
먼저 저장하므로 실패한 시도도 기록에 남을 수 있습니다. 상태를 확인한 뒤 `recover`로 정리하세요.

`kill-master`와 `stop` 시나리오부터 진행할 수 있습니다. 특권 모드를 켜거나 호스트 네트워크를 내리는
자동 우회는 제공하지 않습니다.

## 설정과 로그 내보내기

```bash
./lab.sh config      # 비밀번호를 가린 Compose 설정 출력
./lab.sh collect     # 주요 상태 + 최근 로그, 전체 inspect 환경변수는 제외
./lab.sh results     # 클라이언트 내부 결과를 output/으로 복사
```

진단 파일은 공유 전에 직접 확인하세요. 직접 실행한 전체 `podman inspect`, Compose 상세 출력,
.env 자체에는 비밀번호가 포함될 수 있습니다. 타 서비스의 정보를 통째로 수집하지 않습니다.

## 종료, 복구, 초기화의 차이

```bash
./lab.sh recover        # 기록된 장애 제거 후 복제/감시 정상화 확인
./lab.sh down           # 컨테이너 삭제, named volume 유지
./lab.sh up --no-build  # 저장된 설정/데이터로 재기동
./lab.sh reset --yes    # 이 실습의 named volume도 삭제 (복구 불가)
```

`reset`은 rslab 프로젝트의 정확한 컨테이너/볼륨 이름과 소유 라벨을 확인합니다.
다른 프로젝트의 Redis/MariaDB/Elasticsearch 컨테이너나 볼륨을 지우는 prune 명령은 없습니다.
LAB_NAME이 같은 다른 복사본은 같은 실습 자원을 가리키므로, 동시에 별도 실습을 띄우려면
이름·서브넷·모든 IP를 각각 다르게 설정하세요.

참고: [REFERENCES](../REFERENCES.md) R13/R14/R15.

## 수정판 검증 보고서와 중단 처리

`./lab.sh validate --yes`는 실습용 전체 검사입니다. `output/live-validation.json`의 `status`를 확인하세요.
`BLOCKED`/`RUNNING`/`FAIL`은 성공이 아닙니다. `./lab.sh test`도 매번 보고서를 새로 작성합니다.
`verify`가 성공한 쓰기 0개를 보고하면 클라이언트가 연결/인증/역할 문제로 모든 요청을 실패했는지 확인하세요.
설정 파일은 있지만 fingerprint가 사라진 경우 무조건 config를 덮어쓰지 말고, 기존 .env와 matching volume state를 보존/복원하세요.
