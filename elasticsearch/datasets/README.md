# 데이터는 실행 시 생성합니다

이 디렉터리에는 시드 데이터 본문을 동봉하지 않습니다.

```bash
# 생성하면서 바로 ES 적재. 로컬 데이터 파일 없음.
./scripts/04-seed-data.sh

# ES 연결 없이 Bulk NDJSON 파일만 생성할 때:
./scripts/04-seed-data.sh --generate-only --size-mb 100
```

두 번째 모드에서만 `datasets/generated/*.bulk.ndjson` 및 `manifest.json`이 만들어집니다. 이 디렉터리는 `.gitignore` 대상입니다. 모든 데이터는 합성 데이터이며 실제 고객 데이터가 아닙니다. 자세한 내용은 `docs/SEED-GUIDE.md`를 확인하세요.
