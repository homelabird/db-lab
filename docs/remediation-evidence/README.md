# 2026-09-18 개선판 검증 증거

`summary.json`은 최종 성공 로그에서 집계한 결과입니다. root 91개 중 16개는 작은 Go-template subset renderer + YAML/schema 계약 검사이며 실제 Helm 검사가 아닙니다. 모든 모의 runtime/DB는 테스트 목적의 명시적 double입니다. 실제 Helm 검사 시도는 도구 부재로 127을 반환했습니다.

재현: 전용 환경에 각 프로젝트 테스트 의존성 및 scripts/requirements-checks.txt를 설치하고 `bash scripts/test-offline.sh`. 실제 Helm 검사: `bash scripts/test-helm.sh`. 실제 엔진/클러스터 시험은 docs/RUNTIME-ACCEPTANCE.md를 따릅니다.
