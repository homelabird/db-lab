# 장애 스크립트 검증 결과와 한계

## 판정

**스크립트의 정적 검사·오프라인 회귀 검사·모의 HTTP/Compose 통합 검사: PASS.**

**실제 Elasticsearch 5노드, 실제 Podman/Docker, Cerebro 브라우저 화면, 실제 장애 복구 시간 측정: NOT RUN.** 이 문서는 모의 서버를 실제 Elasticsearch 실행으로 표현하지 않습니다. 가짜 API의 판정이 실제 ES allocator, Lucene 복구, master 선출 구현을 증명하지도 않습니다.

제작 환경에는 Podman/Docker 실행 파일과 runtime socket이 없었습니다. Java는 있지만 ES 실행 배포본은 없고 공식 배포 주소의 DNS 해석도 실패했습니다. 실제 클러스터 실행을 검증했다고 주장할 근거가 없습니다. 사용자 환경에서 `13-fault-lab.sh run ... --yes`를 실행해야 실제 API와 컨테이너 명령에 대한 결과가 만들어집니다.

## 이번에 실행한 검사

**총 76개 테스트의 개별 PASS를 확인했습니다: 기존 25개 + 장애 관련 51개. Bash 30개 파일의 문법, Python 13개 파일의 compile, Compose YAML 2개 파일의 구문도 검사했습니다.** YAML 구문 확인은 `podman-compose config` 실행이나 Compose 호환성 인증과 다릅니다.

전체 suite 실행은 제작 도구의 wall-clock 제한으로 종료되었으므로, 그 로그에서 완료된 69개 PASS만 집계하고 기존 시드 suite 21개를 별도 완료 실행했습니다. 중복을 제거한 76개 test ID가 discovery 결과와 정확히 일치하는지 대조했습니다. 즉 “하나의 전체 실행이 정상 종료했다”가 아니라 **최종 코드의 모든 개별 테스트를 분할 검증한 결과**입니다. 중간 로그도 숨기지 않고 첨부했습니다.


최종 수와 소요시간은 `../reports/validation/offline-tests.txt`의 실제 unittest 결과 및 `offline-summary.json`을 기준으로 합니다. 기존 시드·검색·네트워크 기본값 테스트 25개와 새 장애 기능 테스트를 함께 실행했습니다.

| 계층 | 수행 내용 | 해석 |
|---|---|---|
| Bash 문법 | `scripts/*.sh`, `scenarios/*.sh` 전체 `bash -n` | 구문 오류 점검. 명령의 실제 런타임 호환을 보증하지 않음 |
| Python 문법 | 전체 Python 파일 compile | import/REST/실제 ES 의미론 검증과는 별개 |
| 기존 회귀 | 시드 생성·중복 방지·Bulk 실패·PIT·0.0.0.0 기본값 | 기존 기능 손상 여부를 오프라인으로 검사 |
| 장애 기능 단위 | 11개 시나리오의 주입·판정·복구, 원래 설정 보존 | 상태를 가정한 FakeES 사용 |
| 실제 HTTP → 모의 서버 | write-block 403, canary red 검색 503, replica 과다, allocation=none | 실제 HTTP 전송·본문·상태 코드 처리. ES 엔진 아님 |
| 실제 Bash → Python → 모의 provider | 기본 8개 suite, 기존 번호별 wrapper, apply/check/recover | 실제 셸 인자 전달·종료 코드·report 검사 |
| 실제 Bash → 모의 Compose → 실제 curl → 모의 HTTP | es01 접속점 실패 시 생존 노드 경유 | 명령 체인/응답 파싱 검사. 컨테이너는 띄우지 않음 |

새 테스트는 `tests/test_fault_lab.py`, provider 대역은 `tests/fake_compose_provider.py`입니다. **대역 provider는 테스트에서만 PATH에 넣으며 실제 Podman 대체물로 설치하지 마세요.** 최종 ZIP에는 실제/모의 실행 중의 `active.json`, 사용자 시드 본문, fake cluster 결과를 실제 검증 결과로 오해할 수 있는 `suite-PASS` 파일을 넣지 않습니다.

## 실패 경로도 검사

cluster.name 불일치, `_na_`, cluster UUID 변경, 부족한 노드, 잘못된 node/index 인자, `_shards.failed>0`, `_nodes.failed>0`, HTTP 200의 `timed_out=true`, 예기치 않은 쓰기 성공, 의도한 403 대신 발생한 401, 문서 수 변경, 동일 문서 수에서의 표본 내용 변경, 설정 변경 실패, Ctrl-C, 노드 재기동 실패, 다른 실험 소유의 canary, 동시 실행/남아 있는 journal, 다른 Compose project를 거부하는지 검사했습니다.

원복이 실패하면 journal을 보존하고 재실행 가능한지, 다른 wrapper의 `--recover`로 엉뚱한 실험을 원복하지 않는지, 수동 `check`가 실패했는데 이후 최종 보고서를 PASS로 바꾸지 않는지도 검사했습니다. canary 생성 때문에 shard가 재배치될 수 있으므로 노드 정지 직전에 primary 목록을 새로 저장하는 회귀 테스트도 있습니다.

## 기존 ZIP에서 확인한 내용과 수정

기준 파일: `elasticsearch-cerebro-lab-5nodes-seed-query-bind-all.zip`.

| 기존 파일/구현 | 확인 내용 | 이번 변경 |
|---|---|---|
| `06-node-failure-and-recovery.sh` | recover의 `wait_es` 기본 최소 노드가 1. 대상 노드 재합류·green·데이터 보존 검증 없음 | 원래 노드 이름 목록 전체·green·미할당/초기화/이동 0·읽기/쓰기·시드 보존 확인 |
| 같은 파일 | 시작 부분의 `guard_lab`은 호스트 ES_URL 단일 경로에 의존. es01 정지 시 recover 진입도 어려움 | 생존 컨테이너 API 경유와 UUID 확인 추가 |
| 같은 파일 | `curl ... | head -80`에 pipefail 사용. 출력 크기/파이프 타이밍에 따라 SIGPIPE/curl 오류로 안내 전에 종료할 가능성 | JSON 전체 응답 처리로 교체 |
| 같은 파일 | `compose stop`만 사용 | 정상 종료 요청과 SIGKILL 시나리오 분리 |
| `03`, `02/07/08/10/11` 원복 | replica=1/2 또는 null로 고정 복원. 사용자 실험 전 값과 다를 수 있음 | persistent/transient와 인덱스의 변경 전 값을 journal로 보관하고 정확히 복원 |
| `05-impossible-allocation-filter.sh` | 이미 할당된 인덱스에 ghost 필터를 걸어도 즉시 red가 된다고 보장하지 않음. 기존 문서도 그 한계를 언급 | 새 canary를 ghost 필터로 처음부터 생성하여 미할당 primary와 검색 오류를 검사 |
| `07-disable-allocation.sh` | allocation만 none으로 만들면 기존 shard가 남아 있어 장애 징후가 나타나지 않을 수 있음 | canary replica를 추가하여 실제 미할당·enable decider 검사 |
| `11-disk-watermark-simulation.sh` | 고정 200GB/150GB는 여유 공간이 큰 호스트에서 효과가 없을 수 있음. 2초 sleep은 disk 정보 반영을 확인하지 않음 | 현재 free bytes 기반 threshold 계산, 실제 미할당/decider 조건까지 polling |
| `04-allocation-explain.sh` | shard 0 replica 고정 기본값 | 현재 UNASSIGNED 자동 선택, 없으면 명시. SHARD/PRIMARY 명시 시 기존 지정 조회도 지원 |
| `09-cancel-replica-recovery.sh` | 텍스트 파싱 후 요청 제출, 원복 상태 검증 없음 | JSON 파싱·index/shard 검증·dry-run·green/시드 보존 검사 추가 |
| `13-scale-out-node.sh` | 60회 루프 종료 후에도 es06 합류 실패를 성공과 구분하지 않음. remove 오류를 `|| true`로 무시 | 명령 오류 전파, 정확한 노드 목록 및 green까지 확인 |

`02/03/05/06/07/08/10/11` wrapper와 새로운 공통 장애 로직은 모의 실행에 포함했습니다. `01/09/12/13/14`의 실제 ES/컨테이너 실행은 수행하지 않았습니다. 특히 **09와 13은 구문·소스 검토까지이며 새 로직의 실제 REST/Compose 동작은 사용자 환경 검증이 남아 있습니다.**

## 실제 환경에서 확인할 명령

```bash
./scripts/12-offline-tests.sh
./scripts/09-verify-seed.sh
./scripts/13-fault-lab.sh run node-stop --node es03 --yes
./scripts/13-fault-lab.sh run node-crash --node es03 --yes
./scripts/13-fault-lab.sh run master-failover --yes
./scripts/13-fault-lab.sh run all --yes

# 기본 suite에 포함하지 않는 선택 검사
./scripts/13-fault-lab.sh run disk-watermark --yes
./scripts/13-fault-lab.sh run zone-awareness --yes
./scripts/13-fault-lab.sh run rebalance-disabled --yes
```

이후 `reports/faults/*.json`에서 `fault_check.status`, `recovery.status`, `result`를 따로 확인하세요. `active.json`이 남으면 원복이 완료되지 않은 것입니다. 저장된 이전 PASS를 새 실행 결과로 오인하지 않도록 `run_id`와 UTC 시간을 확인하세요.

## 입증하지 않는 것

실제 fault injection 결과가 모두 PASS여도 전체 데이터의 full checksum, 부하 중 모든 acknowledged write 보존, 연속 무중단 서비스, 장시간 soak test, RTO/RPO 수치, 디스크 corruption, 호스트 전원 장애, network partition, JVM OOM, 5노드 중 여러 노드의 동시 장애, 운영 환경 안전성까지 입증하지 않습니다. 현재 도구는 단일 장애의 기능·복구 경로를 학습하고 확인하기 위한 랩입니다.

공식 API 의미의 근거는 [장애 실습 가이드](FAULT-DRILLS.md)의 Elastic 7.17 공식 문서 링크를 참고하세요. 실제 사용 중인 Compose provider에 따라 명령 호환성도 확인해야 합니다. Podman의 `podman compose`는 외부 provider에 명령을 위임합니다: [Podman 공식 문서](https://docs.podman.io/en/latest/markdown/podman-compose.1.html).
