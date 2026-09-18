# 명령 요약

프로젝트 루트에서 실행합니다. 설정은 `.env.example`을 참고하세요.

```bash
./lab.sh up
./lab.sh seed
./lab.sh verify
./lab.sh query list
./lab.sh query 04-high-risk
./lab.sh pit
./lab.sh status
./lab.sh size
./scenarios/01-manual-shard-move.sh
./scripts/05-reset-cluster-settings.sh
./scripts/02-down.sh
```

검색 요청 구조와 응답 해석은 `docs/QUERY-GUIDE.md`, 샤드 실습은 `docs/SHARD-LABS.md`를 확인하세요. 기본 시드는 현재 시각이 아니라 2026년 8월 UTC 데이터입니다.
