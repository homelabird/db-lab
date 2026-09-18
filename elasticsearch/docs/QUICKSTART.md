# 시작·환경 설정

## 1. 필요 도구

Linux Bash, Python 3.9 이상, curl 7.76 이상(`--fail-with-body` 사용), Podman과 Compose provider가 필요합니다. Fedora 계열의 예시는 다음과 같습니다. 패키지 설치는 호스트 관리 권한으로 수행합니다.

```bash
sudo dnf install podman podman-compose python3 curl unzip
```

Docker Compose가 이미 있는 환경도 지원합니다. `.env`의 `COMPOSE_PROVIDER`에 `auto`, `podman-compose`, `podman`, `docker` 중 하나를 지정합니다. `auto`는 podman-compose → podman compose → docker compose 순으로 선택합니다. 모든 관리 명령을 같은 일반 사용자로 실행하세요. rootless Podman과 `sudo podman`은 컨테이너·볼륨 저장소가 다릅니다.

## 2. 호스트 설정과 기동

```bash
cp .env.example .env
chmod +x scripts/*.sh scripts/*.py scenarios/*.sh
sudo sysctl -w vm.max_map_count=262144
./lab.sh doctor
./lab.sh compose config
./lab.sh up
```

`vm.max_map_count`는 **호스트/VM 커널**에서 설정해야 합니다. 재부팅 후에도 유지하려면 호스트의 sysctl 설정으로 관리합니다. 이 랩은 mmap을 사용하며, `sysctl` 문제를 해결하기 위해 보안 기능을 끄거나 컨테이너를 무조건 privileged로 실행하지 않습니다.

`01-up.sh`는 단순히 `/`가 HTTP 200을 반환하는지만 보지 않습니다. 실제 cluster UUID가 생겼는지, 최소 5개 노드인지, 상태가 yellow 이상인지 확인합니다. `cluster_uuid: "_na_"`라면 서버 프로세스가 떠 있어도 클러스터 형성에 성공한 상태가 아닙니다.

```bash
./lab.sh compose ps
./lab.sh compose logs --tail=120 es01 es02 es03
./lab.sh status
```

첫 실행에는 컨테이너 이미지를 다운로드할 수 있는 네트워크가 필요합니다. 시드 데이터 생성 자체는 외부 다운로드가 없습니다.

## 3. 데이터와 검색

```bash
./lab.sh seed
./lab.sh verify
./lab.sh size
./lab.sh query list
./lab.sh query 01-latest
```

전체 설정은 `.env.example`, 시드 인자는 `./lab.sh seed --help`에서 확인합니다. `seed`는
세 인덱스에 총 약 100MiB의 결정적 합성 데이터를 생성하고 완료 후
`reports/seed-manifest.json`을 기록합니다. 적재 완료 후에도 `./lab.sh verify`까지
실행해야 매핑·샤드·검색 예제 검증이 끝납니다. CLI 인자가 환경변수보다 우선하며,
`.env`에는 `KEY=value`만 사용하고 inline 주석, 변수 치환, 쉘 명령을 넣지 마세요.

## 4. 기존 랩과 포트가 겹칠 때

새 `.env`를 다음처럼 바꾸면 기존 9200/9000과 겹치지 않습니다.

```dotenv
ES_PORT=19200
CEREBRO_PORT=19000
ES_URL=http://127.0.0.1:19200
```

Cerebro와 Kibana의 내부 ES 연결 주소는 모두 `http://es01:9200`입니다. 두 UI는 서로 다른 호스트 포트(기본 Cerebro `9000`, Kibana `5601`)를 사용하므로 동시에 실행할 수 있습니다. 호스트에 공개하는 포트와 컨테이너 네트워크 내부 포트는 다릅니다. 새 프로젝트는 기존 랩의 볼륨을 자동 재사용하지 않습니다. 옛 인덱스를 유지한 채 새 시드만 넣는 마이그레이션 도구가 아닙니다.

## 5. 다른 PC에서 서버의 Cerebro 보기

### 기본값: 모든 IPv4 인터페이스에 바인딩

`.env.example`과 Compose의 fallback 기본값을 모두 다음과 같이 설정했습니다. `ES_BIND_IP`는 이름과 달리 **Elasticsearch와 Cerebro의 호스트 공개 포트 모두**에 적용됩니다.

```dotenv
ES_BIND_IP=0.0.0.0
ES_PORT=9200
CEREBRO_PORT=9000
KIBANA_PORT=5601
ES_URL=http://127.0.0.1:9200
```

`0.0.0.0`은 수신 인터페이스 지정이고, `0.0.0.0/0`은 모든 IPv4 주소를 포함하는 CIDR 대역 표기입니다. `.env`의 바인딩 값에는 `/0`을 붙이지 않습니다. 브라우저는 `0.0.0.0`이 아니라 **서버의 실제 IP**로 접속합니다. 예를 들어 서버 IP가 `192.168.1.100`이라면:

```text
Cerebro:       http://192.168.1.100:9000
Kibana:        http://192.168.1.100:5601
Elasticsearch: http://192.168.1.100:9200
```

서버 자체에서 시드·검색 스크립트를 실행할 때는 `ES_URL=http://127.0.0.1:9200`을 그대로 사용합니다. 다른 PC에서 API에 요청할 때만 `ES_URL=http://192.168.1.100:9200`처럼 실제 서버 IP를 지정합니다. Cerebro 내부의 Elasticsearch 주소는 계속 `http://es01:9200`입니다. es02~es05와 노드 간 통신 포트 9300은 호스트에 추가 공개하지 않았습니다.

**로그인 인증/TLS가 없는 실습 환경입니다.** 9000/9200에 접근할 수 있는 사용자는 조회뿐 아니라 설정 변경·데이터 삭제도 할 수 있습니다. 모든 인터페이스에 바인딩하는 것과 방화벽에서 모든 출발지(`0.0.0.0/0`)를 허용하는 것은 다릅니다. 호스트 방화벽과 클라우드 보안그룹은 신뢰하는 실습 PC의 IP만 허용하고, 공유기의 인터넷 포트포워딩을 설정하지 마세요. 이 프로젝트는 방화벽을 자동 변경하지 않습니다. IPv6 바인딩을 추가하는 설정도 아닙니다.

### 기존 설치에 적용하기

이미 만든 `.env`는 새 `.env.example`이나 Compose fallback보다 우선합니다. 프로젝트 디렉터리에서 아래처럼 **바인딩 값만** 변경하면 기존 seed·포트 등 다른 설정을 유지할 수 있습니다.

```bash
# .env가 없다면 새 예제로 생성
[ -f .env ] || cp .env.example .env

# 기존 키를 수정하거나, 없으면 추가
if grep -q '^ES_BIND_IP=' .env; then
  sed -i 's/^ES_BIND_IP=.*/ES_BIND_IP=0.0.0.0/' .env
else
  printf '\nES_BIND_IP=0.0.0.0\n' >> .env
fi

# 이전에 export한 값이 .env를 덮어쓰지 않도록 현재 쉘에서 해제
unset ES_BIND_IP
./lab.sh compose config

# 단순 restart가 아니라 재생성해야 호스트 포트 바인딩에 반영됩니다.
# es01이 잠시 재시작합니다. 기존 named volume과 seed 데이터는 보존합니다.
./lab.sh compose up -d --force-recreate es01 cerebro
./lab.sh status
```

ZIP은 기존과 동일한 프로젝트 폴더 이름을 유지합니다. 기존 볼륨을 이어 쓰려면 원래 프로젝트 디렉터리에 변경 파일을 반영하고 `COMPOSE_PROJECT_NAME`을 유지하세요. 기존 `.env`를 무조건 새 예제로 덮어쓰지 마세요. 시드를 다시 채울 필요는 없으며 `down -v`나 `--purge`는 실행하지 않습니다.

### 선택: 로컬 바인딩 + SSH 터널

외부 직접 접속이 필요 없으면 `.env`를 `ES_BIND_IP=127.0.0.1`로 바꾼 뒤 위의 재생성 명령을 실행합니다. Windows PowerShell에서:

```powershell
ssh -N -L 9000:127.0.0.1:9000 -L 9200:127.0.0.1:9200 server@서버주소
```

그 다음 Windows 브라우저에서 `http://127.0.0.1:9000`에 접속합니다. 포트를 바꿨다면 터널 대상 포트도 변경합니다. 특정 내부 IP에만 바인딩하려면 `ES_BIND_IP`를 그 서버 인터페이스의 실제 IPv4 주소로 지정할 수도 있습니다.

## 6. 중지와 재시작

```bash
# 중지·컨테이너 제거, 볼륨의 ES 데이터 유지
./scripts/02-down.sh

# 다시 기동. 유지된 데이터에 seed를 다시 넣을 필요는 없음
./lab.sh up
```

완전 폐기할 때만:

```bash
# 확인 인자 없이는 실행되지 않습니다. ES 데이터 볼륨 6번째 노드용까지 삭제.
./scripts/02-down.sh --purge --yes
```

`cluster.initial_master_nodes`는 새 클러스터 최초 bootstrap용입니다. 이 랩의 간단한 재생성용 Compose 패턴을 운영 설정에 그대로 복사하지 마세요. 기존 클러스터 복구를 새 클러스터 bootstrap으로 대체하거나, 일부 볼륨만 임의 삭제해서는 안 됩니다.

공식 근거: [Important Elasticsearch configuration](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/important-settings.html).
