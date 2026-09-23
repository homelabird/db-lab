---
name: db-lab-quality
description: Implement, debug, or review code, Compose configuration, and documentation in this DB Lab monorepo. Use when a change touches all.sh, a standalone database lab, the shared Elasticsearch core, MVP, Helm, or Ansible.
---

# DB Lab Quality

Make the smallest change that fixes the owning project's demonstrated problem. Keep evidence precise: source tests, Compose rendering, local container runs, and real database or cluster behavior prove different things.

## Find the owner first

- Read the root `README.md` for entry points and project boundaries, then the affected lab's README and current troubleshooting/validation guide.
- `all.sh` is a dispatcher. The independent lab owns its `lab.sh`, configuration, data, and safeguards. `all.sh up` means the four default HA labs; `all.sh mvp up` is a separate six-container application. Elasticsearch 9 is opt-in (`es9`).
- Elasticsearch 7 and 9 share `lib/es-lab/`; trace both wrappers/callers before changing shared behavior. Each project's own `scripts/common.sh`, `.env`, and Compose files still define version-specific settings.
- Preserve existing dirty-worktree changes. Inspect `git status` and relevant diffs before editing; don't clean or replace unrelated files.

## Keep configuration and lifecycle coherent

- Treat each `.env.example` as the editable configuration contract. When changing a setting, trace how the lab loader parses it, how Compose interpolates it, which scripts consume it, and where the docs describe it. Do not assume all labs use the same `.env` parser.
- Generated credential-bearing `.env` files must remain private and must not be printed, committed, or copied into evidence. Keep Compose defaults aligned with script defaults and documented defaults.
- Verify project/volume/container naming together. Some Compose stacks use fixed `container_name`; changing only `COMPOSE_PROJECT_NAME` may not isolate a second copy. Follow the lab's documented prefix/identity settings.
- Normal `down` generally preserves named data. Read the exact command implementation before cleanup. Use a lab's explicit purge/reset path only when the user requested data deletion and the target is unambiguous; never substitute a broad prune or volume deletion.
- Keep local firewall, package installation, deployment, and remote-host changes opt-in. A diagnosis command should distinguish application startup failure from container-network failure before suggesting host firewall changes.

## Make validation match the claim

- Select the narrow existing check for the changed owner. Reuse existing tests and validation scripts; add a small regression check for a demonstrated bug when appropriate. Don't run a broad destructive/runtime workflow just because a syntax or source change was made.
- For Compose edits, render the affected files with the intended provider and a safe example env when available. Rendering checks parsing/interpolation only; it does not prove image pulls, container startup, readiness, or persistence.
- Run real `up`, smoke, fault, purge, Helm, or Ansible workflows only when the task calls for that evidence and the target is isolated. Check current engine/context/project identity first; don't reuse existing user volumes for disposable validation.
- Report each result at its real level: static/unit, Compose-rendered, container-running, service-ready, or end-to-end data/failover. State which providers/versions were exercised and which were not. Never infer whole-repository support from one lab/provider.
- For an install/readiness failure, inspect its generated report and service-specific logs. Preserve raw output secrecy; summarize classified causes and the next useful command rather than dumping credentials or entire noisy logs.

## Keep documentation useful

- Update the affected lab README/help text and troubleshooting guide when commands, config keys, defaults, destructive behavior, or support claims change.
- Treat dated repair/validation reports as historical evidence. The root README should link to current project guidance and must not contradict per-lab runtime evidence.
- Distinguish `down` (preserve) from explicit data removal, and distinguish supported from actually tested runtime/provider combinations.

## Finish

Summarize the owner files changed, the user-visible behavior, exact checks run, runtime evidence not obtained, and any remaining risk. Do not claim production or HA guarantees from this educational lab's tests.
