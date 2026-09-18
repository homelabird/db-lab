# Elasticsearch Lab Helm chart

이 Chart는 Elasticsearch 3노드 StatefulSet, Kibana, Cerebro를 한 번에
배포합니다. 기본값은 현재 Compose/Kubernetes 실습 환경과 동일한
Elasticsearch/Kibana `7.17.29`입니다.

```bash
helm lint ./helm/elasticsearch-lab
helm install es-lab ./helm/elasticsearch-lab --namespace elasticsearch-lab --create-namespace
helm status es-lab -n elasticsearch-lab
```

기본 3노드 외에 최대 100노드 manifest도 실행 없이 생성할 수 있습니다.

```bash
helm template es-lab ./helm/elasticsearch-lab \
  --set elasticsearch.replicas=100 > elasticsearch-100.yaml
```

Chart는 3개 미만 또는 100개 초과의 replica 값을 거부합니다. 이 명령은
클러스터에 연결하거나 컨테이너를 생성하지 않습니다.

UI 확인:

```bash
kubectl -n elasticsearch-lab port-forward svc/es-lab-elasticsearch-lab-kibana 5601:5601
kubectl -n elasticsearch-lab port-forward svc/es-lab-elasticsearch-lab-cerebro 9000:9000
```

버전 업그레이드는 먼저 snapshot과 호환성을 확인한 뒤 같은 7.17 계열에서
rolling update로 수행합니다.

```bash
helm upgrade es-lab ./helm/elasticsearch-lab \
  -n elasticsearch-lab \
  --set elasticsearch.image=docker.elastic.co/elasticsearch/elasticsearch:7.17.30
kubectl -n elasticsearch-lab rollout status statefulset/es-lab-elasticsearch-lab-elasticsearch
```

`7.x`에서 `8.x`로의 직접 이미지 교체는 이 Chart에서 보장하지 않습니다.
운영 업그레이드는 snapshot/restore 또는 공식 migration 절차를 별도 검증해야
합니다. `sysctlInitContainer.enabled=true`는 privileged 권한을 요구하므로
Pod Security 정책이 이를 허용하지 않는 클러스터에서는 노드 설정을 먼저
적용하고 해당 값을 `false`로 설정하십시오.
