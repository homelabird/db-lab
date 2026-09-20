# db-lab Helm chart 0.2.0 — 격리된 실습용 / 실기동 검증 대기

이 chart는 Compose 실습의 모든 복구 기능을 이식한 제품이 아닙니다. 템플릿·보호 로직을 보강했지만 이번 수정 환경에서는 **실제 Helm, Kubernetes, DB 이미지를 실행하지 못했습니다.** 사용자 데이터나 운영 자격 증명을 넣지 마세요. `../docs/REMEDIATION-REPORT.md`와 `../docs/RUNTIME-ACCEPTANCE.md`가 현재 상태의 기준입니다.

## 요구사항과 기본 배포

Helm 3.14 이상의 `--reset-then-reuse-values` 지원, kubectl, Python 3/PyYAML, 동작하는 StorageClass, NetworkPolicy를 지원하는 CNI가 필요합니다. 기본은 **Redis 3 + Sentinel 3**이고, 다른 DB는 명시적으로 활성화합니다. resources는 설계값이지 측정된 최소 사양이 아닙니다. 분산 프로필은 서로 다른 worker 3개를 요구합니다.

프로젝트 루트에서 실행합니다. 다음 명령은 실제 클러스터를 변경하므로 폐기 가능한 전용 환경에서만 실행하세요.

```bash
export DB_LAB_CONTEXT=kind-db-lab
export DB_LAB_NAMESPACE=db-lab
export DB_LAB_RELEASE=db-lab
# 다른 실습 context를 허용하려면 이름을 정확히 지정합니다. 운영 context를 넣지 마세요.
export DB_LAB_ALLOWED_CONTEXTS=kind-db-lab,k3d-db-lab,minikube

./all.sh k8s init        # namespace/Secret 생성; 기존 Secret 검증 후 보존
./all.sh k8s preflight   # 읽기 전용 대상/Secret/PVC/StorageClass 검사
./all.sh k8s up
./all.sh k8s status
```

`init`이 만드는 Secret은 `db-lab-credentials`입니다. 키는 `redis-password`, `mariadb-root-password`, `mariadb-sst-password`이며 24~128자의 URL-safe 문자열을 요구합니다. 값은 인수·출력·리포트에 기록하지 않고 kubectl 표준 입력으로 전달합니다. 별도 Secret을 만들면 `DB_LAB_SECRET`과 각 component의 `existingSecret`을 함께 지정하세요. Secret은 DB에 저장된 암호와 별개이며 **자동 회전 기능은 없습니다.** Redis/Sentinel은 기존 볼륨의 암호 fingerprint가 다르면 기동을 거부합니다. MariaDB 암호 회전은 DB 내부 계정과 Secret을 수동으로 일치시켜야 합니다.

`k8s up/preflight`의 추가 옵션은 `-f/--values/--set/--set-string/--set-json/--set-file`로 제한합니다. context/namespace/kubeconfig는 `DB_LAB_*` 환경 변수로만 지정합니다. force 교체·검증 생략·post-renderer·wait 해제는 전달하지 않습니다. 기존 release의 사용자 values를 읽고 새 차트 기본값 및 새 overrides에 합쳐 사전 렌더링하며, 실제 적용은 같은 reset-then-reuse 계약을 사용합니다. 구성 요소 비활성화 등 **명시한 변경은 기존 워크로드를 없앨 수 있으므로** 변경 전 렌더링을 확인하세요.

## 추가 프로필과 최초 bootstrap

```bash
# Kafka + ZooKeeper만 추가로 지정하는 독립 프로필
./all.sh k8s up -f ./helmchart/profiles/kafka-only.yaml

# 새 PVC로 처음 설치하는 MariaDB 전용 프로필
./all.sh k8s up -f ./helmchart/profiles/mariadb-only.yaml \
  --set mariadb.bootstrapNewCluster=true

# 모든 DB: legacy ES를 명시적으로 허용하는 격리된 실습
./all.sh k8s up -f ./helmchart/profiles/full.yaml \
  --set elasticsearch.bootstrapNewCluster=true \
  --set mariadb.bootstrapNewCluster=true

# 여러 worker에 같은 component의 Pod를 분산 (3개 worker 필요)
./all.sh k8s up -f ./helmchart/profiles/full.yaml \
  -f ./helmchart/profiles/distributed.yaml \
  --set elasticsearch.bootstrapNewCluster=true \
  --set mariadb.bootstrapNewCluster=true
```

위 예시는 **각각 새 전용 release에서 선택해서 사용하는 프로필**입니다. 같은 release에서 순서대로 모두 실행하는 튜토리얼이 아닙니다. 최초 bootstrap 플래그가 켜진 component의 PVC가 이미 하나라도 있으면 root preflight가 거부합니다. 설치가 준비 상태에 도달하면 root가 두 번째 Helm upgrade로 bootstrap/recovery 플래그를 꺼 둡니다. 이 단계까지 성공하기 전에는 데이터를 넣지 마세요. 설치 실패 후 기존 PVC가 남았다면 같은 fresh-bootstrap 명령을 반복하지 말고 상태를 조사합니다. 자동 PVC 삭제·강제 bootstrap·실패 자원 강제 롤백은 수행하지 않습니다.

직접 `helm`으로 실행하면 이 보호·봉인 단계가 없습니다. 직접 사용자는 Secret, 기존 PVC, context, 플래그 제거를 직접 검증해야 합니다. ES 7.17은 legacy 교육 목적으로만 유지하며, 최신 버전으로의 자동 업그레이드는 하지 않습니다.

## Redis 접속과 장애 전환

Redis Service는 **peer 발견용 headless Service**이고 쓰기 primary를 골라 주는 프록시가 아닙니다. 일반 클라이언트는 Sentinel 지원 드라이버로 세 Sentinel endpoint, master name `mymaster`, Redis/Sentinel 인증을 모두 설정해야 합니다. `helm template` 또는 `kubectl --context ... -n ... get svc`로 실제 이름을 확인하세요.

Sentinel hostname 해석을 활성화하고 설정을 PVC에 보존합니다. Redis는 세 Sentinel 중 두 개 이상이 합의한 primary를 확인한 뒤 primary/replica 역할과 복제 인증을 설정합니다. Sentinel 합의가 없으면 임의의 redis-0을 primary로 만들지 않습니다. AOF는 `everysec`, 최소 복제본 쓰기 조건은 1개로 설정했지만 **데이터 무손실이나 0 RPO를 보장하지 않습니다.** Redis/Sentinel PVC 손실, 전체 클러스터 동시 중단, 비대칭 네트워크 단절은 별도의 인수 시험 대상입니다. PVC 삭제는 일반 복구 방법이 아닙니다.

NetworkPolicy 사용 시 같은 namespace의 클라이언트 Pod에 `db-lab/client=<release>` label이 필요합니다. 같은 release의 DB 간 통신 및 DNS는 허용합니다. CNI가 정책을 시행하는지는 실제 접속 시험으로 확인해야 합니다. TLS·운영용 ACL/RBAC·완전한 tenant 보안 경계는 제공하지 않습니다.

## MariaDB 전체 중단 후 복구

영속 볼륨은 `/bitnami/mariadb`, 데이터 디렉터리는 이미지 계약상 `/bitnami/mariadb/data`입니다. 실제 선택한 이미지에서 `SELECT @@datadir`와 mount를 먼저 확인하세요. 각 Pod의 주소와 peer Service를 구분하고, 최초 빈 볼륨의 ordinal 0만 명시적인 fresh-bootstrap을 수행합니다.

전체 중단 후에는 **모든 노드의 UUID/seqno 및 recover 결과를 비교하고 최신의 안전한 후보를 결정**해야 합니다. 이 자동 판정은 구현하지 않았습니다. 사전 검토 후에만 `mariadb.recovery.bootstrapOrdinal=<0|1|2>`와 `mariadb.recovery.confirmed=true`를 사용합니다. 대상 데이터가 존재하고 해당 파일의 `safe_to_bootstrap: 1` 조건이 있어야 하며, 스크립트는 값을 강제로 변경하지 않습니다. 노드 번호만으로 최신 데이터를 판단하지 마세요. 다른 노드의 살아 있는 Primary와 충돌하는 독립 cluster를 만들지 않도록 먼저 모두 확인해야 합니다.

Bitnami 이미지 태그의 실제 pull 가능성, UID/권한, SST 성공 여부는 아직 검증하지 못했습니다. 레지스트리 경로를 무작정 바꾸거나 운영 볼륨을 다른 이미지에 바로 연결하지 마세요.

## 0.1.x → 0.2.0 마이그레이션

Service/StatefulSet selectors, Sentinel 리소스 이름, PVC 사용 방식, 기본 활성 component가 변경되었습니다. **기존 release에 강제 덮어쓰기 업그레이드를 하지 마세요.** 별도 namespace/release에서 새 차트를 검증하고, 기존 데이터는 DB 수준 백업 후 복원하는 방법을 사용하세요. root preflight는 확인 가능한 옛 chart 버전 또는 immutable selector 변경을 거부하지만, 모든 수동 변경 이력이나 스토리지 위험을 검출하는 마이그레이션 도구는 아닙니다.

```bash
./all.sh k8s down --yes   # release 제거. Secret/PVC 자동 삭제·암호 회전은 하지 않음.
```

PVC/PV reclaim policy, 백업, Secret을 별도로 관리하세요. 제거를 데이터 복구나 안전한 비우기 절차로 오해하지 마세요.
