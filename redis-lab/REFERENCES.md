# 공식 참고 자료

프로젝트 구현/교재를 확인할 수 있는 1차 자료입니다. 별도 라이브러리/이미지는 해당 프로젝트 라이선스를 따릅니다.

- R1. Redis Sentinel 설정 원본(7.4): https://raw.githubusercontent.com/redis/redis/7.4/sentinel.conf
- R2. Redis 설정 원본(7.4): https://raw.githubusercontent.com/redis/redis/7.4/redis.conf
- R3. Redis 자료구조: https://redis.io/docs/latest/develop/data-types/
- R4. TTL: https://redis.io/docs/latest/commands/ttl/
- R5. SCAN: https://redis.io/docs/latest/commands/scan/
- R6. Persistence: https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/
- R7. WAIT: https://redis.io/docs/latest/commands/wait/
- R8. Sentinel client protocol: https://redis.io/docs/latest/develop/reference/sentinel-clients/
- R9. redis-py Sentinel 구현(v7.4.1): https://raw.githubusercontent.com/redis/redis-py/v7.4.1/redis/sentinel.py
- R10. Key eviction: https://redis.io/docs/latest/develop/reference/eviction/
- R11. SLOWLOG: https://redis.io/docs/latest/commands/slowlog/
- R12. redis-cli RDB backup: https://redis.io/docs/latest/develop/tools/cli/
- R13. Podman run / named volumes / SELinux: https://docs.podman.io/en/latest/markdown/podman-run.1.html
- R14. podman-compose 구현: https://raw.githubusercontent.com/containers/podman-compose/main/podman_compose.py
- R15. Redis 공식 이미지 메타데이터: https://raw.githubusercontent.com/docker-library/official-images/master/library/redis
- R16. Replication: https://redis.io/docs/latest/operate/oss_and_stack/management/replication/
- R17. Sentinel 운영 문서: https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/

이미지 태그는 R15에서 확인했고, Sentinel 인증/영속 설정은 R1, 역할/복제/저장 설정은 R2,
Sentinel-aware 클라이언트 연결은 R9를 참고했습니다. 공식 문서의 일반 설명은 실제 호스트에서
해당 실습이 성공했다는 증거를 대신하지 않습니다. 실행 검증 범위는 TESTING.md에 구분합니다.
