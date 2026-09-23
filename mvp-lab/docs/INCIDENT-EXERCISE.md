# 통합 운영 사고 훈련: 검색 결과가 늦게 갱신됨

목표: API 쓰기는 성공하는데 검색 화면에 새 주문이 나타나지 않는 사고를 운영자 관점에서 진단하고, 원본 데이터를 보존하며 복구한다. 이 훈련은 기존 메시지 드릴을 사고 대응 순서로 수행한다. 실제 고객 트래픽이나 운영 클러스터에는 사용하지 않는다.

## 사고 카드

- **사용자 신고:** 주문 접수는 성공했지만 검색 결과에는 보이지 않는다.
- **영향 범위 가정:** 신규/변경 주문의 검색 반영 지연. 주문 상세 원본 조회는 가능할 수 있다.
- **초기 가설:** API 쓰기 실패, outbox 적체, Kafka consumer 지연, Elasticsearch 색인 거부, 캐시 stale.
- **복구 목표:** 첫 실패 지점을 식별하고, 원본 주문을 유지한 채 검색을 수렴시킨다. 재전송으로 중복을 만들거나 처음부터 전체 재색인하지 않는다.
- **중단 조건:** 준비된 전용 로컬 MVP가 아니거나, 기존 메시지 드릴의 미정리 marker가 있거나, source/worker 이미지가 현재 소스와 맞지 않으면 훈련을 시작하지 않는다.

## 준비

```bash
bash ./all.sh mvp doctor
bash ./all.sh mvp smoke
bash ./all.sh mvp messages list
bash ./all.sh mvp messages plan poison-schema
```

계획에서 전용 토픽·그룹·인덱스 사용을 확인한다. 다음 명령은 합성 주문을 만들고 고의로 지원하지 않는 스키마 이벤트를 보내므로 승인된 전용 환경에서만 실행한다.

## 대응 진행

### 1. 영향 확인

훈련 진행자는 `messages run poison-schema --keep --yes`를 실행한다. 대응자는 아래 질문에 답하고, 다음 단계로 가기 전에 `events.json`/`report.md`의 실제 관측으로 답을 확인한다.

```bash
bash ./all.sh mvp messages run poison-schema --keep --yes
bash ./all.sh mvp diagnose
bash ./all.sh mvp sql outbox
bash ./all.sh mvp logs worker
```

- HTTP 쓰기 승인과 검색 가시성은 같은 성공 조건인가?
- 주문 원본은 저장됐는가? outbox는 전송 대기인가?
- 실패는 broker 연결, consumer 처리, 검색 색인 중 어디에서 처음 관측됐는가?
- poison 이벤트 뒤의 정상 이벤트는 처리됐는가, 대기 중인가?

이 실습은 전용 메시지 파이프라인에서 장애를 재현한다. 기본 웹 검색 파이프라인이 실제로 중단됐다고 주장하지 않는다. 앱 경로와 증거 범위를 구분해서 사고 기록에 적는다.

### 2. 완화와 복구

검사 결과를 바탕으로 전용 DLQ ACK 경계와 현재 SQL 원본을 확인한다. 이 훈련에서는 payload를 임의 수정하거나 source offset을 되감지 않는다. 지원되는 교정 절차는 실행별 SQL 원본을 읽어 새 이벤트를 만들고, ACK 뒤에만 source offset을 commit한다.

```bash
RUN='msg-실제 출력된 실행 ID'
bash ./all.sh mvp messages inspect "$RUN"
bash ./all.sh mvp messages cleanup "$RUN" --yes
```

`inspect` 결과에서 poison 격리, 후속 이벤트 처리, 원본에서 만든 교정, 최종 대조를 순서대로 확인한다. `cleanup`은 해당 실행의 전용 토픽/인덱스 정리다. 주문, 일반 outbox, 기본 토픽, 볼륨을 삭제하지 않는다. 실행 도중 실패해 active marker가 남으면 `messages recover --yes`로 해당 실습만 복구한 뒤 결과를 다시 확인한다.

### 3. 종료 판단

훈련은 다음 항목을 모두 증거로 확인한 뒤에만 종료한다.

- 원본 주문이 유지되고 실행 전후의 주문 ID가 같다.
- 실패 이벤트는 격리되었고 후속 정상 이벤트가 처리됐다.
- source offset은 DLQ 전송 ACK 이후에만 전진했다.
- 검색 문서는 원본의 현재 버전/내용과 일치한다.
- 실행 전용 자원이 정리됐거나, 복구가 필요한 marker와 정확한 복구 명령이 기록됐다.

## 회고 기록

```text
첫 사용자 증상 / 영향 시간:
첫 실패 구성요소와 근거:
request_id / event_id / topic-partition-offset:
outbox 및 consumer lag의 전후 변화:
완화 조치와 부작용:
복구 완료 근거와 남은 불확실성:
다음 액션(알림 임계값, payload 호환성, 재처리 권한 중 하나):
```

이 드릴은 짧은 합성 실행이다. 실시간 알림, 다중 호스트 failover, 처리량 용량, 실제 운영 RTO/RPO를 검증하지 않는다. 실서비스 대응 훈련으로 확장하기 전에는 각 조직의 실제 SLO·알림·승인 절차와 비식별화된 데이터로 별도 설계해야 한다.
