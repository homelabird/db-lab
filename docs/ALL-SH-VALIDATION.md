# all.sh 변경 검증

검증일: 2026-09-18

## 변경 범위

루트 `all.sh`, 루트 회귀 테스트 `tests/test_all.py`, LF 유지용 `.gitattributes`,
루트 사용 설명과 이 검증 보고서를 추가했습니다. 원본 ZIP의 파일 302개를
바이트 단위로 비교했으며, 기존 파일 중 변경한 것은 루트 `README.md`뿐입니다.
하위 네 프로젝트와 `helmchart`의 기존 파일은 변경하지 않았습니다.

## 실행하여 통과한 검사

| 검사 | 결과 | 범위 |
| --- | --- | --- |
| `bash -n all.sh` | PASS | 새 Bash 스크립트 구문 |
| `./all.sh self-test` | PASS, 44 tests | 실제 Bash 실행 + 모의 프로젝트/Helm |
| 실제 `./all.sh init` | PASS, 4 projects | 별도 복사본의 환경 초기화 |
| 실제 `init` 재실행 | PASS | 기존 네 `.env` 내용 해시가 동일 |
| 새 `.env` 권한 | PASS | 네 파일 모두 `0600` |
| 실제 프로젝트별 `--help` | PASS, 4 projects | 업로드된 기존 진입점 호출 |
| `--dry-run` | PASS | 자식 미실행, 환경 파일 미생성 |
| 원본 파일 무결성 비교 | PASS | 기존 파일 중 README.md만 변경 |

회귀 테스트 원문은 [all-sh-tests.txt](all-sh-tests.txt)입니다.
실제 초기화 검사는 원본과 별개의 컨테이너 내 복사본에서 실행했습니다.
그때 생성한 `.env`, 무작위 비밀번호, 상태 디렉터리는 배포 ZIP에 포함하지 않습니다.

회귀 테스트는 프로젝트별 호출/작업 디렉터리, 공백·SQL 인자 보존, 별칭,
시작 전 초기화, 종료 순서, 초기화 실패/종료 실패 뒤 기동 차단,
실패 후 계속 실행과 fail-fast, 자식의 130/143 종료 코드 전파,
삭제 대상 및 확인 플래그, 파일 누락 시 선행 차단, 기존 환경 파일 보존,
심볼릭 링크 및 외부 디렉터리에서의 호출, Helm 인자·values 경로 전달 등을 검사합니다.

## 실행하지 않은 검사

이 검증 환경에는 Podman, Docker, Helm, kubectl, ShellCheck가 설치되어 있지 않습니다.
실제 DB 기동, 컨테이너 종료/볼륨 삭제, DB 복구, Kubernetes 배포/제거,
실제 Helm lint/template, ShellCheck 검사는 실행하지 않았습니다.
Helm 호출은 모의 실행파일에 전달된 인자만 검증했습니다.
기존 네 실습의 전체 테스트는 이번 변경에서 재실행하지 않았으며,
`./all.sh test`가 원래 호스트 테스트 진입점으로 연결되는지 모의 검사했습니다.
회귀 테스트 통과는 실제 DB 클러스터나 기존 Helm chart의 정상 동작을 의미하지 않습니다.

## 재현

```bash
bash -n all.sh
./all.sh self-test
./all.sh --dry-run up
./all.sh --dry-run reset all --yes
./all.sh --dry-run k8s up
```

실제 호스트에서는 각 실습의 요구사항을 준비한 뒤 `./all.sh init`,
`./all.sh doctor`, 필요한 프로젝트에 대한 `./all.sh up` 및 상태 확인을 수행하세요.
`up`은 전체 실습을 순차 시작하므로, 자원이 부족하면 프로젝트 이름으로 범위를 제한하세요.
