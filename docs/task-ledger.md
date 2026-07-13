# Task ledger

This ledger records delegated ownership, dependencies, integration status, and
verification for the initial KEF LSX integration. The lead agent updates it as
work is inspected and integrated.

| Agent / task | Branch or worktree | Owned files | Dependencies | Status | Verification |
|---|---|---|---|---|---|
| Lead / orchestration and integration | `feat/initial-integration` (shared worktree) | cross-cutting integration, ledger, final plan | all workstreams | in progress | pending |
| Research 1 / legacy protocol and aiokef audit | shared worktree, strict ownership | `docs/research/legacy-protocol-aiokef.md` | none | in progress | lead review pending |
| Research 2 / current HA and HACS architecture | shared worktree, strict ownership | `docs/research/ha-hacs-architecture.md` | none | in progress | lead review pending |
| Research 3 / reliability and concurrency design | shared worktree, strict ownership | `docs/research/reliability-concurrency.md` | none | in progress | lead review pending |
| Research 4 / fake speaker and test design | shared worktree, strict ownership | `docs/research/testing-fake-speaker.md` | wave 1 context allowed, no file dependency | queued | pending |
| Research 5 / competing implementations | shared worktree, strict ownership | `docs/research/competing-implementations.md` | none | queued | pending |
| Research 6 / read-only live HA investigation | shared worktree, strict ownership | `docs/research/live-ha.md` | HA MCP access | queued | pending |
| Architecture reviewer A | shared worktree, strict ownership | `docs/research/architecture-a.md` | research 1-6 | queued | pending |
| Architecture reviewer B / adversarial | shared worktree, strict ownership | `docs/research/architecture-b.md` | research 1-6 and reviewer A | queued | pending |

Implementation and final-review rows will be added after architecture approval.
