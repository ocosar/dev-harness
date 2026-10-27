---
name: ship
description: Gate 3 handover. Final checks on the branch, open or update the PR with an owner-readable summary and evidence, and stop — the owner merges. Use when a feature set is done and reviewed.
disable-model-invocation: true
---

1. Confirm: all target features `passes: true` with evidence (or listed as deferred), `bash .harness/check.sh` green, reviewer verdict "ready for owner".
2. Run the repo's slower pre-merge checks named in CLAUDE.md (e.g. tests inside the production Docker image). Paste the result summary into the PR.
2b. **Evals** (/dev-harness:eval, per `.harness/config.json → evals`):
   - Product evals, if enabled: run them (free ones always; paid ones only if the owner asked for them in this ship, through /dev-harness:paid-call). A regression stops here.
   - Second opinion: `"manual"` (default) → build the packet and give the owner the path with the one-line paste instruction; don't wait for it unless `required: true`. `"api"` → dry run, then one paid request. `"off"` → skip.
   - With `required: true`, a FAIL verdict (or no verdict yet) stops the ship until it is resolved.
3. `git fetch origin` and rebase/merge the base branch into the feature branch if needed; re-run checks after.
4. Open/update the PR. Body (configured language, default English), short:
   - **What changes for the user** (3 bullets max)
   - **Evidence**: review paths / screenshots / frame strips, test results (incl. in-image tests)
   - **Evals**: Claude reviewer verdict; second opinion (model + verdict, or "packet ready: <path>"); product eval score vs. baseline if enabled
   - **Cost**: spent so far on this work (ledger), expected ongoing cost if any
   - **Risks & rollback**: one line each
   - **Owner checklist**: the 1–3 things to look at before clicking Merge
5. Suggest `/dev-harness:retro` to the owner if the last retro is older than a week or this ship had owner corrections.
6. Merge mode (`"merge"` in `.harness/config.json`):
   - `"owner"` (default): stop. The owner merges on GitHub.
   - `"auto"` (only with `autonomy: "autopilot"` and `deploys_on_merge: false`): when every check, the reviewer and the evals are green and no second opinion says FAIL, merge it yourself with `gh pr merge <N> --squash` alone, and say so in the report. If anything is not green, stop as in `"owner"`.
   - `"ask"`: after the PR is open and every check above is green, ask the owner in chat "Merge PR #N now?". Only after a yes, run exactly `gh pr merge <N> --squash` on its own (if `gh pr merge` can't reach GitHub, `gh api -X PUT repos/<owner>/<repo>/pulls/<N>/merge -f merge_method=squash`). The hook shows the owner a prompt — that click is Gate 3. One PR per click; never `--admin`/`--auto`; never in an unattended session. If the click is declined, stop.
7. Otherwise: never merge, never push to main. Report in the harness format with the PR link. If deploy happens on merge (e.g. Railway from main), say so explicitly in the PR so the owner knows the merge click is also the deploy click.
