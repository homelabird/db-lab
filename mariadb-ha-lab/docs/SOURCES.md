# 공식 참고 문서

검토 기준일: 2026-09-17. 온라인 문서는 변경될 수 있습니다. 프로젝트의 개념 설명과 구성 선택을 위한 참고이며, 본 랩의 실제 컨테이너 실행 성공을 입증하는 자료는 아닙니다.

- MySQL InnoDB Cluster: https://dev.mysql.com/doc/mysql-shell/8.4/en/mysql-innodb-cluster.html
  - MySQL Group Replication, MySQL Shell AdminAPI, Router의 역할 구분. 문서가 현재 Shell 버전으로 리다이렉트될 수 있음.
- MariaDB Galera 시작/재시작: https://mariadb.com/docs/galera-cluster/galera-management/installation-and-deployment/getting-started-with-mariadb-galera-cluster
- Galera 필수 설정: https://mariadb.com/docs/galera-cluster/galera-management/configuration/configuring-mariadb-galera-cluster
- mariadb-backup SST, socat, 계정 권한, TLS 구분: https://mariadb.com/docs/galera-cluster/high-availability/state-snapshot-transfers-ssts-in-galera-cluster/mariadb-backup-sst-method
- Quorum와 safe_to_bootstrap: https://mariadb.com/docs/galera-cluster/high-availability/understanding-quorum-monitoring-and-recovery
- Quorum reset/최신 상태 노드 확인: https://mariadb.com/docs/galera-cluster/high-availability/resetting-the-quorum-cluster-bootstrap
- InnoDB/PK/큰 트랜잭션/가시성/auto increment 등 제한: https://mariadb.com/docs/galera-cluster/reference/mariadb-galera-cluster-known-limitations
- HAProxy HTTP health check와 별도 검사 포트: https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/health-checks/
- MariaDB 공식 이미지 Dockerfile: https://raw.githubusercontent.com/MariaDB/mariadb-docker/master/11.8/Dockerfile
- MariaDB 공식 이미지 entrypoint: https://raw.githubusercontent.com/MariaDB/mariadb-docker/master/11.8/docker-entrypoint.sh
- 일반 비동기 복제(비교 학습): https://mariadb.com/docs/server/ha-and-performance/standard-replication/setting-up-replication
- MariaDB GTID(비교 학습): https://mariadb.com/docs/server/ha-and-performance/standard-replication/gtid

공식 예제를 그대로 운영에 복사하지 말고 실제 실행 이미지 버전·환경·보안 요구에 맞춰 확인하세요. 신규 async replica, MaxScale, Prometheus/Grafana, MySQL InnoDB Cluster는 이 패키지에서 자동 설치하는 구성에 포함하지 않았습니다.
