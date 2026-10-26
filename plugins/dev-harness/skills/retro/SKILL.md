---
name: retro
description: The harness's self-improvement step. Reads what happened (gate events, check failures, owner lessons and corrections, PR comments, spend, autoloop reports), updates the repo's lessons, scores the period, and proposes harness changes as PRs with regression cases — the owner merges. Use after each ship, weekly, or when the owner says the harness got in the way.
argument-hint: "[since YYYY-MM-DD]"
---

Period: $ARGUMENTS (default: since the last entry in `.harness/retro.md`, else the last 14 days).

The harness improves only through PRs the owner merges. You may never weaken a gate: anything that lets an agent spend money, merge, approve a spec, or change a locked evaluator without the owner is out of scope for a retro — at most, describe it as a question for the owner.

**1. Gather (read-only)**
- `.harness/events/*.jsonl` (one file per branch) in the period: `gate_deny` / `gate_ask` (with tool, input, reason), `check_fail`, `check_gave_up`, `lesson_captured`, `paid_executed`, `paid_booked`, `paid_ran_without_request`, `autoloop_*`.
- `.harness/lessons-inbox/*.md`, `.harness/lessons.md`, `.harness/progress.md` (incl. "Pilot metrics"), `.harness/ledger/*.jsonl` (+ the repo's own ledgers in config), `.harness/requests/` (any `asked`/`executed` request never booked = a bookkeeping gap), `docs/autoloop/*.md`.
- Only what is merged or on this branch is visible here; run the retro on main after merges, or say which branches were not covered.
- Owner's PR reviews and comments in the period: `gh pr list --state all --search "updated:>=<date>"`, then `gh pr view <n> --comments`. Owner corrections are the most valuable signal.

**2. Classify every gate deny/ask**: correct (it stopped something that needed the owner) · false positive (it blocked a free/harmless action) · unclear. Count friction: owner messages that only relayed or re-explained something the repo state already held.

**3. Score the period** — append one row to `.harness/retro.md` (create with this header if missing):
`| period | features shipped | owner corrections | false-positive denies | missed catches | check give-ups | relays | USD paid | USD per feature | autoloop best Δ |`
Use numbers you counted; write `?` when the data isn't there and say what would capture it.

**4. Repo lessons (PR in this repo)**
- Curate `.harness/lessons-inbox/*.md` + recurring failures into `.harness/lessons.md`: one line each, `- <area>: <rule> (source: <date / PR / commit>)`, ≤ 30 lines total, merge duplicates, drop lessons that are now enforced in code or obsolete. Delete the curated inbox files after curating.
- Every false positive or missed paid pattern becomes a case in `.harness/cases.json` (`paid_must_not_match` / `paid_must_match`) plus the config fix that makes `selftest` pass. Config changes prompt the owner — that is expected.

**5. Harness changes (PR on ocosar/dev-harness)** — only for problems that are not repo-specific: a gate that misfires everywhere, a skill step that keeps getting skipped, a report the owner keeps asking to reformat, a missing check.
- One PR per change, each with a regression test in `tests/` (or a case file) that fails before and passes after, and a one-paragraph "why" citing the events/corrections.
- Bump the plugin version (patch for fixes, minor for new behaviour); add a line to the README changelog.
- Also propose deletions: a gate, step or agent that caught nothing in the period and cost time is a candidate to remove (harness components encode model weaknesses that may have gone).
- If this session has no write access to ocosar/dev-harness, write the proposal (diff + test) into `docs/harness-proposals/<date>-<slug>.md` instead.

**6. Report to the owner** (configured language, ≤ 15 lines): the score row, top 3 lessons added, PR links, and decisions only the owner can make.
