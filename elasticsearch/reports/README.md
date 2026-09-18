# 실행 후 생성되는 보고서

- `seed-manifest.json`: 시드 실행 결과. 원문 크기, 문서 수, 설정, 클러스터 UUID, status.
- `live-verification.json`: 실제 ES에서 `lab.sh verify`가 실행한 결과.

ZIP에는 실제 ES 적재를 수행한 것처럼 보이는 가짜 PASS 보고서를 넣지 않습니다. 제공 환경의 생성기·단위 테스트 결과는 `docs/VALIDATION.md`에서 별도로 확인합니다.

## 장애 실습 결과

`faults/active.json`: 진행 중이거나 복구가 미완료인 실험의 원래 설정. 삭제하지 마세요.
`faults/<run_id>.json`: 완료된 실험의 장애 확인/복구 결과.
`faults/suite-<id>.json`: 기본 전체 시험이 성공한 경우의 요약.
`validation/`: 제작 환경 오프라인/모의 검사 로그. 실제 사용자 클러스터 결과와 다릅니다.
