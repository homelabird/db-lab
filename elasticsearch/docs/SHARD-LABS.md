# 5노드 샤드 운영 실습

검색 실습과 `lab.sh verify`를 마친 뒤 진행합니다. 한 번에 하나의 장애 조건만 적용하고 원복하세요. 장애 중에는 검색 예제가 실패하거나 느려지는 것이 정상적인 관찰 대상일 수 있습니다.

새 관리형 장애 도구: [장애 주입·대응·복구](FAULT-DRILLS.md). 기존 번호별 장애 wrapper도 `--yes`로 주입하고 원래 설정을 기록합니다. 아래 표의 `05`는 새 canary에 ghost 필터를 사용하고 `07`은 canary replica 추가까지 수행합니다. `11`은 고정 용량이 아니라 실제 여유 공간으로 threshold를 계산합니다.

`05-reset-cluster-settings.sh` 전에 `13-fault-lab.sh status`를 확인하세요. **새 관리형 실험의 active journal이 있으면 먼저 `13-fault-lab.sh recover`를 사용합니다.** 설정 전체 reset은 실험 직전 값 복원의 대체물이 아닙니다.

## 기본 배치

```bash
./lab.sh status
./scenarios/12-primary-vs-replica.sh
./lab.sh query 16-search-shards
```

시드 인덱스 3개만의 기준은 primary 38개, replica copy 46개, 합계 84개입니다. replica 설정 1은 "샤드당 사본 한 개 추가"이므로 총 copy는 primary 수 × (1 + replica 설정)입니다. audit은 8 × (1 + 2) = 24개입니다.

## 수동 이동

```bash
./scenarios/01-manual-shard-move.sh
```

기본 대상은 거래 인덱스의 shard 0 primary입니다. 현재 그 샤드의 어떤 copy도 갖고 있지 않은 data node를 찾고 `dry_run`으로 이동 가능 여부를 먼저 검사합니다. 기존 copy가 있는 노드를 무작정 목적지로 고르지 않습니다.

```bash
INDEX=lab-web-logs-v1 SHARD=3 ./scenarios/01-manual-shard-move.sh
```

Cerebro와 다음 API를 같이 봅니다. 데이터가 작거나 디스크가 빠르면 이동 상태가 금방 지나갈 수 있습니다.

```bash
export ES_URL=http://127.0.0.1:9200
curl -sS "$ES_URL/_cat/recovery?v&active_only=true"
curl -sS "$ES_URL/_cat/shards/lab-transactions-v1?v&s=shard,prirep"
```

## 유지보수 drain과 갑작스러운 stop 비교

```bash
# 살아 있는 es03에서 샤드를 다른 노드로 이동시키도록 제외
./scenarios/02-drain-node.sh es03 --yes
./scenarios/02-drain-node.sh --restore

# 실제 컨테이너 정지. 기본 es03을 사용해 호스트 API 진입점 es01을 유지
./scenarios/06-node-failure-and-recovery.sh --yes
./scenarios/06-node-failure-and-recovery.sh --recover
```

시드 인덱스의 node-left delayed timeout은 45초입니다. 노드 이탈 직후의 replica 승격·지연 할당·복구를 구분해 관찰합니다. node stop 후 클러스터가 안정화되면 replica 재배치로 다시 green이 될 수도 있으므로 yellow가 영구 유지될 것이라고 가정하지 마세요.

## 과도한 replica로 미할당 만들기

```bash
./scenarios/03-too-many-replicas.sh --yes
./scenarios/04-allocation-explain.sh
./scenarios/03-too-many-replicas.sh --restore
```

스크립트는 현재 data node 수와 같은 수의 replica를 요청합니다. 5노드라면 replica=5, 즉 primary 포함 6 copy가 필요하므로 일부가 할당될 수 없습니다. 6번째 노드를 띄웠을 때도 같은 학습 조건을 만들도록 현재 노드 수를 읽습니다.

## 다른 시나리오

| 실행 | 관찰할 내용 | 원복 |
|---|---|---|
| `05-impossible-allocation-filter.sh` | 새 canary에 ghost 필터를 설정해 red와 filter decider 확인 (시드 필터는 변경하지 않음) | 같은 명령 `--restore` |
| `07-disable-allocation.sh` | allocation=none 상태에서 canary replica를 추가하여 미할당 재현 | `--restore` |
| `08-zone-awareness.sh` | zone-a/b/c에 copy를 분산하려는 배치 규칙 | `--restore` |
| `09-cancel-replica-recovery.sh` | replica copy 취소 후 재할당·동기화 | 자동 복구 관찰 |
| `10-rebalance-control.sh` | 자동 rebalance와 명시적인 이동 구분 | `--restore` |
| `11-disk-watermark-simulation.sh` | 현재 free-space보다 높은 요구량으로 disk decider 확인; 실제 disk-full/flood-stage 주입 아님 | 즉시 `--restore` 또는 전역 리셋 |
| `13-scale-out-node.sh` | es06 합류 후 6노드 재분산 | `--remove` |
| `14-relocation-under-write-load.sh` | 쓰기 중 이동 관찰 안내 | writer 종료 |

위 파일들은 모두 `./scenarios/파일명`으로 실행합니다. 디스크 시뮬레이션은 실제 데이터로 디스크를 가득 채우는 방식이 아닙니다. low/high/flood_stage를 같은 free-space byte 단위로 설정합니다. 적용 전 다른 사용량 조건을 확인하고 끝나면 반드시 되돌리세요.

6번째 노드는 컨테이너를 제거해도 볼륨을 남깁니다. `02-down.sh --purge --yes`는 선택 노드용 볼륨까지 포함해 랩 전체를 폐기합니다. scale-out remove는 drain 없이 제거하는 단순 실습이며 무중단 유지보수 절차와 다릅니다.

## 쓰기 부하

```bash
# 별도 터미널. .env의 ES_URL도 적용됩니다.
RATE=100 ./lab.sh load
```

Ctrl-C로 종료합니다. live load는 시드 외 문서를 추가하므로 그 뒤에는 시드 manifest와 문서 수가 달라져 `lab.sh verify`가 실패할 수 있습니다. 검색 0건·Bulk 실패 등을 처리하면서 검증기를 통과하도록 임의로 expected count를 바꾸지 마세요. 아래 절차로 시드 기준 상태를 다시 만들 수 있습니다.

## 전체 원복

```bash
# 정지한 컨테이너를 먼저 복구
./lab.sh compose start es01 es02 es03 es04 es05
./scripts/05-reset-cluster-settings.sh

# 데이터까지 기본 시드로 되돌릴 필요가 있을 때만. 3개 인덱스 데이터 삭제.
./lab.sh seed --recreate --yes
./lab.sh verify
```

리셋은 이 랩이 사용하는 allocation·disk watermark·replica·refresh 설정을 되돌립니다. 사용자가 별도로 추가한 모든 종류의 클러스터 설정을 무차별 초기화하는 명령은 아닙니다. 운영 클러스터에 사용하지 마세요.

공식 참조: [Cluster reroute](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/cluster-reroute.html), [Allocation explain](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/cluster-allocation-explain.html), [Cluster-level shard allocation](https://www.elastic.co/guide/en/elasticsearch/reference/7.17/modules-cluster.html).
