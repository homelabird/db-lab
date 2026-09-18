# 문제 해결

## 1. 우선순위: API → 클러스터 → 인덱스 → 검색 조건

```bash
export ES_URL=http://127.0.0.1:9200
curl --fail-with-body -sS "$ES_URL/"
curl --fail-with-body -sS "$ES_URL/_cluster/health?pretty"
curl --fail-with-body -sS "$ES_URL/_cat/nodes?v"
curl --fail-with-body -sS "$ES_URL/_cat/indices/lab-*?v"
./lab.sh query 01-latest
```

연결 자체가 실패한 상황과 정상 검색이 0건을 반환한 상황은 다르게 진단해야 합니다.

## 2. API는 떠 있는데 cluster_uuid가 _na_

아직 bootstrap이 완료되지 않았을 수 있습니다. 이 상태를 적재 가능한 클러스터라고 취급하지 않습니다.

```bash
./lab.sh compose ps
./lab.sh compose logs --tail=200 es01 es02 es03 es04 es05
```

`master not discovered`, DNS/transport 연결 실패, bootstrap check, 메모리 부족, 일부 컨테이너 재시작 여부를 확인합니다. Compose의 node.name과 discovery.seed_hosts, initial master 이름이 서로 맞는지도 봅니다. 일부 노드만 오래된 별도 cluster UUID를 가진 볼륨으로 시작하면 자동으로 한 클러스터가 되는 것이 아닙니다.

처음부터 버려도 되는 **이 랩 데이터만** 초기화할 때는:

```bash
./scripts/02-down.sh --purge --yes
./lab.sh up
```

실제 운영 데이터 복구법으로 볼륨 삭제를 사용하지 마세요.

## 3. vm.max_map_count / 메모리 / Exit 137

```bash
cat /proc/sys/vm/max_map_count
free -h
./lab.sh compose logs --tail=100 es01
```

이 7.17 mmap 기반 랩에서는 호스트의 vm.max_map_count를 262144 이상으로 준비합니다. Exit 137만으로 OOM을 확정하지 말고 런타임 inspect의 OOM 표시와 호스트 커널 로그를 함께 확인하세요. 5개의 JVM heap 합계만 메모리 요구량이라고 생각하면 부족할 수 있습니다.

자원이 부족하면 우선 다른 컨테이너를 줄이고 랩 VM 메모리를 확보하세요. heap을 크게 바꾸는 것만으로 실제 사용 가능 메모리가 늘어나지는 않습니다. 소량 데이터로 시작하는 방법은 새 시드에서 `--size-mb 5`입니다.

## 4. 포트 충돌 또는 다른 클러스터에 연결

기존 랩을 종료하거나 `.env`의 ES_PORT, CEREBRO_PORT, ES_URL을 일치시켜 변경합니다. `cluster.name`이 다르면 쓰기 스크립트가 거부합니다. 의도한 랩인지 확인하지 않은 채 검사 값을 바꾸어 우회하지 마세요.

브라우저를 다른 PC에서 열었다면 그 PC의 localhost와 서버의 localhost는 다릅니다. 기본 바인딩은 `0.0.0.0`이므로 `http://<서버IP>:9000`에 직접 접속하세요. 브라우저 주소에 `0.0.0.0`이나 `0.0.0.0/0`을 쓰지 않습니다.

접속이 안 되면 `.env`의 `ES_BIND_IP=0.0.0.0`, `./lab.sh compose config`의 두 공개 포트, 호스트 방화벽·클라우드 보안그룹·서버까지의 네트워크 경로를 확인하세요. 이전 `.env`가 `127.0.0.1`이면 새 기본값을 덮어씁니다. 설정을 변경한 후에는 `./lab.sh compose up -d --force-recreate es01 cerebro`로 재생성해야 합니다. 볼륨 삭제는 필요하지 않습니다. 로컬 바인딩을 유지하는 경우에는 `QUICKSTART.md`의 SSH 터널을 사용하세요.

## 5. Cerebro는 열리는데 ES가 안 보임

```bash
./lab.sh compose logs --tail=100 cerebro
./lab.sh compose exec cerebro sh -c 'getent hosts es01 || true'
```

Cerebro 안에서는 호스트의 `127.0.0.1:9200`이 아니라 컨테이너 DNS 이름 `es01:9200`으로 접속합니다. 최소 이미지에는 getent 같은 진단 명령이 없을 수 있습니다. 그 경우 명령 부재를 DNS 실패로 혼동하지 말고 로그와 네트워크 inspect를 확인합니다.

SELinux 환경에서 Cerebro 설정 파일 bind mount는 `:ro,Z`를 사용합니다. 권한 문제가 나면 파일 경로·읽기 권한·라벨·AVC 로그를 확인하세요. SELinux 전체 비활성화를 기본 해결책으로 사용하지 않습니다.

## 6. 구버전 인덱스 또는 다른 크기의 시드가 존재

`older/different dataset`은 보호 동작입니다. 기존 데이터를 자동 삭제하지 않습니다. 데이터가 필요하면 먼저 보존한 뒤 **3개 시드 인덱스를 삭제해도 되는 경우에만**:

```bash
./lab.sh seed --size-mb 100 --recreate --yes
```

`--recreate`만 넣으면 거부하며 `--yes`가 필요합니다. `lab-*` 전체를 지우는 wildcard 삭제가 아니라 정확한 세 이름만 처리합니다.

## 7. 시드가 중간에 실패

```bash
cat reports/seed-manifest.json
./lab.sh status
./lab.sh seed
```

마지막 명령은 실패한 실행과 **같은 설정**으로 다시 수행해야 합니다. 같은 ID에 재적재하므로 문서 수를 중복 증가시키지 않습니다. 설정을 바꾸면 안전 검사에서 거부할 수 있습니다.

429와 502/503/504는 제한 횟수만큼 재시도합니다. 계속 실패하면 클러스터 상태·쓰기 거부·디스크·노드 생존을 점검하세요. 매핑 오류는 무조건 재시도하지 않으며 문제 필드와 타입을 확인해야 합니다.

적재 중 refresh=-1로 바뀌었다가 오류로 복원까지 실패했다면 해당 랩 인덱스에 1s를 복구합니다. 이 작업은 검색 가시성을 복구하기 위한 것이며 누락 문서를 복구하지는 않습니다.

```bash
./scripts/05-reset-cluster-settings.sh
```

이 명령은 다른 랩 장애 설정도 함께 원복하므로 의도적으로 진행 중인 실습이 있으면 주의하세요.

## 8. 검증이 green에서 멈추거나 timeout

```bash
curl --fail-with-body -sS "$ES_URL/_cat/shards/lab-*?v&s=state,index,shard"
./scenarios/04-allocation-explain.sh
./scripts/05-reset-cluster-settings.sh
```

노드가 5개 보인다고 모든 shard가 정상인 것은 아닙니다. replica 과다, allocation=none, exclude/require 필터, 디스크 watermark, 진행 중인 복구를 확인하세요. 검증기는 안정적인 seed 기준 상태를 요구합니다.

## 9. 검색 결과 0건

우선 match_all로 원문이 있는지 확인한 뒤 필터를 한 개씩 추가하세요. 기본 시드는 2026년 8월 UTC입니다. `now-1d`가 항상 맞는 예제가 아닙니다. keyword의 대소문자와 필드 타입을 확인합니다. 이 랩의 `service`, `user_id`에는 `.keyword`를 붙이지 않습니다.

```bash
./lab.sh query 01-latest
./lab.sh query 21-mapping
./lab.sh query 18-validate
```

valid=true는 검색 조건이 유효하다는 뜻이지 결과가 반드시 존재한다는 뜻이 아닙니다.

## 10. 검증 건수가 manifest와 다름

live_load 또는 직접 추가·삭제한 문서가 있는지 확인하세요. 시드 검증은 정확한 기준 데이터 수를 검사합니다. 임의 데이터가 있으면 자동으로 삭제하지 않고 오류를 냅니다. CRUD는 별도 scratch 인덱스에서 연습하면 기준을 유지할 수 있습니다.

## 11. 데이터 크기가 100MiB와 다름

`--size-mb`는 원문 JSON+LF 기준입니다. `pri.store.size`, replica 포함 store, filesystem 사용량을 구분하세요. 생성 파일에는 Bulk action 줄도 있으므로 파일 합계가 더 커집니다. 큰 payload도 색인 구조와 압축의 영향을 받으므로 Primary 저장량을 정확히 100MiB로 맞추는 옵션이 아닙니다.

## 12. 삭제 명령의 범위

```bash
# 시드 인덱스 3개만 삭제. 다른 lab-query-scratch 등은 보존.
./scripts/06-purge-lab-indices.sh --yes

# 이 Compose 프로젝트의 모든 ES 데이터 볼륨 삭제. 완전 폐기 시에만.
./scripts/02-down.sh --purge --yes
```

두 명령 모두 데이터 손실이 발생합니다. 단순 장애 원복은 데이터 삭제 없는 `05-reset-cluster-settings.sh`부터 검토하세요.
