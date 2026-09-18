# 03 · Sentinel과 자동 장애조치

## 0. 상태 읽기

```bash
./lab.sh cli sentinel-1 SENTINEL MASTER mymaster
./lab.sh cli sentinel-1 SENTINEL REPLICAS mymaster
./lab.sh cli sentinel-1 SENTINEL SENTINELS mymaster
./lab.sh cli sentinel-1 SENTINEL CKQUORUM mymaster
./lab.sh cli sentinel-1 SENTINEL GET-MASTER-ADDR-BY-NAME mymaster
```

Sentinel은 프록시가 아닙니다. 클라이언트는 Sentinel에서 Master 주소를 받은 뒤 Redis에 직접 접속합니다.
이 프로젝트의 반복 쓰기는 redis-py의 `Sentinel.master_for()`를 사용합니다.
Sentinel 접속 비밀번호와 Redis 데이터 접속 비밀번호는 서로 구분합니다.

## 1. Master 강제 종료

정상 상태에서 터미널 A:

```bash
./lab.sh workload --seconds 180 --rate 5
```

터미널 B:

```bash
./lab.sh fault kill-master
./lab.sh status
./lab.sh logs sentinel-1 --tail 200
```

로그에서 다음과 같은 이벤트 계열을 찾습니다. 모든 Sentinel에 같은 순서/문구가 동일하게 찍히는 것은 아닙니다.

```text
+sdown       개별 Sentinel의 주관적 장애 판단
+odown       quorum을 충족한 객관적 장애 판단
+try-failover / +elected-leader
+selected-slave / +promoted-slave
+switch-master
```

설정의 down-after 5초는 **총 복구시간 보장값이 아닙니다**.
선출, 승격, 다른 노드 재설정, 클라이언트 재연결 시간이 추가됩니다.
워크로드의 오류 시작/성공 재개 시각으로 관찰한 시간만 기록하세요.

```bash
./lab.sh recover
./lab.sh status
```

돌아온 기존 Master가 새 Master의 Replica가 되는지 확인합니다.
기존 Master라는 이유만으로 강제 재승격시키지 마세요.
workload 종료 후 `verify latest`, `results`, `collect`를 실행합니다.

## 2. Sentinel 한 대 중단

```bash
./lab.sh fault stop sentinel-3
./lab.sh cli sentinel-1 SENTINEL CKQUORUM mymaster
./lab.sh fault kill-master
./lab.sh status
./lab.sh recover
```

정상적으로 상호 발견된 3대 중 2대가 살아 있으면 기본 quorum=2와 다수 승인을 확보할 수 있습니다.
다만 승격 가능한 Replica, 연결 상태 등 다른 조건도 충족해야 합니다.

## 3. Sentinel 두 대 중단: 왜 전환되지 않는가

먼저 `./lab.sh wait`로 전체 정상 상태를 확인하세요. 새 Sentinel이 다른 두 대를 아직 모르는 상태에서
시작하면 의도한 “기존 3대 중 2대 상실” 시험과 달라집니다.

```bash
./lab.sh fault stop sentinel-2
./lab.sh fault stop sentinel-3
./lab.sh cli sentinel-1 SENTINEL CKQUORUM mymaster
./lab.sh cli master SET lab:quorum:still-serving yes
./lab.sh fault kill-master
./lab.sh status
./lab.sh logs sentinel-1 --tail 150
```

관찰: Sentinel 두 대를 잃어도 Master가 살아 있는 동안 데이터 서비스 자체가 즉시 정지하지는 않습니다.
그다음 Master가 죽으면 남은 Sentinel 한 대가 기본 quorum과 3대 기준 다수 승인을 확보하지 못해
자동 장애조치를 진행하지 못하는 상황을 관찰합니다.

quorum은 장애 판단에 필요한 동의 수이고, 리더 선출/장애조치에는 알려진 Sentinel 다수의 승인이 별도로
필요합니다. `quorum=1`로 낮추는 것을 장애 해결의 만능책으로 사용하지 마세요.

복구:

```bash
./lab.sh recover sentinel-2
./lab.sh status
# 선출/승격을 관찰한 다음 모든 남은 장애를 복구합니다.
./lab.sh recover
```

## 4. 컨테이너 pause와 네트워크 단절

```bash
./lab.sh fault pause-master
./lab.sh status
./lab.sh recover
```

pause는 실행 중인 프로세스를 정지시켜 응답하지 않는 상황입니다. rootless/cgroup 환경에서 지원되지
않으면 명령이 실패할 수 있습니다. 그 경우 실제 pause가 성공했다고 가정하지 말고 오류와 상태를 확인하세요.

별도의 회차에서:

```bash
./lab.sh fault isolate master
./lab.sh status
./lab.sh recover
```

이 명령은 대상 하나를 전용 bridge에서 분리합니다. 복구 시 원래 고정 IP와 별칭으로 다시 연결합니다.
호스트 전체 네트워크나 방화벽 규칙을 수정하지 않습니다.
완전 격리 실습으로, “구 Master에 일부 클라이언트만 계속 접근하는 비대칭 partition”과는 다릅니다.

## 5. 고정 접속과 Sentinel 접속 비교

현재 Master 이름을 확인합니다. 예를 들어 redis-2가 Master이면:

```bash
./lab.sh workload --mode fixed --fixed-node redis-2 --seconds 120
```

다른 터미널에서 `fault kill-master`를 실행하고 **기존 노드는 잠시 복구하지 않은 채** 결과를 관찰합니다.
새 Master가 생겨도 고정 접속 클라이언트는 계속 기존 노드를 시도합니다.
기존 노드를 복구한 뒤에도 Replica라면 READONLY/연결 오류를 경험할 수 있습니다.

다음 회차에서 `--mode sentinel`로 동일 절차를 반복합니다. 매회 `recover`, `wait`로 출발 상태를 맞추고,
동시에 여러 workload를 실행할 때는 `latest` 대신 출력된 RUN_ID로 검증하세요.

참고: [REFERENCES](../REFERENCES.md) R1/R8/R9.
