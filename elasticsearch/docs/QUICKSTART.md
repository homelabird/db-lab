# 시작 및 환경 설정

## 사전 요구사항

Linux/WSL2, Bash, Python 3.9+, curl, 그리고 Docker Compose 또는 Podman Compose가 필요합니다.
추가 Python 패키지는 없습니다. Podman을 쓸 때는 모든 명령을 같은 일반 사용자로 실행하세요.
rootless Podman과 `sudo podman`은 서로 다른 컨테이너·볼륨 저장소를 사용합니다.

`./lab.sh doctor`는 사전 요구사항과 네트워크를 확인합니다. `python3`, `curl`, `iptables`가
없으면 안내를 출력하며, `./lab.sh doctor --install-missing`을 명시하면 지원 패키지 관리자와
sudo로 없는 도구를 설치합니다. `iptables`는 네트워크 방화벽 진단용 선택 도구입니다.

Elasticsearch는 호스트의 `vm.max_map_count >= 262144`가 필요합니다. 낮으면 doctor/up이
호스트에서 실행할 `sysctl` 명령을 보여줍니다. 이 값은 자동 변경하지 않습니다.

## 시작 및 확인

`.env`가 없으면 `doctor`나 `up`이 `.env.example`에서 권한 `600`으로 만듭니다.
기존 `.env`는 덮어쓰지 않습니다. 설정을 바꾸려면 먼저 `.env`를 편집하세요.

```bash
./lab.sh doctor
./lab.sh up
./lab.sh verify-install
./lab.sh seed --size-mb 5
./lab.sh verify
```

`up`은 인증서·저장소 준비, Elasticsearch 5노드 green 확인, snapshot 저장소 검증,
Kibana/Cerebro readiness 순서로 진행합니다. 데이터는 자동 적재하지 않습니다.
문제가 생기면 아래 명령으로 확인합니다.

```bash
./lab.sh status
./lab.sh logs es01 --tail 100
./lab.sh logs kibana --tail 100
./lab.sh doctor
```

## 런타임과 네트워크

`.env`의 `COMPOSE_PROVIDER`는 `auto`, `docker`, `podman`, `podman-compose` 중에서
선택합니다. `auto`는 설치된 provider를 감지합니다. Compose 네트워크의 실제 이름과
subnet은 `doctor` 출력에서 확인합니다.

컨테이너 안에서 Elasticsearch가 응답하지만 노드 간 연결이 실패하면 `up`은 컨테이너
자체 기동 문제와 네트워크 문제를 구분해 진단합니다. subnet을 찾으면 실제 주소를 넣은
iptables 명령을 안내합니다. 방화벽 규칙은 사용자가 검토하고 적용해야 하며 랩이 자동으로
호스트 방화벽을 수정하지 않습니다.

기본 바인딩은 loopback(`127.0.0.1`)입니다. 원격 접속은 SSH 터널을 권장합니다. 격리된
네트워크에서 직접 공개하려면 `.env`에 `ES_BIND_IP=0.0.0.0`과
`ES_ALLOW_PUBLIC_BIND=yes`를 설정해야 합니다. 방화벽에서는 신뢰하는 클라이언트만
허용하고 인터넷 포트포워딩은 설정하지 마세요.

기본 호스트 포트는 Elasticsearch 9200, Cerebro 9000, Kibana 5601입니다. 포트를 바꾸면
`.env`의 해당 포트와 `ES_URL`을 함께 맞춘 뒤 `./lab.sh up`으로 적용합니다.

## 데이터 보존과 삭제

```bash
./lab.sh down                     # 컨테이너 제거, named volume과 데이터 보존
./lab.sh up                       # 기존 데이터로 다시 시작
./lab.sh purge --yes              # 시드 인덱스 5개만 삭제
./lab.sh down --purge --yes       # 프로젝트 볼륨까지 모두 삭제
```

`down --purge --yes`는 Elasticsearch 노드 데이터와 snapshot을 포함한 프로젝트 볼륨을
삭제합니다. 복구할 데이터가 필요한지 확인한 뒤 사용하세요. 프로젝트 이름을 유지하면
기존 Compose 리소스를 이어서 사용합니다.

## 데이터와 검색

`./lab.sh seed`는 약 100MiB의 결정적 합성 데이터를 생성합니다. 외부 데이터셋을
다운로드하지 않으며 실제 개인정보나 거래 기록을 포함하지 않습니다.

```bash
./lab.sh seed --size-mb 5          # 작은 smoke dataset
./lab.sh verify                    # 문서·샤드·쿼리 확인
./lab.sh size                      # Lucene 저장 크기
./lab.sh query list                # 쿼리 예제 목록
```

`.env`는 `KEY=value` 형식으로 작성합니다. 옵션 이름과 기본값은 [프로젝트 README](../README.md)
및 [.env.example](../.env.example)을 참고하세요.

공식 근거: [Elasticsearch 7.17 중요 설정](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/important-settings.html).
