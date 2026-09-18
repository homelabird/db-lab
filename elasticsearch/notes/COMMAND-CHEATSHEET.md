# 명령 요약

프로젝트 루트에서 실행합니다. 설정은 `.env.example`을 참고하세요.

```bash
./scripts/01-up.sh
./scripts/04-seed-data.sh
./scripts/09-verify-seed.sh
./scripts/08-query-examples.sh list
./scripts/08-query-examples.sh 04-high-risk
./scripts/10-pit-pagination.sh
./scripts/03-status.sh
./scripts/07-dataset-size.sh
./scenarios/01-manual-shard-move.sh
./scripts/05-reset-cluster-settings.sh
./scripts/02-down.sh
```

검색 요청 구조와 응답 해석은 `docs/QUERY-GUIDE.md`, 샤드 실습은 `docs/SHARD-LABS.md`를 확인하세요. 기본 시드는 현재 시각이 아니라 2026년 8월 UTC 데이터입니다.
