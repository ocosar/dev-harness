---
name: autoloop
description: Overnight, unattended tuning loop for a FREE, measurable goal (autoresearch-style) — edit allowed files, run the locked evaluator, keep only real improvements, log every run, stop at budget, report a PR. Use when an owner-approved autoloop profile exists in .harness/config.json.
argument-hint: "<profile> | continue | report"
---

Target: $ARGUMENTS

Runner (decides keep/discard, scope and honesty — not you): `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autoloop.py"`

**Preconditions.** The profile exists in `.harness/config.json` → `autoloop.<profile>` (only the owner adds or edits it — that click IS the approval of goal, metric, evaluator, editable files and budget). Its evaluation is free. If there is no profile, help the owner write one, ending with the config edit (which prompts them), then stop.

**Start** (`<profile>`): run `autoloop.py start <profile>`. Read its output: goal, baseline score, editable files. Read the editable files, the evaluator (read-only!), `.harness/lessons.md`, and any notes on what was already tried (previous `docs/autoloop/*.md` for this profile).

**Loop — do not stop to ask the owner whether to continue; they are asleep.** Each iteration:
1. Pick ONE idea. Prefer ideas that explain a specific failure the evaluator reports (its `notes`), then simple parameter changes, then bigger rewrites. Don't repeat a discarded idea unless combined with something new.
2. Edit ONLY editable files. Never touch locked files, tests' expectations, data, or dependencies.
3. `autoloop.py run --desc "<idea in one line>"` — it commits, verifies, evaluates, then keeps or resets. Read its verdict and build on the best state.
4. A crash from your own bug (typo, import): fix once and re-run. If the idea itself is broken, move on.
5. Every ~10 runs, append 3 lines to `.harness/progress.md`: best so far, what is working, what isn't.

**Stop** when the runner exits 10 (budget) or 3 (HALT), or `run` keeps saying nothing improves after 15 consecutive attempts. Then:
- `autoloop.py report` (runs the holdout once, writes `docs/autoloop/<run>.md`, commits it).
- Push the loop branch and open a PR titled "autoloop <run>: <baseline> → <best>" with the report as body. Never merge.
- If HALTED (a locked file changed): say so first in the PR and in your final message — the owner must look.

**Rules that keep it honest:** simpler is better (equal score with less code is kept); no special-casing the evaluation files (e.g. branching on a file id or name) — that is gaming the metric, and the reviewer will reject the PR; never weaken a guard the evaluator checks. Paid calls are impossible here by design; if an idea needs one, write it down for the owner instead.

**Running it overnight (owner, on the machine that has the data):**
```
cd <repo> && HARNESS_UNATTENDED=1 claude -p "/dev-harness:autoloop <profile>" --permission-mode acceptEdits --allowedTools "Bash,Read,Edit,Write,Grep,Glob"
```
`HARNESS_UNATTENDED=1` makes every owner-only gate deny instead of ask, so nothing waits for a click at 3 a.m.
