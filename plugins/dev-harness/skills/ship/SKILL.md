---
name: ship
description: Gate 3 handover. Final checks on the branch, open or update the PR with an owner-readable summary and evidence, and stop — the owner merges. Use when a feature set is done and reviewed.
disable-model-invocation: true
---

1. Confirm: all target features `passes: true` with evidence (or listed as deferred), `bash .harness/check.sh` green, reviewer verdict "ready for owner".
2. Run the repo's slower pre-merge checks named in CLAUDE.md (e.g. tests inside the production Docker image). Paste the result summary into the PR.
3. `git fetch origin` and rebase/merge the base branch into the feature branch if needed; re-run checks after.
4. Open/update the PR. Body (configured language, default English), short:
   - **What changes for the user** (3 bullets max)
   - **Evidence**: review paths / screenshots / frame strips, test results (incl. in-image tests)
   - **Cost**: spent so far on this work (ledger), expected ongoing cost if any
   - **Risks & rollback**: one line each
   - **Owner checklist**: the 1–3 things to look at before clicking Merge
5. Suggest `/dev-harness:retro` to the owner if the last retro is older than a week or this ship had owner corrections.
6. Stop. Never merge, never push to main. Report in the harness format with the PR link. If deploy happens on merge (e.g. Railway from main), say so explicitly in the PR so the owner knows the merge click is also the deploy click.
