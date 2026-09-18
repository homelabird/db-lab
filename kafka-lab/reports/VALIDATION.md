# 검증 기록 — 2026-09-18

## 결과 요약

**오프라인 단위/모의 테스트: 75개 통과. 실제 Podman/Kafka 통합 테스트: 미실행.**

원본 실행 출력은 `static-tests.txt`입니다. 이 파일은 성공 출력을 예시로 꾸며 넣은 것이 아니라 아래 명령을 제작 환경에서 실행한 기록입니다.

```bash
./scripts/test-static.sh
```

## 실제 수행한 검증

| 범위 | 검증 내용 |
|---|---|
| Bash 문법 | `lab.sh`, `scripts/*.sh`에 `bash -n` |
| Python 데이터 | 3종 스키마, 같은 seed/run/time 입력의 재현성, synthetic 표시, key 및 payload |
| Producer 모의 | delivery callback 성공/실패, flush 미완료, queued/delivered 구분, 비정상 전송률 거부, 극저속에서도 duration 상한 준수 |
| Metadata 모의 | broker 수, controller, leader, ISR, 토픽 부재와 오류 상태 |
| 오류 판정 모의 | ISR 부족/크기 제한의 지정 오류, 일반 timeout은 통과시키지 않는지 |
| 소비/조회 로직 모의 | log offset 범위, 빈 파티션, EOF와 유한 snapshot 처리 |
| ZooKeeper 모의 | leader/follower 역할 기반 판단, 미서비스 상태, 단순 프로세스 생존과 구분 |
| Compose 정적 | 3+3 구성, ZK 모드, broker/ZK ID, RF/ISR, 볼륨/레이블, loopback, 권한 옵션 |
| Shell 모의 Podman | 대상 노드 제한, 소유권, kill/stop, recovery start/unpause/connect와 DNS alias |
| 실패 전파 | recover의 health 실패가 성공으로 표시되지 않는지 |
| 안전한 삭제 | reset 명시 확인, 해당 project만 down -v, 일반 down은 볼륨 보존 |
| 명령 전달 | seed/read/kcli 인자, 살아 있는 broker 선택, 다른 컨테이너에 로그 명령 거부 |

정적 YAML 검사는 PyYAML 파싱 및 프로젝트 조건 검사입니다. **실제 podman-compose provider로 `config`를 렌더링한 검증은 아닙니다.** 사용자 호스트의 `./lab.sh doctor`가 그 작업을 수행합니다.

최종 대조에서 cp-zookeeper 7.9.x 공식 템플릿에 일반적인 `ZOOKEEPER_4LW_COMMANDS_WHITELIST` 자동 매핑이 없음을 확인했습니다. 지원되는 Java system property를 `KAFKA_OPTS`에 지정하도록 수정하고 관련 정적 검사를 추가했습니다. 기존 health 조회는 기본 허용 명령인 `srvr`를 사용하지만, 다른 진단 명령 허용도 설정 의도에 맞췄습니다. 근거는 `docs/SOURCES.md`의 S3/S12입니다.

## 수행하지 못한 검증

제작 컨테이너에는 Podman/Docker 런타임이 없습니다. 따라서 이미지 pull·client image build, Confluent 이미지의 실제 시작, rootless DNS/cgroup/SELinux, 볼륨 실제 소유권 조정, ZooKeeper quorum 형성, Kafka 전달/복제/리더 선출, UI 접속, 네트워크 분리·재연결의 실제 성공을 확인하지 못했습니다.

`tests/fake_podman.py`는 명령 전달/상태 변경을 검사하는 가짜 런타임이며 Kafka 프로토콜 또는 Podman 네트워크를 구현하지 않습니다. Python mock도 실제 Kafka 서버의 대체 구현이 아닙니다. 75개 테스트 통과를 9개 실서버 장애 시나리오 통과로 해석하면 안 됩니다.

ShellCheck, 부하 벤치마크, 장시간 soak test, 실제 물리 서버/전원/디스크 장애 및 보안 검증도 수행하지 않았습니다. 권장 자원 수치는 설계상 예산이지 측정 결과가 아닙니다.

## 사용자 호스트에서 실제로 실행할 검증

```bash
./lab.sh doctor
./lab.sh up
./scripts/test-live.sh
# 기본 검증 후, 다른 수동 작업이 없는 상태에서
./scripts/test-live.sh --faults
```

기본 실검증은 ZK 역할·broker/full ISR을 확인하고 전용 새 토픽에 120건을 기록한 뒤 event sequence/run ID를 읽어 누락·중복을 검사합니다. `--faults`는 추가로 9개 자동 데모와 복구를 실행합니다.

성공은 명령 exit code=0과 실제 실행의 `LIVE TEST PASS`/`DEMO PASS`로 판단합니다. 실패 출력과 시간 정보는 `reports/run-*/`에 남습니다. 원래 데이터/노드 장애 외의 원인으로 timeout이 나면 로그에서 그 원인을 확인해야 합니다. 자동 복구 실패를 감추지 않도록 non-zero 종료합니다.

## 패키지 범위

외부 이미지와 대규모 데이터는 포함하지 않습니다. named volume은 실행 시 생성합니다. `.env`는 최초 실행 시 `.env.example`로부터 복사합니다. `.state`/demo lock과 실제 실행 로그도 실행 시 생성됩니다. 테스트용 임시 파일은 자동 정리됩니다.
