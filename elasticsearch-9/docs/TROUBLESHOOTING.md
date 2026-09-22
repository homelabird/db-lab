# 문제 해결 (es9)

## 1. 우선순위: API → 클러스터 → 스냅샷 저장소

```bash
export ES_URL=http://127.0.0.1:9201
curl --fail-with-body -sS "$ES_URL/"
curl --fail-with-body -sS "$ES_URL/_cluster/health?pretty"
curl --fail-with-body -sS "$ES_URL/_cat/nodes?v"
curl --fail-with-body -sS "$ES_URL/_snapshot/lab-snapshots/_all?pretty"
./lab.sh logs es01 --tail 100
```

연결 실패와, 연결은 되는데 green/yellow가 반환되는 상황을 구분하세요.

## 2. `cluster_uuid`가 `_na_` — 아직 bootstrap 미완료

`./lab.sh up`은 4단계로 진행하며 두 번째 단계가 "yellow 5노드"입니다. `_na_`인 채
멈추면 다음을 확인합니다.

```bash
./lab.sh compose ps
./lab.sh compose logs --tail=200 es01 es02 es03 es04 es05
```

- `master not discovered yet` / transport 연결 실패 / bootstrap check / Exit 137(메모리)
  / 일부 컨테이너 재시작 여부.
- 올바른 **특정 클러스터가 아니면 소프트웨어적으로 `cluster.initial_master_nodes`가
  최초 볼륨에만 주입**됩니다. 일부 노드만 OS/클러스터 오류로 다시 시작될 때는
  `/usr/share/elasticsearch/data/nodes`이 이미 있으면 shim이 주입을 건너뛰므로
  `seed_hosts`로 합류해야 합니다(아래 6번).
- 정리할 수 있는 이 랩 데이터만 초기화할 때는 `./lab.sh reset --purge --yes && ./lab.sh up`.

## 3. 스냅샷 저장소 등록 시 `access_denied_exception`

`PUT /_snapshot/lab-snapshots`가 500 `access_denied_exception`이면 스냅샷 볼륨
디렉터리에 ES 프로세스(uid 1000)가 쓸 수 없다는 뜻입니다. 새 명명 볼륨 root 소유,
rootless Docker, ES9 이미지 기본 USER(elasticsearch/1000)가 결합된 원인이므로
**`snapshot-init` 서비스로 `chown -R 1000:0`을 해야 하며** uid/gid volume 옵션은
rootless local driver가 거부합니다. 다시 등록하려면:

```bash
./lab.sh snapshot
# = curl으로 저장소 재등록 없이 up의 등록·검증 단계만 재실행
```

## 4. 포트 충돌 (9201/5602)

- 호스트 게시 포트는 **es01(+Kibana) 한 곳뿐**입니다. 모든 노드에 같은 호스트 포트를
  게시하면 `port is already allocated`로 죽습니다 — compose.yaml의 5개 서비스가
  es01(9201)/kibana(5602)만 게시하는지 확인.
- 다른 프로세스가 점유하면 `ES_PORT`/`KIBANA_PORT`/`ES_BIND_IP`를 `.env`에서 바꾸고
  `ES_URL`도 함께 맞춥니다.

## 5. 컨테이너 이름 뒤 `-1` 접미사 문제

Compose v2는 서비스 이름에 `-1`을 붙입니다(예: `es9-lab-es01-1`). 진단/출력은
`container_name`을 명시해 일관된 고정 이름(`es9-lab-es01`…)을 사용하므로, provider나
runtime을 바꿀 때도 컨테이너를 고정 이름에서 찾습니다.

## 6. 재시작 시 bootstrap 재주입 방지

`cluster.initial_master_nodes`는 정책상 최초 부팅 전용입니다. 재시작 후 클러스터가
mastership 불안하거나 노드가 서로 다른 cluster_uuid를 가지면: 볼륨은 그대로 두고
`./lab.sh down && ./lab.sh up`을 처음부터 다시 시도하는 대신 로그를 확인합니다.
`rejected execution of MoveToApplierTask`/`master not discovered`가 잦으면
`.env`의 `DISCOVERY_SEED_HOSTS` 값과 compose의 노드 이름이 일치하는지 보고합니다.

## 7. Kibana가 ES에 등록하지 못함(exit 78)

`value of elastic is forbidden ... Use a service account token` 이면
`KIBANA_USERNAME=kibana_system`(기본값)이 `.env`에 있는지 확인합니다. `elastic`
superuser를 username으로 쓰는 ES 9 방식은 테스트용입니다.
Kibana 대시보드가 뜨지만 인덱스가 없을 때는 `${KIBANA_SERVERNAME}`/포트가 ES와 안
맞을 수 있습니다.

## 8. rootless Docker 주의사항

컨테이너 프로세스는 이미지 기본 USER로 뜹니다(uid 매핑은 데몬 설정에 따라 다름).
`docker info`의 사용자 이름과 볼륨 소유(0:0)를 확인하고, chown이 필요한 자원은
`snapshot-init`처럼 `user: "0:0"` 1회성 서비스로 해결하세요. ES 노드에 `user: 0`을
줘서 root 실행하는 것은 ES 8/9가 거부하므로 쓰지 마세요.