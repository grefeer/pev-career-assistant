# Platform Handover Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the platform handover summary so the next implementation session can distinguish completed real-job-sync work, external verification gaps, and the next recommended work packages.

**Architecture:** Update the existing handover in place so it remains the single source of project handoff context. Derive every completion claim from the merged code, design/plan, commit history, and fresh verification evidence; then test the document with a context-free reader.

**Tech Stack:** Markdown, Git, PowerShell, ripgrep, pytest, Ruff.

## Global Constraints

- Modify `docs/platform-foundation-handover-summary.md`; do not create a competing handover summary.
- Preserve existing platform-foundation content that remains true.
- Record `master` completion commit `75463aa` and fresh result `540 passed, 9 skipped`.
- State that `TEST_TENCENT_DOCS_TOKEN` was unavailable and the live Tencent gate was not executed.
- Never include secret values or password-bearing URLs.
- Never describe `pending_completion` postings as verified or submission-authorized.
- Preserve the rule that Tencent access is read-only and Redis is not job authority.

---

### Task 1: Refresh the handover facts and completed-task inventory

**Files:**
- Modify: `docs/platform-foundation-handover-summary.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-07-15-real-job-sync-vertical-slice-design.md`, `docs/superpowers/plans/2026-07-15-real-job-sync-vertical-slice.md`, commit range `c00f2ed..75463aa`, and current repository verification.
- Produces: one current handover summary containing completed work, remaining gaps, code locations, and next-step recommendations.

- [ ] **Step 1: Update document identity and architecture**

Set the completion version to `75463aa`; update the introduction and Mermaid diagram to include the fixed Tencent Smartsheet MCP read path while keeping MySQL, Redis, MinIO, backend, frontend, and future GUI Agent boundaries.

- [ ] **Step 2: Add the completed real-job-sync task inventory**

Add one section that explicitly covers these nine delivered areas:

1. authoritative schema and migration `20260715_0003`;
2. fixed-endpoint read-only Tencent MCP gateway;
3. source schema validation and mapping;
4. immutable raw snapshots, posting upsert, MySQL leases, and filtered reads;
5. page-by-page synchronization and safe audit/failure handling;
6. authenticated admin sync and job query APIs;
7. MySQL/live-source/redaction gates;
8. Compose/runbook/release-gate updates;
9. final global review and hardening.

- [ ] **Step 3: Correct stale incomplete-work statements**

Remove the claim that reliable Tencent reading, cleaning, and de-duplication are wholly unimplemented. Replace it with the remaining product work: manual JD entry, completion/review workflow, matching/reporting, resume lifecycle, and GUI Agent execution.

- [ ] **Step 4: Refresh operations and evidence**

Add `TENCENT_DOCS_TOKEN` and `TEST_TENCENT_DOCS_TOKEN` instructions without values; update migration/version, commands, key code paths, relevant design/plan links, and verification counts. Explicitly record the live-token external verification gap.

- [ ] **Step 5: Add ordered next-step recommendations**

Recommend the next work packages in dependency order, beginning with a user-facing job completion/review workflow, followed by manual JD ingestion and deduplication, matching/report generation, resume management, and finally GUI Agent form filling under the existing human-submit boundary.

- [ ] **Step 6: Run structural and safety checks**

Run:

```powershell
rg -n "75463aa|20260715_0003|540 passed, 9 skipped|TEST_TENCENT_DOCS_TOKEN|pending_completion|下一步" docs/platform-foundation-handover-summary.md
rg -n "392 passed|腾讯智能文档可靠读取、清洗和去重真实职位" docs/platform-foundation-handover-summary.md
git diff --check -- docs/platform-foundation-handover-summary.md
```

Expected: the first command finds every required fact; the second command has no matches; `git diff --check` exits 0.

### Task 2: Reader-test and finalize the handover

**Files:**
- Review: `docs/platform-foundation-handover-summary.md`

**Interfaces:**
- Consumes: the refreshed handover from Task 1.
- Produces: evidence that a context-free reader can identify current capabilities, external gaps, and the next implementation scope.

- [ ] **Step 1: Ask a fresh reader the handoff questions**

Give a fresh reviewer only the handover document and ask it to answer:

1. What real-job-sync capabilities are complete?
2. Which data store is authoritative for job state?
3. Can the backend write to Tencent?
4. What does `pending_completion` mean?
5. Which release gate remains externally blocked?
6. What should the next implementation session work on first?

- [ ] **Step 2: Check ambiguity and contradictions**

Ask the reviewer to identify stale counts, contradictory completion claims, unsafe secret examples, missing prerequisites, and statements that could grant GUI Agent submission authority.

- [ ] **Step 3: Correct any reader-visible gaps**

Make only factual or structural corrections discovered by reader testing; do not expand the product scope.

- [ ] **Step 4: Verify final repository state**

Run:

```powershell
git diff --check -- docs/platform-foundation-handover-summary.md
git status --short
```

Expected: the handover diff is whitespace-clean; pre-existing `.gitignore` and `AGENTS.md` changes remain preserved.
