# Kubernetes 실습 플랫폼

이 랩은 Docker Compose와 별도로 Kubernetes에서 Elasticsearch 3노드,
Kibana, Cerebro를 배포할 수 있습니다. 리소스는 `k8s/`에 있으며
기본 namespace는 `elasticsearch-lab`입니다.

## Helm Chart

재사용 가능한 Chart는 `helm/elasticsearch-lab`에 있습니다. 기본값은
Elasticsearch/Kibana `7.17.29`, 3개 StatefulSet pod, pod별 20Gi PVC입니다.

```bash
./lab.sh helm lint helm/elasticsearch-lab
./lab.sh helm install es-lab ./helm/elasticsearch-lab \
  --namespace elasticsearch-lab --create-namespace
./lab.sh helm status es-lab --namespace elasticsearch-lab
```

값을 별도 파일로 관리할 수도 있습니다.

```bash
./lab.sh helm upgrade --install es-lab ./helm/elasticsearch-lab \
  -n elasticsearch-lab --create-namespace \
  --set elasticsearch.storageClassName=fast \
  --set elasticsearch.storageSize=50Gi
```

Helm으로 생성되는 UI 서비스도 ClusterIP입니다.

```bash
kubectl -n elasticsearch-lab port-forward \
  svc/es-lab-elasticsearch-lab-kibana 5601:5601
kubectl -n elasticsearch-lab port-forward \
  svc/es-lab-elasticsearch-lab-cerebro 9000:9000
```

## 사전 조건

- `kubectl`이 설치되어 있고 현재 context가 실습용 Kubernetes 클러스터를 가리켜야 합니다.
- 기본 StorageClass가 있어야 합니다. 각 Elasticsearch pod에 20Gi PVC를 만듭니다.
- 각 노드에서 `vm.max_map_count >= 262144`가 필요합니다. 매니페스트의 privileged
  initContainer가 설정을 시도하지만, 관리형 클러스터가 privileged pod를 거부하면
  클러스터 정책에 맞는 노드 sysctl 설정을 먼저 적용해야 합니다.
- 기본 요청량은 Elasticsearch 3개에 각각 1Gi 메모리, Kibana 512Mi,
  Cerebro 256Mi입니다.

  노드 수는 Helm 렌더링 단계에서 3~100개까지 생성할 수 있습니다. 이 명령은
  Kubernetes에 접속하지 않고 YAML만 stdout에 생성하므로, 100노드 구성 검토나
  GitOps manifest 생성에 사용할 수 있습니다.

  ```bash
  ./lab.sh k8s render --nodes 100 > /tmp/elasticsearch-100.yaml
  ```

  기본값은 3노드이며, 실제 100노드 배포를 실행하지 않습니다. 100노드는 PVC,
  메모리, JVM heap, master quorum, 네트워크 및 클러스터 운영 비용을 충분히
  확인한 뒤 별도 환경에서 적용해야 합니다.

## 배포 및 확인

```bash
./lab.sh k8s doctor
./lab.sh k8s apply
./lab.sh k8s status
./lab.sh k8s verify
```

Kibana와 Cerebro는 ClusterIP 서비스로 배포됩니다. 로컬에서 UI를 보려면:

```bash
kubectl -n elasticsearch-lab port-forward svc/kibana 5601:5601
kubectl -n elasticsearch-lab port-forward svc/cerebro 9000:9000
```

그 다음 `http://127.0.0.1:5601` 또는 `http://127.0.0.1:9000`을 엽니다.

## 버전 업그레이드 시나리오

```bash
./lab.sh k8s upgrade docker.elastic.co/elasticsearch/elasticsearch:7.17.29
# 또는
./lab.sh scenario 15-kubernetes-rolling-upgrade.sh \
  docker.elastic.co/elasticsearch/elasticsearch:7.17.29
```

이 시나리오는 StatefulSet의 rolling update를 수행하고 각 pod가 재기동한 뒤
클러스터 health와 3노드 구성을 확인합니다. 현재는 데이터 디렉터리 호환성과
Elasticsearch 7.17의 rolling upgrade 규칙을 보수적으로 적용해 **7.17.x
내에서만** 허용합니다. 7.x에서 8.x로의 직접 이미지 교체는 실행하지 않습니다.
8.x 업그레이드는 snapshot/복구 또는 공식 중간 버전·호환성 절차를 포함한 별도
마이그레이션 실습으로 만들어야 합니다.

운영 업그레이드 전에는 반드시 snapshot, restore 테스트, `kubectl rollout
history`, PDB, 노드별 디스크 여유, 플러그인 호환성을 확인하십시오.

Helm 업그레이드 예:

```bash
./lab.sh helm upgrade es-lab ./helm/elasticsearch-lab \
  -n elasticsearch-lab \
  --set elasticsearch.image=docker.elastic.co/elasticsearch/elasticsearch:7.17.30 \
  --set kibana.image=docker.elastic.co/kibana/kibana:7.17.30
kubectl -n elasticsearch-lab rollout status \
  statefulset/es-lab-elasticsearch-lab-elasticsearch
```

삭제:

```bash
./lab.sh k8s delete
```

`delete`는 namespace와 PVC를 함께 삭제하므로 Kubernetes 데이터가 제거됩니다.
