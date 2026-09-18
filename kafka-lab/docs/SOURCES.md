# 공식 참고 자료

문서의 `[S1]` 같은 표기는 아래 자료를 가리킵니다. 확인일: 2026-09-18. 아래는 설정·동작 의미를 확인한 1차 자료이며 **이 lab의 실기동 테스트 결과를 대신하지 않습니다**. 버전 선택은 학습용으로 고정했으며 최신 패치 선택을 주장하지 않습니다.

## S1. Apache Kafka 4.0 Upgrading

`https://kafka.apache.org/40/getting-started/upgrade/`

ZooKeeper 모드 제거 및 KRaft 전환 조건. 이 프로젝트에 Kafka 4.x를 사용하지 않는 이유입니다.

## S2. Confluent Platform 7.9 — Supported Versions and Interoperability

`https://docs.confluent.io/platform/7.9/installation/versions-interoperability.html`

CP 7.9.x ↔ Kafka 3.9.x 매핑과 ZooKeeper 지원. 이 문서의 현재 7.9.x 구성 요소 표가 고정 이미지 7.9.0의 모든 정확한 patch 버전을 뜻하는 것은 아닙니다.

## S3. Confluent Docker Configuration Reference / ZooKeeper image template

`https://docs.confluent.io/platform/7.9/installation/docker/config-reference.html`

`https://raw.githubusercontent.com/confluentinc/kafka-images/7.9.x/zookeeper/include/etc/confluent/docker/zookeeper.properties.template`

`https://raw.githubusercontent.com/confluentinc/kafka-images/7.9.x/zookeeper/include/etc/confluent/docker/launch`

Kafka ZooKeeper 모드 환경 변수, 다중 listener 설정, ZK server ID와 ensemble. ZK 이미지의 템플릿은 지원하는 속성을 명시적으로 매핑하므로 임의의 `ZOOKEEPER_...` 환경 변수가 모두 자동 반영된다고 가정하지 않습니다. Four-letter-command 허용은 Java system property를 사용합니다.

## S4. Apache Kafka 3.9 — Design

`https://kafka.apache.org/39/design/design/`

파티션 로그, replication/ISR, 데이터 전달·처리 의미, consumer 및 메타데이터 제어 역할.

## S5. Apache Kafka 3.9 — Basic Kafka Operations

`https://kafka.apache.org/39/operations/basic-kafka-operations/`

기본 토픽 관리, consumer group, offset 조회/재설정, 파티션 운영.

## S6. Apache Kafka 3.9 — Topic-Level Configurations

`https://kafka.apache.org/39/configuration/topic-level-configs/`

`min.insync.replicas`, `max.message.bytes`, retention, segment 설정과 영향.

## S7. Confluent Kafka Python client

`https://docs.confluent.io/platform/current/clients/confluent-kafka-python/html/index.html`

`https://pypi.org/project/confluent-kafka/2.8.2/`

Producer delivery callback/flush, Consumer poll/store/commit, AdminClient와 metadata API. 설치 버전은 2.8.2로 고정했으며 current API 문서의 모든 새 기능을 사용하지 않습니다. 실제 image build/import는 제작 환경에서 실행하지 않았습니다.

## S8. Podman Compose 및 네트워크

`https://github.com/containers/podman-compose`

`https://raw.githubusercontent.com/containers/podman-compose/main/podman_compose.py`

`https://docs.podman.io/en/latest/markdown/podman-compose.1.html`

`https://docs.podman.io/en/latest/markdown/podman-network-connect.1.html`

`https://docs.podman.io/en/latest/markdown/podman-network-disconnect.1.html`

독립 podman-compose 실행 파일, `--in-pod=false`, provider 차이, 사용자 정의 네트워크와 서비스 DNS 별칭. 최신 문서/소스 확인은 사용자의 설치 버전에서 같은 동작이 검증되었다는 뜻이 아닙니다.

## S9. Confluent — Post-deployment Kafka Operations

`https://docs.confluent.io/platform/7.9/kafka/post-deployment.html`

controller, 종료·복구, 파티션 증가와 재배치, throttling 및 verify 작업.

## S10. Confluent — External Volumes

`https://docs.confluent.io/platform/7.9/installation/docker/operations/external-volumes.html`

Confluent 컨테이너의 데이터 경로와 비-root 사용자에 대한 볼륨 권한.

## S11. Kafbat Kafka UI

`https://github.com/kafbat/kafka-ui/releases/tag/v1.3.0`

선택적 UI의 고정 release. UI 접속과 read-only 설정 적용 여부는 실제 호스트에서 확인해야 합니다.

## S12. ZooKeeper / Kafka ZooKeeper Operations

`https://kafka.apache.org/39/operations/zookeeper/`

`https://kafka.apache.org/39/getting-started/quickstart/`

`https://zookeeper.apache.org/doc/r3.8.4/zookeeperAdmin.html`

ZooKeeper ensemble 관리, `srvr`/`ruok`의 차이, `zookeeper.4lw.commands.whitelist` Java system property. 이 문서 버전 표기는 lab 이미지 안의 정확한 ZK patch 버전 선언이 아닙니다.

## S13. Podman Run — 볼륨 권한/SELinux

`https://docs.podman.io/en/latest/markdown/podman-run.1.html`

`:U`의 컨테이너 UID/GID 기준 chown, `:z`/`:Z`와 SELinux, rootless 자원 제한. 이 lab은 chown 대상에 호스트 bind mount를 사용하지 않습니다.

---

모든 데이터와 이벤트 ID는 학습용 합성 데이터입니다. 문서의 시나리오, wrapper, 데이터 생성기와 자동 검사 로직은 이 프로젝트용으로 작성되었습니다. Kafka·ZooKeeper·Podman·Confluent·Kafbat의 공식 제품이나 공식 인증 실습으로 표방하지 않습니다. 외부 이미지/패키지는 ZIP에 재배포하지 않고 설정에서 참조합니다.
