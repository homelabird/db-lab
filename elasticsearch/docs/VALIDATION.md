# 최신 장애 기능 검증 추가

장애 도구 추가 후의 최종 검사 결과는 [FAULT-VALIDATION.md](FAULT-VALIDATION.md)와 `reports/validation/offline-tests.txt`를 기준으로 합니다. 아래 내용은 이전 시드·바인딩 작업의 이력이며, 그 당시 테스트 수를 현재 전체 테스트 수로 읽지 마세요. 실제 ES 5노드/Podman/Cerebro UI 검증은 이번 작업에서도 미실행입니다.

---

# 검증 결과와 범위

## 기본 바인딩 변경 재검증

이번 수정에서는 `.env.example`과 `compose.yaml`의 공개 포트 기본 바인딩을 모두 `0.0.0.0`으로 변경하고, 기동 메시지와 접속 문서를 갱신했습니다. 시드 생성·적재 로직, 인덱스 매핑, 검색 쿼리, 데이터 볼륨 이름은 변경하지 않았습니다.

기존 21개 테스트와 네트워크 기본값 회귀 테스트 4개를 합친 **25개 테스트를 다시 실행해 모두 통과**했습니다. Bash 문법, Python 문법, Compose YAML 파싱, 기본값·loopback override·변경 포트의 문자열 치환 정적 검사도 통과했습니다. Compose provider를 실제로 실행한 검사는 아닙니다.

**Podman/Docker 실행 파일이 없어 실제 포트 공개와 다른 PC에서의 접속은 검증하지 못했습니다.** 방화벽·보안그룹·라우팅 확인은 사용자 환경에서 필요합니다. 아래 100MiB 전체 생성 수치는 이전 시드 버전에서 기록한 결과이며, 이번 바인딩 수정에서는 대용량 전체 생성을 다시 실행하지 않았습니다.

## 실제로 수행한 검사

| 검사 | 결과 | 범위 |
|---|---|---|
| Python 단위·회귀 테스트 | **25개 PASS** | `python3 -m unittest discover -s tests -v` |
| 실제 loopback HTTP를 통한 모의 Bulk 적재 | PASS | 로컬 HTTP stub에 생성→인덱스 준비→배치→refresh→count 요청 실행 |
| 반복 적재 | PASS | 같은 설정 재실행 시 동일 ID/본문 유지, 문서 수 중복 증가 없음 |
| 부분 오류/HTTP 오류 | PASS | 429 일부 항목만 재시도, HTTP 503 재시도, 400은 실패 처리 |
| 삭제 안전장치 | PASS | 명시적 yes 필요, 다른 시드 설정이면 쓰기 전 거부, 정확한 3개 이름만 삭제 |
| 대상 클러스터 확인 | PASS | 다른 cluster.name에 쓰기 요청 없음 |
| 실패 시 refresh 복원 | PASS | Bulk 오류 주입 후 원래 refresh_interval 복원 |
| PIT 페이지 처리 | PASS | 최신 PIT ID·전체 sort 커서 유지, 정상/오류 시 PIT 닫기 |
| 전체 기본 데이터 생성 | **142,640건 / 104,858,904 bytes** | 실제 100MiB 생성 모드를 실행하고 생성된 NDJSON 전체를 다시 읽어 검사 |
| NDJSON 전수 검사 | PASS | action/source 짝, JSON 파싱, ID 순서, 매핑 필드명, 건수·바이트 합계 |
| Bash 스크립트 문법 | PASS | 모든 `.sh`에 `bash -n` |
| Python 문법 | PASS | Python 컴파일 검사 |
| Compose / 매핑 / 예제 파일 | PASS | YAML/JSON 파싱 및 5노드·필드·인덱스 참조 정적 검사 |

## 기본 100MiB 생성 결과

Python 실행 환경에서 기본 seed=42, 시작일=2026-08-01 UTC, 31일, payload=256 bytes로 실행했습니다. 전송용 action 줄을 제외한 source JSON+LF 기준입니다.

| 인덱스 | 문서 수 | source bytes | 생성된 주요 이상 사례 |
|---|---:|---:|---|
| lab-transactions-v1 | 66,796 | 52,429,469 | high-risk-payment 3,340건 |
| lab-web-logs-v1 | 47,956 | 33,554,984 | payment-timeout 2,400건 |
| lab-audit-v1 | 27,888 | 18,874,451 | failed-login 1,395건, privileged-change 1,395건 |
| **합계** | **142,640** | **104,858,904** | — |

검사에 사용한 대용량 NDJSON은 최종 ZIP에 넣지 않았습니다. 사용자가 스크립트로 생성하게 되어 있습니다. 설정이나 실행 환경이 달라지면 실제 manifest를 기준으로 확인하세요.

## 수행하지 못한 검사

이 작업 환경에는 Podman/Docker 실행 파일이 없었고, Elasticsearch 배포 파일 다운로드도 외부 DNS 실패로 진행할 수 없었습니다. 따라서 아래 항목은 **미실행**입니다.

- 실제 Elasticsearch 7.17.29 5개 프로세스/컨테이너의 클러스터 형성
- 실제 Lucene 인덱싱과 저장 용량, 샤드 분산·이동·복구
- 실제 Elasticsearch에서 22개 쿼리와 PIT 검색 실행
- Cerebro 이미지 기동·브라우저 화면 조작·rootless Podman/SELinux 동작
- Compose provider가 설정을 해석하고 컨테이너를 생성하는 과정

**로컬 HTTP stub 테스트는 실제 Elasticsearch 통합 테스트가 아닙니다.** HTTP 요청 형식, 분할, 오류 처리, 안전장치를 검사했지만 Query DSL의 실제 실행이나 Lucene 동작을 검증하지는 않습니다. 원문 생성 크기와 실제 Primary store 크기를 혼동하지 마세요.

## 사용자 환경에서 이어 실행할 실제 검사

```bash
./lab.sh compose config
./lab.sh up
./lab.sh seed
./lab.sh verify
./lab.sh pit --pages 3 --page-size 10
./lab.sh size
```

`lab.sh verify`는 실제 클러스터에서 5개 이상 data node, 인덱스별 정확한 문서 수와 시드 signature, 38 primary + 46 replica의 STARTED 배치, 22개 쿼리 응답과 주요 모의 사고 검색 결과를 검사합니다. 이 실행이 끝나야 `reports/live-verification.json`에 실제 환경의 PASS/FAIL이 기록됩니다. PIT 검색은 별도 명령으로 수행합니다.

정적 검사만 다시 실행하려면:

```bash
./scripts/12-offline-tests.sh
```

이 명령에는 ES가 필요 없고 Python의 표준 라이브러리만 사용합니다. HTTP 테스트는 loopback 임시 포트를 열기 때문에 로컬 소켓 생성 권한은 필요합니다.
