# 메모리 단편화 실험

```bash
./ops.sh run fragmentation --mib 64 --seconds 30 --yes
```

`--mib`는 할당 패턴의 규모를 정하는 예산이며 실제 RSS나 원시 값 크기를 정확히 지정하는 옵션이 아닙니다. 객체별 오버헤드를 고려해 키 수를 계산합니다. 값 크기는 384/400/448/480/512 byte를 순환하고, 생성 후 키의 75%를 흩어지게 삭제합니다. 결과는 Redis 빌드, jemalloc, 할당기 기존 상태에 영향을 받습니다.

## 진행 단계

1. 초기 메모리를 저장하고 자동 active defrag를 끕니다.
2. 규칙적인 다양한 크기의 값을 생성하고, 4개 중 1개만 남깁니다.
3. 살아남은 모든 키의 기대 값과 SHA-256 digest를 확인합니다.
4. 무조치 상태를 관측한 다음 `MEMORY PURGE`를 실행합니다.
5. 실습용 낮은 임계값으로 active defrag를 켜고 정리 동작/낭비 감소를 관찰합니다.
6. 모든 생존 키를 다시 검증하고 설정과 실험 데이터를 정리합니다.

비교는 같은 인스턴스에서 순서대로 진행하므로 통제된 독립 A/B 시험이 아닙니다. 정리 경과와 자연적인 페이지 회수가 섞일 수 있습니다. 결과에는 각 단계의 원본 지표를 남깁니다.

## 핵심 지표

| 지표 | 확인할 질문 |
|---|---|
| used_memory | Redis가 사용 중인 메모리 자체가 늘었는가? |
| used_memory_rss | 프로세스의 상주 메모리는 얼마나 남아 있는가? |
| allocator_frag_ratio / bytes | 사용 중 할당기 페이지 내부의 공간 낭비가 큰가? |
| allocator_rss_ratio / bytes | 할당기가 보유한 페이지와 활성 페이지 사이 차이가 큰가? |
| active_defrag_running / hits | 정리가 실제로 실행됐는가? |
| used_cpu_user / sys, probe p99 | 정리 중 CPU와 요청 지연 비용은 어떠한가? |

`mem_fragmentation_ratio` 하나만으로 순수 단편화라고 결론 내리지 마세요. [INFO 공식 필드 설명](https://redis.io/docs/latest/commands/info/)

`MEMORY PURGE`는 반환 가능한 페이지의 회수를 요청하며, 살아 있는 객체를 옮기는 active defrag와는 다른 동작입니다. jemalloc이 아닌 경우 의미가 다르므로 이 시나리오는 BLOCKED 처리합니다. [MEMORY PURGE](https://redis.io/docs/latest/commands/memory-purge/)

Redis 7.4 기본 active-defrag-ignore-bytes는 100MB입니다. 작은 실습에서 정리가 시작되도록 이 실험은 **1MiB, lower threshold 1%, cycle 5~25%**를 일시적으로 적용합니다. 운영 권장값이 아닙니다. 지원되지 않는 빌드라면 BLOCKED이며 성공으로 처리하지 않습니다. [Redis 7.4 설정](https://raw.githubusercontent.com/redis/redis/7.4/redis.conf)

## 이 실험의 판정 조건

초기 대비 allocator_frag_bytes가 2MiB 이상 증가하고, 전체 낭비가 4MiB 이상이며 ratio가 1.15 이상이면 단편화를 관측한 것으로 분류합니다. 이후 실제 정리 동작과 낭비 20% 이상 감소, 생존 데이터 일치를 모두 확인해야 PASS입니다. 이 수치는 **실습 판정용**이지 운영 장애 임계값이 아닙니다.

단편화가 충분하지 않거나 관찰 시간에 정리가 충분하지 않으면 NOT_REPRODUCED입니다. 무조건 데이터를 더 늘리지 말고 현재 사용량과 빌드 지원을 확인하세요. `--mib` 상한 96, `--seconds` 상한 60이며 메모리 안전 기준도 적용됩니다.

BGSAVE/BGREWRITEAOF는 단편화 정리 명령이 아닙니다. persistence 실험에서 별도로 다룹니다.
