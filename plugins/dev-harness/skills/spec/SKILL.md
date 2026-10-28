---
name: spec
description: Gate 1. Turn an idea or change request into docs/specs/<slug>/{spec,plan}.md and a feature contract (.harness/features.json), then stop for the owner's approval. Use for any new feature, change or experiment series before code.
disable-model-invocation: true
argument-hint: "<idea in your own words>"
---

Idea from the owner: $ARGUMENTS

1. **Clarify once, not forever.** First write your best reading of the idea in one sentence with an honest confidence number ("~60% — missing: what counts as success"). Check it covers **who** it's for, **why now**, what **success** looks like (a number or visible behaviour) and the binding **constraint**. If confidence is high (~90%+) and nothing missing would change the plan, say your reading in one line and go on. Otherwise ask ONE round of at most 4 multiple-choice questions (AskUserQuestion) aimed at the missing pieces, each option a concrete, escalating interpretation, recommended option first. Listen for the gap between what was asked for and what is wanted ("a dashboard" may really be "know by Monday whether the test set got worse"). If the session is unattended, pick the most reasonable reading and write it at the top of spec.md as "Assumed:".
2. **Delegate to the `planner` subagent** (`dev-harness:planner`) with the clarified intent. It writes `docs/specs/<slug>/spec.md`, `plan.md` and `.harness/features.json` with `"approved": false`.
3. **Check its work yourself** before showing it: every acceptance criterion checkable? paid costs sourced? features small and ordered? new dependencies listed? Fix gaps with the planner rather than passing them on.
4. Commit on the session's assigned `claude/…` branch if it has one (cloud sessions do), otherwise on `claude/spec-<slug>`; push, and present to the owner in the configured language (default English):
   - what will be built (3–5 bullets), what will NOT be built,
   - the feature list (id + title + PAID marker),
   - estimated paid cost with sources, and open questions,
   - the spec file links.
5. **Autopilot** (`autonomy: "autopilot"` in `.harness/config.json`): if every feature is free (`paid: false`, no `paid_budget_usd`), have the **reviewer** subagent check the spec for scope/risk first; if it has no BLOCKER, approve it yourself in ONE edit (same fields as below) — the hook allows it for free specs only — and continue with /dev-harness:build without stopping. Tell the owner in the report that it self-approved. A spec with paid features always stops for the owner: put `"paid_budget_usd"` (total for this spec) and `"max_call_usd"` (largest single call) in features.json so that one approval covers the paid runs inside it.
6. **Otherwise stop.** Ask: "Approve the spec?" When the owner says yes in chat, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/spec_hash.py"` and, in ONE edit of `.harness/features.json`, set `"approved": true`, `"approved_at"` (now) and `"approved_spec_sha"` (that output) — the hook shows the owner a confirmation prompt; that click is Gate 1. If spec.md/plan.md change later, the briefing flags it and building stops until the owner re-approves. If they want changes, revise and ask again (approval resets to false on any scope change).

Never start building in the same turn as writing the spec.
