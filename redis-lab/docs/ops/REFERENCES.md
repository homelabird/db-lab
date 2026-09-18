# 공식 참고 자료

확인일: 2026-09-18. 코드의 목표 Redis 계열은 기존 패키지와 같은 7.4이며 기본 이미지 태그는 7.4.11-alpine입니다. 최신 버전이라는 의미가 아니라 학습 환경의 기준 버전입니다. 컨테이너 레지스트리 pull은 제작 환경에서 수행하지 못했습니다.

- Redis INFO: https://redis.io/docs/latest/commands/info/
- MEMORY PURGE: https://redis.io/docs/latest/commands/memory-purge/
- Redis 7.4 설정: https://raw.githubusercontent.com/redis/redis/7.4/redis.conf
- Redis 자체 메모리 테스트 참고: https://raw.githubusercontent.com/redis/redis/7.4/tests/unit/memefficiency.tcl
- SLOWLOG: https://redis.io/docs/latest/commands/slowlog-get/
- UNLINK: https://redis.io/docs/latest/commands/unlink/
- Eviction: https://redis.io/docs/latest/develop/reference/eviction/
- Client handling: https://redis.io/docs/latest/develop/reference/clients/
- Persistence: https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/
- Replication: https://redis.io/docs/latest/operate/oss_and_stack/management/replication/
- Sentinel: https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/
- XAUTOCLAIM: https://redis.io/docs/latest/commands/xautoclaim/
- Pub/Sub: https://redis.io/docs/latest/develop/pubsub/
- Distributed lock cautions: https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/
- Podman update: https://docs.podman.io/en/latest/markdown/podman-update.1.html
- Redis release archive: https://download.redis.io/releases/

프록시·부하발생기·보고서 생성기·단편화 패턴은 이 패키지의 교육용 구현입니다. Redis 자체 테스트를 실행한 것으로 취급하지 않습니다.
