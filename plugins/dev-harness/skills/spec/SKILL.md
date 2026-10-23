---
name: spec
description: Gate 1. Turn an idea or change request into docs/specs/<slug>/{spec,plan}.md and a feature contract (.harness/features.json), then stop for the owner's approval. Use for any new feature, change or experiment series before code.
disable-model-invocation: true
argument-hint: "<idea in your own words>"
---

Idea from the owner: $ARGUMENTS

1. **Clarify once, not forever.** If the idea can be read in more than one way, ask ONE round of at most 4 multiple-choice questions (AskUserQuestion), each option a concrete, escalating interpretation, recommended option first. If the session is unattended, pick the most reasonable reading and write it at the top of spec.md as "Assumed:".
2. **Delegate to the `planner` subagent** (`dev-harness:planner`) with the clarified intent. It writes `docs/specs/<slug>/spec.md`, `plan.md` and `.harness/features.json` with `"approved": false`.
3. **Check its work yourself** before showing it: every acceptance criterion checkable? paid costs sourced? features small and ordered? new dependencies listed? Fix gaps with the planner rather than passing them on.
4. Commit on `claude/spec-<slug>`, push, and present to the owner in the configured language (default English):
   - what will be built (3–5 bullets), what will NOT be built,
   - the feature list (id + title + PAID marker),
   - estimated paid cost with sources, and open questions,
   - the spec file links.
5. **Stop.** Ask: "Approve the spec?" When the owner says yes in chat, set `"approved": true` and `"approved_at"` in `.harness/features.json` — the hook will show the owner a confirmation prompt; that click is Gate 1. If they want changes, revise and ask again (approval resets to false on any scope change).

Never start building in the same turn as writing the spec.
