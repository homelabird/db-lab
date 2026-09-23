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

`./lab.sh up`은 Elasticsearch 5노드가 모두 합류하고 cluster health가 green이 될 때까지
기다린 뒤 snapshot과 Kibana를 확인합니다. `_na_`인 채 멈추면 다음을 확인합니다.

```bash
./lab.sh compose ps
./lab.sh compose logs --tail=200 es01 es02 es03 es04 es05
```

- `master not discovered yet` / transport 연결 실패 / bootstrap check / Exit 137(메모리)
  / 일부 컨테이너 재시작 여부.
- `cluster.initial_master_nodes`는 빈 데이터 볼륨을 처음 기동할 때만 주입됩니다.
  기존 노드는 `discovery.seed_hosts`를 통해 합류합니다. 볼륨을 임의로 일부 삭제하지 마세요.
- 클러스터 데이터를 완전히 버려도 될 때만 `./lab.sh down --purge --yes && ./lab.sh up`을
  실행하세요. 이 명령은 모든 프로젝트 데이터 볼륨을 삭제합니다.

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

## 5. 재시작 시 bootstrap 재주입 방지

`cluster.initial_master_nodes`는 빈 데이터 볼륨의 최초 기동에만 사용됩니다. 이후에는
`discovery.seed_hosts`를 통해 기존 클러스터에 합류합니다. 재시작 후 노드가 서로 다른
cluster UUID를 보이거나 master를 찾지 못하면 로그를 확인하고 볼륨을 임의로 삭제하지 마세요.

## 6. Kibana가 ready가 되지 않음

```bash
./lab.sh logs kibana --tail 150
./lab.sh verify-install
```

보안 모드에서는 `.env`의 `KIBANA_PASSWORD`가 Elasticsearch `kibana_system` 계정에
설정한 비밀번호와 일치해야 합니다. 로그인 계정은 `KIBANA_LOGIN_USERNAME`과
`KIBANA_LOGIN_PASSWORD`입니다. 로그의 첫 ES 연결 오류를 확인하세요.

## 7. Kibana가 ES에 등록하지 못함(exit 78)

`value of elastic is forbidden ... Use a service account token` 이면
`KIBANA_USERNAME=kibana_system`(기본값)이 `.env`에 있는지 확인합니다. `elastic`
superuser를 username으로 쓰는 ES 9 방식은 테스트용입니다.
Kibana 대시보드가 뜨지만 인덱스가 없을 때는 `${KIBANA_SERVERNAME}`/포트가 ES와 안
맞을 수 있습니다.

## 8. rootless Podman/Docker 주의사항

컨테이너 프로세스는 이미지 기본 USER로 뜹니다(uid 매핑은 데몬 설정에 따라 다름).
`docker info`의 사용자 이름과 볼륨 소유(0:0)를 확인하고, chown이 필요한 자원은
`snapshot-init`처럼 `user: "0:0"` 1회성 서비스로 해결하세요. ES 노드에 `user: 0`을
줘서 root 실행하는 것은 ES 8/9가 거부하므로 쓰지 마세요.
