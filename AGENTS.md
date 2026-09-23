# DB Lab Agent Guide

@/home/server/.codex/RTK.md

## Repository map

- `all.sh`: root dispatcher; individual labs own their runtime and lifecycle.
- `elasticsearch/`, `elasticsearch-9/`, `kafka-lab/`, `mariadb-ha-lab/`, `redis-lab/`: standalone labs.
- `mvp-lab/`: separate six-container application, not part of the default `all.sh up` batch.
- `lib/es-lab/`: shared Elasticsearch 7/9 shell and Python core.
- `helmchart/`, `ansible/`: separate deployment paths.
- `docs/QUALITY-GUIDE.md` and `docs/RUNTIME-ACCEPTANCE.md`: validation entry points and evidence boundaries.

## Project skill

Use `$db-lab-quality` for project coding, configuration, troubleshooting, review, and validation work. It describes ownership boundaries, config/lifecycle invariants, and how to report runtime evidence accurately.

Preserve unrelated dirty changes. Do not treat dated reports or offline tests as current live-runtime proof.
