# 05 · RDB 백업과 독립 복원

## 1. 백업 만들기

```bash
./lab.sh wait
./lab.sh backup
ls -lh output/backups
```

현재 Master를 조회하고 검증용 marker를 쓴 다음, `redis-cli --rdb`로 일관된 RDB stream을 받습니다.
출력에는 실제 만들어진 `output/backups/날짜-식별자.rdb`와 대응 JSON 경로가 안내됩니다.
JSON에는 SHA-256, 원본 노드, marker key/value, 시각이 들어갑니다. 비밀번호는 넣지 않습니다.

이 방식은 서버의 과거 dump.rdb를 복사하는 것과 다릅니다. 다만 업무 시스템의 외부 DB까지 묶은
애플리케이션 일관성 백업은 아닙니다. 백업 전후에 failover가 계속 발생하면 정상화한 후 다시 시도하세요.

## 2. sandbox에만 복원

`backup`이 출력한 실제 경로를 사용합니다.

```bash
./lab.sh restore output/backups/실제파일명.rdb --yes
```

이 명령은 **redis-sandbox의 기존 데이터만 삭제/대체**합니다. 주 구성의 Master와 Replica는 덮어쓰지 않습니다.
동작 순서는 sandbox 정지 → 기존 sandbox 데이터 정리 → AOF를 끈 상태로 RDB 로드 → marker 확인 →
AOF 다시 켜기 → AOF rewrite 완료 확인 → CONFIG REWRITE입니다.

AOF가 활성화된 상태에서 RDB만 복사해 놓고 재시작하면 원하는 RDB가 적용되지 않을 수 있으므로,
첫 로드와 AOF 재생성 순서를 명시적으로 분리합니다.
메타데이터가 있는 백업은 체크섬이 다르면 복원을 거부합니다. 신뢰하는 본인 실습 파일만 사용하세요.

```bash
./lab.sh cli redis-sandbox DBSIZE
./lab.sh cli redis-sandbox HGETALL lab:user:100
./lab.sh cli redis-sandbox TTL lab:session:100
./lab.sh cli redis-sandbox INFO persistence
```

RDB에 저장된 만료 시각은 복구 후에도 의미를 갖습니다. 시간이 많이 지난 세션 키는 이미 만료되어
없을 수 있습니다. 원본/복원본 DBSIZE가 항상 같아야 한다는 판정은 하지 않습니다.
marker 일치만으로 모든 키를 검증했다고 말하지 말고 업무 중요 키·값·TTL도 별도로 확인하세요.

## 3. 재시작 후 유지 여부

```bash
./lab.sh cli redis-sandbox SET lab:restored:new-write persisted
# 정상 종료 후 재시작: fault 명령은 HA 노드 전용이므로 sandbox에는 직접 제한된 명령 사용
podman stop rslab-redis-sandbox
./lab.sh start redis-sandbox
./lab.sh cli redis-sandbox GET lab:restored:new-write
```

LAB_NAME을 바꿨으면 rslab 접두어도 맞춰야 합니다.

## 하지 않는 작업

손상된 AOF를 자동 수정하거나 운영 Master의 데이터 파일을 교체하지 않습니다.
AOF 복구 도구의 `--fix`는 데이터가 잘려나갈 수 있으므로 원본을 보존한 복사본에서 별도 학습해야 합니다.
이 패키지는 그 위험 작업을 자동화하지 않습니다.

참고: [REFERENCES](../REFERENCES.md) R6/R12.

## 수정판 복원 사전 검사

1.1부터 RDB를 sandbox 임시 경로에 복사하고 `redis-check-rdb`가 성공한 뒤에만 sandbox를 중단하고 데이터를 교체합니다.
원본 cluster는 덮어쓰지 않습니다. 체크섬/metadata 오류 또는 RDB 구조 검사 실패는 복원 전 오류로 처리합니다.
이 검사 코드를 작성 환경에서 실제 Redis 바이너리로 실행한 것은 아닙니다. [TESTING](../TESTING.md)을 참조하세요.
