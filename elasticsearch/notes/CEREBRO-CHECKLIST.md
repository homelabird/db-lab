# Cerebro 화면에서 볼 것

1. **Overview**
   - cluster health: green / yellow / red
   - node 수, index 수, shard 수
   - 각 노드의 heap / disk / load

2. **Nodes / Shards**
   - `p`(primary)와 `r`(replica) 위치
   - `STARTED`, `RELOCATING`, `INITIALIZING`, `UNASSIGNED` 상태 변화
   - 같은 shard ID의 primary/replica가 같은 node에 배치되지 않는지

3. **실습할 때 API와 화면을 같이 보기**
   - 터미널: `_cat/shards`, `_cluster/health`, `_cluster/allocation/explain`
   - Cerebro: shard box가 노드 사이에서 이동하는 시각적 변화

4. **중요한 운영 사고 흐름**
   - 현상: yellow/red 또는 shard 이동
   - 사실 확인: `_cat/nodes`, `_cat/shards`
   - 원인 진단: `_cluster/allocation/explain`
   - 조치: reroute / allocation setting / node 복구
   - 복구 확인: health green, relocating_shards=0, unassigned_shards=0
