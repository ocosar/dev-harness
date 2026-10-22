---
name: build
description: Work through the approved feature contract — one feature at a time, builder → evaluator → reviewer, check.sh green, progress logged — until done, blocked, or a paid call is needed. Use after the spec is approved.
disable-model-invocation: true
argument-hint: "[feature id | all]"
---

Target: $ARGUMENTS (default: the next unfinished feature; `all` = keep going until done or blocked).

Preconditions — stop and report if any fails:
- `.harness/features.json` exists and `"approved": true`.
- Working tree clean or only harness bookkeeping changed; on a `claude/<slug>` branch (create it from the spec branch / main if needed).

Loop, per feature:
1. Mark it `in_progress`. Delegate to the **builder** subagent (`dev-harness:builder`) with the feature id. It works test-first and commits.
2. Delegate to the **evaluator** subagent (`dev-harness:evaluator`) with the feature id. If FAIL → send the concrete failures back to the builder (max 2 rounds), then mark `blocked` with the reason.
3. If the evaluator says `NEEDS_PAID_RUN` → stop the loop and run the /dev-harness:paid-call procedure for exactly that call. Never batch several paid calls behind one approval.
4. Mark `passes: true` only when every criterion is PASS with evidence. Append to `.harness/progress.md`.
5. Keep each feature to one coherent commit series; do not start the next feature while the current one is red.

After the last feature (or when stopping): delegate to the **reviewer** subagent (`dev-harness:reviewer`) for the whole branch, fix BLOCKERs (builder), re-run evaluator for touched features, then hand over to /dev-harness:ship.

Stop conditions: all done → ship. Blocked → report what is needed. Paid call needed → paid-call. Context getting long → write progress.md and tell the owner to continue in a fresh session (the SessionStart briefing will pick it up).

Report in the harness format at every stop.
