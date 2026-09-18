# db-lab

네 개의 데이터베이스 실습 프로젝트를 Kubernetes에서 하나의 Helm release로
관리하려면 `helmchart`를 사용합니다. 이 chart는 Elasticsearch, Kafka,
ZooKeeper, MariaDB, Redis의 StatefulSet과 Service를 함께 생성합니다.

```bash
helm lint ./helmchart
helm template db-lab ./helmchart
helm install db-lab ./helmchart --namespace db-lab --create-namespace
kubectl -n db-lab get statefulsets,pods,svc
```

구성 요소별로 끄거나 이미지·복제 수·스토리지를 바꿀 수 있습니다.

```bash
helm upgrade --install db-lab ./helmchart \
  --namespace db-lab --create-namespace \
  --set kafka.enabled=false \
  --set mariadb.replicas=3 \
  --set redis.storage=10Gi
```

기존 Compose 기반 `lab.sh`는 장애 주입, 데이터 생성, 실습 시나리오 실행을
위한 별도 도구로 계속 유지됩니다. 기본 MariaDB 이미지는 Galera 이미지이며,
`rootPassword`와 `sstPassword`는 실사용 환경에서 반드시 Kubernetes Secret
또는 외부 Secret 관리자로 교체하십시오. 첫 배포 후 Galera bootstrap/SST와
스토리지 클래스를 대상 클러스터에서 검증해야 합니다.
