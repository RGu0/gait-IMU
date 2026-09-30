# Upstream Reference Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile the four changed FeetForcePlate sources with their cited Gait-IMU documents, then update only the verified pins.

**Architecture:** The existing `check_upstream_refs` lint failure is the RED proof.  Each cited statement is compared against the current upstream source, stale conclusions or line ranges are corrected in the owning document, and only then does the checker's `--update` write normalized hashes and byte counts.  The document index receives the reconciliation receipt.

**Tech Stack:** Python 3.13, project reference checker, Markdown, JSON.

**Spec:** `D:\Onedrive\Project-Doc-Sync\gait-IMU\evidence\ray-545\requirement-revisions\R3.json`

## Global Constraints

- Scope: RAY-545 `upstream-reference-refresh`, requirement revision R3.
- Do not run `check_upstream_refs.py --update` before reading every cited document.
- Do not change Gait-IMU cloud behavior or expand into the upstream repository.
- Validate with governed `./dev lint` and `./dev test` on Windows Python 3.13.

## Review Focus

- The controlled identity-recovery routes are recorded as upstream facts, not silently treated as Gait-IMU API commitments.
- The normal session replay and its license rule retain their documented scope, while recovery-protected sessions name their different path.
- `SubjectSummary.external_identifier_id` is included where the document enumerates the response shape.
- Changed source line ranges point to current upstream code.
- The pins' normalized hashes and byte counts are written only after the prose review is complete.

---

### Task 1: Reconcile changed source citations and pins

**Files:**
- Modify: `.project-context/documents/云端服务接口约定_抄录卷.md`
- Modify: `.project-context/documents/云端服务接口约定_待确认卷.md`
- Modify: `docs/云端接口约定.md`
- Modify: `tools/upstream_refs.json`

**Interfaces:**
- Consumes: current FeetForcePlate files `shared/contracts/cloud.py`, `cloud/api/app.py`, `cloud/api/postgres.py`, and `client/sync/persistent_upload.py`.
- Produces: accurate cited facts and matching LF-normalized pins for `check_upstream_refs`.

- [x] **Step 1: Confirm the failing reference check**

Run the governed lint action and retain the four reported drifts as the RED baseline.

- [x] **Step 2: Update only findings supported by the source comparison**

Record the new controlled recovery routes, the recovery guard before normal session replay, `SubjectSummary.external_identifier_id`, and revised cited line ranges.  Keep unaffected claims unchanged.

- [x] **Step 3: Re-pin with the checker**

Run `tools/check_upstream_refs.py --update` through the project runtime after the document review.  It must update the four changed entries' `sha256` and `bytes` together.

- [x] **Step 4: Run governed lint to verify the check is green**

Expected: all five lint gates pass, including `check_upstream_refs`.

### Task 2: Record the shared-document reconciliation

**Files:**
- Modify: `.project-context/documents/派生文档索引.md`
- Create: `.project-context/evidence/ray-545/upstream-reference-refresh/acceptance/2026-09-30-upstream-reference-refresh.md`

**Interfaces:**
- Consumes: Task 1's reconciled source facts and regenerated pins.
- Produces: a durable account of what changed, what remained valid, and the evidence needed for review.

- [x] **Step 1: Add the index entry**

State the four files reviewed, the material upstream additions, affected document versions, and whether product semantics changed.

- [x] **Step 2: Write the per-file evidence**

List each upstream file, its relevant current fact, cited documents reviewed, and the fresh digest recorded by the checker.

- [ ] **Step 3: Run governed test and commit**

Expected: `./dev test` passes after the documentation and pin refresh. Commit only the documented scope files.
