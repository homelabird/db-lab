# DB Lab v9 — Ansible 제어 추가·검증 보고서

**작성일: 2026-09-19 · 입력: db-lab-simulation-v8.zip · 출력: db-lab-simulation-v9-ansible.zip**

## 1. 결과와 변경 경계

기존 `all.sh`를 호출하는 프로젝트 전용 Ansible 모듈, 로컬/SSH inventory, 제어 playbook, 공유 보고서 수집 playbook, 사용 예시를 추가했습니다. 기존 DB·MVP·Helm 제어기의 안전장치를 우회하는 직접 Docker/SQL 관리 로직은 추가하지 않았습니다.

**전체 호스트 테스트 1,493개가 통과했습니다. 실제 ansible-playbook·SSH·Docker 실행은 이번 환경에서 완료하지 못했습니다.** 실제 Ansible 검사 진입점은 ansible-playbook 부재로 종료127입니다. YAML 파싱이나 AnsibleModule double을 실제 playbook 성공으로 집계하지 않습니다.

사용자 서버·DB·원격 저장소에 적용하지 않았습니다. 첨부 프로젝트의 별도 수정 사본이며 원본 ZIP은 보존했습니다. 기존 `all.sh`, 앱 소스, Compose, DB별 실습, Helm, CI workflow는 바이트 단위로 변경하지 않았습니다. 기존 446개 중 README.md/.gitignore 두 개만 변경하고 신규21개를 추가해 최종467개 파일입니다. 정량 결과·실제 SHA-256은 별도 증거 묶음의 package-integrity.json을 기준으로 합니다.

기존 v8에서 실행 중인 앱의 소스 계약을 바꾸지 않으므로 **이 Ansible 계층만 추가하기 위해 앱 이미지를 새로 빌드할 필요는 없습니다.** 사용자가 `up`을 명시적으로 선택하면 기존 기동 명령의 빌드 동작은 그대로 수행됩니다. 원래 `.env`, `.state`, 볼륨, engine pin을 보존해야 합니다.

## 2. 구조와 지원 범위

```text
제어 PC의 ansible-playbook
    → local 또는 SSH
    → 대상 Linux 서버의 db_lab_control
    → 대상 서버의 기존 all.sh
    → 기존 네 DB 실습 / MVP / Helm
```

대상은 `mvp`, `all`, `elasticsearch`, `kafka`, `mariadb`, `redis`, `k8s`입니다. `all`은 기존 네 독립 실습이며 MVP/Helm을 포함하지 않습니다. MVP의 13 simulations/11 drills/6 messages와 verify/study를 명시적인 action/verb/name/options로 호출합니다. 범용 shell/exec나 알 수 없는 옵션을 그대로 전달하지 않습니다.

원격 제어는 SSH로 실습 서버에 접속해 **그 서버의 로컬 엔진**을 사용합니다. 기존 자동 실습의 원격 DOCKER_HOST 금지·소유권·엔진 pin 정책을 해제하지 않습니다. 로컬 Docker 실습을 여러 물리 서버의 분산 HA로 바꾸는 작업도 아닙니다.

각 대상에 전체 프로젝트와 Python/Bash/flock/런타임이 먼저 준비되어 있어야 합니다. OS/Docker 설치, 소스 동기화·덮어쓰기, SSH 키 배포, DB 암호 회전, firewall 변경은 하지 않습니다. 프로젝트를 운영하던 동일 사용자로 접속하고 become을 사용하지 않습니다.

## 3. 안전 및 실행 계약

| 구분 | 구현 |
|---|---|
| 일반 변경 | allow_changes를 명시해야 init/up/down/restart/smoke 등을 실행 |
| 장애 실습 | 추가 allow_faults 요구. native --yes도 기존 정책대로 전달 |
| 데이터 삭제 | allow_destroy와 정확한 DELETE:target 또는 UNINSTALL:namespace/release 필요 |
| 최초 엔진 bind | BIND:mvp 확인 문구 필요. 기존 pin 덮어쓰기는 하지 않음 |
| check mode | 입력·경로·계획만 검사. all.sh 및 실습 파일/DB 명령은 호출하지 않음 |
| 여러 서버 | 순차 실행, 첫 실패에서 중단. 변경 작업은 한 호스트로 limit해야 함 |
| 실행 방식 | shell=False의 argv, 명령당 1회. 자동 재시도/ignore_errors/무조건 recover 없음 |
| 실패 전달 | native rc2/127 등도 실패로 전달. status 조회 성공을 DB 건강 판정으로 바꾸지 않음 |
| 시간 제한 | 명령당 기본3600초, 30~43200초 허용. TERM/유예/KILL 뒤 timeout은 실패 |
| 출력 | private directory700/file600, 스트림당2MiB까지만 보관. 기본 Ansible 출력에 원문 숨김 |

체크 모드도 Ansible 접속·임시 모듈 전송은 발생할 수 있습니다. DB 준비 여부를 예측하는 모드가 아닙니다. 작업 정책의 동의값은 운영 실수 방지용이며 playbook·Docker를 직접 실행할 권한이 있는 사람을 격리하는 보안 경계는 아닙니다.

추가 flock은 같은 프로젝트 경로의 Ansible 호출끼리 중복 실행을 막습니다. 기존 하위 잠금도 유지하지만 다른 사본·직접 Docker·다른 사용자의 전체 작업까지 전역 직렬화하지 않습니다. MVP restart는 down/up 두 단계이며 down 실패 시 up을 실행하지 않습니다.

init은 환경 파일의 전후 해시가 동일하면 changed=false입니다. 나머지 변경 작업은 명령 실행 사실을 changed=true로 보고합니다. 완전한 선언형 상태 수렴 모듈이 아닙니다. 관찰 명령도 native CLI의 receipt와 Ansible 진단 파일을 남길 수 있습니다.

SSH 단절/호스트 종료/강제 종료 이후 자동 회복을 보장하지 않습니다. 생성된 fault marker를 지우지 않고 원래 simulate/drills/messages recover로 확인해야 합니다.

## 4. 결과 수집

`collect.yml`은 지정한 accept-ID에 대해 기존 `mvp verify export`를 실행한 뒤 정확히 summary.json/report.md/junit.xml만 fetch합니다. 경로·symlink·크기 검사와 fetch checksum 확인을 사용합니다. `.env`, 원시 stdout/stderr, 전체 reports, 주문 원문, 복구 marker를 재귀 복사하지 않습니다. 원래 blocked/failed를 passed로 바꾸지 않습니다.

수집 결과는 제어 PC의 `ansible/artifacts/<inventory_alias>/<accept-ID>/`입니다. 어디에도 자동 업로드하지 않습니다. 원시 native 출력이 필요한 경우에만 show_output을 켤 수 있으며, 이 경우 자격 증명·합성 데이터가 노출될 수 있습니다. 범용 민감정보 스캐너를 구현한 것은 아닙니다.

## 5. 실제로 실행한 검사

| 묶음 | 테스트 수 | 결과 / 범위 |
|---|---:|---|
| root | 177 | 기존91 + 신규 Ansible 계층86, 모두 통과 |
| MVP | 763 | 기존 mock/HTTP/SQLite 어댑터 시험, 모두 통과 |
| Elasticsearch | 77 | 기존 호스트 검사 통과 |
| Kafka | 94 | 기존 호스트 검사 통과 |
| MariaDB | 198 | 기존 호스트 검사 통과 |
| Redis | 184 | 기존 호스트 검사 통과 |
| **합계** | **1,493** | **실패·skip 없음** |

신규86개는 허용 명령·기존 실제 argparse와의 호환, 동의/삭제/경로/옵션 검증, 안전한 subprocess와 출력 한도·권한·잠금·timeout, failed down 뒤 up 차단, env 보존, AnsibleModule double 경계, YAML 구조 검사입니다. AnsibleModule 경계는 명시적인 double이며 실제 Ansible 모듈 전송·templating·inventory 우선순위 증거가 아닙니다.

정적 검사: Python133개, Bash59개, 일반 YAML/Ansible/workflow35개 통과. Helm template11개는 일반 YAML에서 제외했습니다. MariaDB SHA256SUMS와 Redis MANIFEST.sha256도 통과했습니다. 앱 JavaScript는 변경하지 않았습니다. 이번에 브라우저/JS 검사를 다시 실행한 것으로 주장하지 않습니다.

첫 정적 분류에서 Jinja 문자열이 있는 기존 GitHub workflow를 template로 제외했습니다. Ansible/workflow의 인용된 표현은 YAML로 검사하도록 범위를 바로잡아 최종35개를 확인했습니다. initial/final 기록을 보존합니다.

### 실제 adapter → all.sh 호출

별도 임시 v8 사본에서 새 정책/실행기를 통해 기존 all.sh를 실제 호출했습니다. 이 경로에는 가짜 DB나 가짜 컨트롤러가 없습니다. 단, Ansible 전송 계층은 통과하지 않습니다.

| 호출 | 관측 |
|---|---|
| mvp init 최초 | rc0, 환경 파일 생성, changed=true |
| mvp init 반복 | rc0, 환경 파일 내용 동일, changed=false |
| simulate plan kafka-outage | rc0, 원래 계획 생성 |
| mvp doctor | rc1, 실제 런타임 없음 |
| verify run core --yes | rc127/blocked, adapter도 failed=true |
| 위 실행 verify export | rc0, 공유 결과는 blocked 유지, 정확히 세 파일 |

이 여섯 CLI 호출과 최종 ZIP 재검사는 1,493개 unittest 수에 중복 합산하지 않습니다. 이전 보고서의 독립19개 재검사는 이번에 재실행하지 않았고 이번 통과 수에 포함하지 않습니다.

### 실제 Ansible 인수 경로

`ansible/tests/test_playbooks_live.py`에 실제 ansible-playbook을 사용하는 로컬 fixture 검사7개를 별도로 제공했습니다. 두 playbook의 syntax-check, 모듈 패키징/실행, check 무실행, 동의 거부, rc7 실패 전달, init 반복, 다중 호스트 변경 거부를 검사합니다.

이번 환경에는 ansible-playbook이 없어 `scripts/test-ansible.sh`가 **종료127/blocked, 실제 Ansible 검사0개 실행**입니다. 이 7개를 통과 수에 넣지 않았습니다. actual ansible-lint, 실제 local playbook, SSH 원격, collect의 실제 fetch, sudo/권한/SSH-agent 환경, 실 DB/HA/복구도 미검증입니다.

호스트 Python3.13.5에 -S와 설치 site-packages, PATH shim을 사용해 환경의 Python startup hook 영향을 분리했습니다. Docker/Podman/Helm/kubectl도 없습니다. PyPI 접속은 curl6/DNS 오류였습니다. 모의 성공을 실기동 성공으로 바꾸는 fallback은 추가하지 않았습니다.

## 6. 적용·인수 순서

제어 PC의 Python 가상환경에 requirements를 설치하고 ansible/ 디렉터리에서 실행합니다. 기본 로컬 inventory는 상위 프로젝트 루트를 사용합니다. 원격은 remote.example.yml을 복사해 실제 전용 서버의 주소·사용자·절대 프로젝트 경로로 바꾸고 --limit로 한 서버를 지정합니다.

```bash
# 프로젝트 루트
python3 -m venv .venv-ansible
. .venv-ansible/bin/activate
python -m pip install -r ansible/requirements.txt
bash scripts/test-ansible.sh
cd ansible
ansible-playbook control.yml --check -e @examples/mvp-up.yml
ansible-playbook control.yml -e @examples/mvp-up.yml
ansible-playbook control.yml -e @examples/verify-core.yml
```

최초 환경에서는 up 전에 init/doctor를 실행합니다. 전체 명령과 동의/수집/복구 예시는 ansible/README.md를 따릅니다. 이미 준비된 v8 환경에서는 env/state를 새로 생성하거나 pin을 삭제하지 않습니다.

다음 완료 조건은 전용 서버에서 실제 Ansible → all.sh → 로컬 DB의 core 인수를 통과하고, 수집된 보고서가 해당 실행과 일치하는지 확인하는 것입니다. 이번 변경이 기존 DB 인수 검증 공백을 해소한 결과라고 주장하지 않습니다.

## 7. 증거와 출처

별도 증거 ZIP의 full/summary.json과 suite 로그, native-chain.json, real-ansible-attempt.json, static-results.json, environment.json, source-diff.json, package-integrity.json이 이번 실행 근거입니다. 원본 v8 ZIP 전체 목록을 기준으로 비교하며 기존 보고서/ignored 파일도 누락시키지 않습니다.

공식 Ansible check-mode, 모듈 개발, fetch, 오류 처리, Python 지원 범위 및 ansible-core2.21.4 PyPI 문서는 ansible/README.md의 [S1]~[S6]에서 확인할 수 있습니다. 공식 문서 확인일은 2026-09-19이며, 문서 조회를 실제 설치/실행 증거로 사용하지 않습니다.
