# Redis Sentinel 장애 대응 실습실

**Podman Compose · Redis 3대 · Sentinel 3대 · Python 클라이언트 1대 · 1.1.0-reviewed**

> 수정판 검증: 단위/회귀 검사 107개 PASS. 작성 환경의 실제 Podman/Redis 검증은 실행기 부재로 BLOCKED입니다.
> [검증 범위](TESTING.md)와 [수정 내용](CHANGELOG.md)을 확인하세요. 실구동 성공을 확인한 파일로 표시하지 않습니다.

기본 명령, 복제, 자동 장애조치, quorum 부족, 복제 인증 오류, 메모리 쓰기 거부,
RDB 저장 실패와 백업 복원을 직접 연습하는 프로젝트입니다. CLI 중심이며 웹 UI는 없습니다.
실습용 합성 데이터만 생성합니다. 운영 서버나 실제 고객 데이터를 연결하지 마세요.

## 1. 바로 실행

Linux 호스트에서 압축을 풀고 실행합니다. 설치부터 종료까지 **같은 일반 사용자**로 진행하세요.
`sudo podman`과 일반 `podman`을 섞지 마세요.

```bash
unzip redis-sentinel-lab-reviewed.zip
cd redis-sentinel-lab
chmod +x lab.sh
./lab.sh up
```

`up`은 `.env`와 무작위 비밀번호 생성 → 사전 점검 → 이미지 2종 빌드 → 기본 7개 컨테이너 기동 →
복제·Sentinel·실제 읽기/쓰기 검증을 수행합니다. 초기 빌드에는 인터넷이 필요합니다.
Redis 이미지와 Python 클라이언트 의존성은 ZIP에 포함되어 있지 않습니다.

정상 기동 후:

```bash
./lab.sh status
./lab.sh demo
./lab.sh seed --users 2000 --payload-bytes 256
./lab.sh cli master SET lab:hello world
./lab.sh cli master GET lab:hello
./lab.sh test
```

`test`는 **현재 Master를 실제로 강제 종료**하고 새 Master 선출·기준 데이터 보존·기존 노드의 Replica 복귀를
검사합니다. 성공 여부는 실행 환경에서 판정하며, 결과는 `output/failover-test.json`에 저장됩니다.
중간에 종료했으면 `./lab.sh faults`와 `./lab.sh recover`로 확인·복구하세요.

## 전체 실구동 검증을 한 번에 실행

```bash
./lab.sh validate --yes
cat output/live-validation.json
```

이미지 빌드부터 장애 주입·복구·백업 복원·재기동까지 수행합니다. `--yes`는 현재 실습에 장애를 주입하고
**redis-sandbox의 데이터를 교체**하는 데 동의한다는 의미입니다. 다른 터미널에서 동시에 실습을 조작하지 마세요.
PASS/FAIL/BLOCKED를 구분하고 `output/live-날짜-고유ID/`에 단계별 로그를 남깁니다.
이미지 다운로드·빌드 실패도 보고하며, 검사에 통과했다고 위장하지 않습니다.

### 기존 1.0 환경에 덮어쓰는 경우

기존 `redis-sentinel-lab/`의 `.env`, `.lab/`, `output/`은 유지하고 소스 파일만 덮어쓰세요.
ZIP에는 `.env`와 `.lab/`을 넣지 않았습니다. 새 디렉터리에서 비밀번호를 새로 만든 채 기존 rslab 볼륨에
접속하면 의도적으로 환경 불일치 오류가 납니다. 기존 비밀번호/상태를 먼저 보존해야 합니다.
수정판 이미지 태그는 `:1.1`이므로 최초에는 `up` 또는 `validate --yes`를 사용하여 다시 빌드하세요.
처음부터 `--no-build`로 구버전 이미지를 재사용하지 마세요.

## 2. 준비 사항

필요한 명령은 `podman`, `podman-compose`, `python3`(3.9 이상), `bash`, 압축 해제용 `unzip`입니다.
Podman 4/5 계열과 rootless cgroup v2를 대상으로 설계했으며, Compose provider는 `--in-pod=false`,
profiles, build target을 지원하는 버전을 사용하세요. 오래된 provider에서는 업그레이드가 필요할 수 있습니다.

Fedora 예시:

```bash
sudo dnf install -y podman podman-compose python3 unzip
podman --version
podman-compose --version
```

Ubuntu/Debian 예시:

```bash
sudo apt update
sudo apt install -y podman podman-compose python3 unzip
podman --version
podman-compose --version
```

이 설치 명령은 일반적인 배포판 패키지 사용 예입니다. 배포판 버전에 따라 제공되는 Compose 버전이 다릅니다.
이미 설치된 환경에서는 재설치하지 말고 `./lab.sh doctor`부터 실행하세요.

호스트 여유 메모리는 **약 3 GiB 이상을 실습 예산**으로 잡으세요. 기본 컨테이너 메모리 제한의 합은
약 2.06 GiB이고, 별도 sandbox를 띄우면 512 MiB가 추가됩니다. 이는 실제 상주 메모리 측정값이 아닙니다.
OS·이미지 빌드·다른 실습환경의 메모리는 별도로 필요합니다. 단일 호스트 장애는 견디지 못합니다.

## 3. 구조

| 서비스 | 초기 역할 | 기본 내부 주소 | 내부 포트 |
|---|---|---|---|
| redis-1 | Master | 10.89.77.11 | 6379 |
| redis-2 | Replica | 10.89.77.12 | 6379 |
| redis-3 | Replica | 10.89.77.13 | 6379 |
| sentinel-1 | 감시/장애조치 | 10.89.77.21 | 26379 |
| sentinel-2 | 감시/장애조치 | 10.89.77.22 | 26379 |
| sentinel-3 | 감시/장애조치 | 10.89.77.23 | 26379 |
| lab-client | 실습/검증 | 10.89.77.30 | 공개 없음 |
| redis-sandbox | 선택 실행, 독립 Redis | 10.89.77.40 | 6379 |

기본 컨테이너 이름은 `rslab-redis-1`처럼 `rslab-`이 붙습니다. 역할은 바뀌어도 이름은 바뀌지 않습니다.
Sentinel은 `mymaster`라는 논리 이름을 감시합니다. Redis Cluster/샤딩 구성이 아닙니다.

Sentinel이 중단된 노드도 주소로 추적할 수 있도록 내부 IP를 고정했습니다.
서비스 DNS는 보조로 사용할 수 있지만 Sentinel의 영속 설정은 노드 DNS 생존에 의존하지 않습니다.
**호스트 포트는 하나도 publish하지 않습니다.** 따라서 다른 실습의 6379·9000·9200 등과 직접 충돌하지 않습니다.
Windows 브라우저나 호스트의 `localhost:6379`로 접속하는 구성이 아닙니다.
`./lab.sh cli ...` 또는 컨테이너 안에서 접근하세요.

기본 네트워크가 LAN/VPN/다른 Podman 네트워크와 겹치면 `doctor`가 중단합니다.
최초 기동 전에 `.env`의 `LAB_SUBNET`과 **8개 IP 항목을 모두** 변경하세요.
사용 중인 볼륨을 유지한 채 주소를 바꾸지 마세요.

## 4. 가장 먼저 할 장애 실습

터미널 A — 쓰기를 계속 발생시킵니다.

```bash
./lab.sh workload --seconds 120 --rate 5
```

터미널 B — 초기 쓰기 성공을 확인한 후 현재 Master를 종료합니다.

```bash
./lab.sh master
./lab.sh fault kill-master
./lab.sh status
./lab.sh logs sentinel-1 --tail 150
```

새 Master가 선출되어 터미널 A의 쓰기가 재개되는지 관찰합니다.
기존 Master를 복구하고 전체 복제/감시 상태를 검사합니다.

```bash
./lab.sh recover
./lab.sh status
```

터미널 A의 workload가 끝난 다음:

```bash
./lab.sh verify latest
./lab.sh results
./lab.sh collect
```

`verify`는 성공 응답을 받은 키, 결과가 불확실했던 키, 명시적으로 거부된 키를 구분해서 확인합니다.
요청 또는 성공 응답을 받은 쓰기가 0개이면 실패로 처리합니다. 고정 노드가 장애 난 동안의 실험이라면 이 실패가 예상 결과일 수 있습니다.
시험 한 번에서 데이터가 남았다고 모든 장애에서 무손실을 보장하는 것은 아닙니다.

## 5. 명령 요약

| 작업 | 명령 |
|---|---|
| 실행 전 설정 생성/점검 | `./lab.sh init` / `./lab.sh doctor` |
| 기동/재기동 | `./lab.sh up` / `./lab.sh up --no-build` |
| 상태 및 현재 Master | `./lab.sh status` / `./lab.sh master` |
| 기본 자료구조 실습 | `./lab.sh demo` |
| 합성 데이터 | `./lab.sh seed --users 10000 --payload-bytes 512` |
| CLI 접속 | `./lab.sh cli master` / `./lab.sh cli redis-2` |
| Sentinel 조회 | `./lab.sh cli sentinel-1 SENTINEL MASTER mymaster` |
| 반복 쓰기 | `./lab.sh workload --seconds 120 --rate 5` |
| 고정 노드 접속 비교 | `./lab.sh workload --mode fixed --fixed-node redis-1 --seconds 120` |
| 동일 연결의 WAIT 비교 | `./lab.sh workload --wait-replicas 1 --seconds 120` |
| 자동 장애조치 시험 | `./lab.sh test` |
| 전체 실구동 검증 | `./lab.sh validate --yes` |
| 현재 Master 강제 종료/정지 | `./lab.sh fault kill-master` / `./lab.sh fault pause-master` |
| 특정 노드 정상 종료 | `./lab.sh fault stop sentinel-2` |
| 특정 노드 네트워크 단절 | `./lab.sh fault isolate redis-2` |
| Replica 인증 오류 | `./lab.sh fault auth-replica redis-2` |
| 장애 기록/복구 | `./lab.sh faults` / `./lab.sh recover` |
| 로그 | `./lab.sh logs sentinel-1 --follow` |
| Redis 메모리 쓰기 거부 | `./lab.sh sandbox-oom` |
| RDB 저장 실패/MISCONF | `./lab.sh sandbox-persistence` |
| sandbox 복구 | `./lab.sh sandbox-recover` |
| 현재 Master의 RDB 백업 | `./lab.sh backup` |
| sandbox에만 복원 | `./lab.sh restore output/backups/파일명.rdb --yes` |
| 결과/진단 자료 추출 | `./lab.sh results` / `./lab.sh collect` |
| 컨테이너 삭제, 데이터 유지 | `./lab.sh down` |
| 이 실습의 볼륨까지 초기화 | `./lab.sh reset --yes` |

`./lab.sh cli master`는 **시작할 때만** 현재 Master를 찾습니다. 열린 redis-cli 세션이 자동으로
새 Master를 따라가지는 않습니다. 자동 전환 실습에는 Sentinel-aware `workload`를 사용하세요.
`auth-replica redis-2`는 redis-2가 실제 Replica일 때만 허용합니다.

## 6. 학습 문서

- [기본 명령과 자료구조](docs/01-basics.md)
- [복제, offset, RDB/AOF, WAIT](docs/02-replication.md)
- [Sentinel 장애조치와 quorum 실습](docs/03-sentinel.md)
- [장애 증상별 대응 절차](docs/04-incidents.md)
- [백업과 독립 복원](docs/05-backup-restore.md)
- [운영·권한·Podman 문제 해결](docs/06-operations.md)
- [기능 범위와 제외 항목](docs/07-scope.md)
- [실습 기록지](docs/worksheet.md)
- [검증 범위와 실행 결과](TESTING.md)
- [공식 참고 자료](REFERENCES.md)

## 7. 보존과 안전

Redis 데이터·runtime config, 각 Sentinel 설정, 클라이언트 결과는 모두 개별 named volume입니다.
호스트 디렉터리 bind mount, `privileged`, SELinux 비활성화, 전역 `podman system prune`은 사용하지 않습니다.
초기 설정은 빈 볼륨에서 한 번만 생성하고, 이후 재기동에서는 바뀐 Master/Replica 구성을 유지합니다.

비밀번호·IP·Master 이름이 기존 볼륨과 달라지면 자동으로 덮어쓰지 않고 중단합니다.
`.env`, `.lab/`를 실습 도중 삭제하지 마세요. `reset --yes`는 이 실습의 데이터와 클라이언트 내부 결과도 지웁니다.
`output/`으로 내보낸 파일과 `.env`는 남깁니다.

이 프로젝트의 계정은 학습 편의를 위한 관리자 권한입니다. 인증은 있지만 TLS/세분화 ACL/운영 보안 설계는
별도 과제입니다. 이미지는 최초 기동 시 받아야 하므로 ZIP만으로 완전 오프라인 설치되지는 않습니다.
