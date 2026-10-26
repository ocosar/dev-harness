---
name: autoloop
description: Overnight, unattended tuning loop for a FREE, measurable goal (autoresearch-style) — edit allowed files, run the locked evaluator, keep only real improvements, log every run, stop at budget, report a PR. Use when an owner-approved autoloop profile exists in .harness/config.json.
argument-hint: "<profile> | continue | report"
---

Target: $ARGUMENTS

Runner (decides keep/discard, scope and honesty — not you): `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autoloop.py"`

**Preconditions.** The profile exists in `.harness/config.json` → `autoloop.<profile>` (only the owner adds or edits it — that click IS the approval of goal, metric, evaluator, editable files and budget). Its evaluation is free. If there is no profile, help the owner write one, ending with the config edit (which prompts them), then stop.

**Continue** (`continue`, or the briefing says an autoloop is ACTIVE): run `autoloop.py status`, read the last rows of the results in `.harness/state/autoloop/<run>/results.tsv`, then keep looping from the best state. **Report** (`report`): jump to Stop.

**Evaluator rules (when the owner asks you to write one):** compute the score in the evaluator process, outside the code under test (run that code as a subprocess or read its outputs); print exactly ONE JSON score line, last; write nothing inside the repo except gitignored paths; never call paid APIs. The runner rejects runs with more than one score line, runs that leave files behind, and evaluations that see paid keys.

**Start** (`<profile>`): run `autoloop.py start <profile>`. Read its output: goal, baseline score, editable files. Read the editable files, the evaluator (read-only!), `.harness/lessons.md`, and any notes on what was already tried (previous `docs/autoloop/*.md` for this profile).

**Loop — do not stop to ask the owner whether to continue; they are asleep.** Each iteration:
1. Pick ONE idea. Prefer ideas that explain a specific failure the evaluator reports (its `notes`), then simple parameter changes, then bigger rewrites. Don't repeat a discarded idea unless combined with something new.
2. Edit ONLY editable files. Never touch locked files, tests' expectations, data, or dependencies.
3. `autoloop.py run --desc "<idea in one line>"` — it commits, verifies, evaluates, then keeps or resets. Read its verdict and build on the best state.
4. A crash from your own bug (typo, import): fix once and re-run. If the idea itself is broken, move on.
5. Every ~10 runs, append 3 lines to `.harness/progress.md`: best so far, what is working, what isn't.

**Stop** only when the runner says so: exit 10 (budget, time, or plateau — `patience` runs without a keep) or exit 3 (HALT). A Stop hook keeps the session going while budget remains; don't fight it, and don't end early because ideas feel exhausted — try a different kind of idea. Then:
- `autoloop.py report` (scores the holdout once on the base and once on the best commit — if the holdout got worse while the tuning score rose, say "overfit" in the PR title — writes `docs/autoloop/<run>.md`, commits it).
- Push the loop branch and open a PR titled "autoloop <run>: <baseline> → <best>" with the report as body. Never merge.
- If HALTED (a locked file changed): say so first in the PR and in your final message — the owner must look.

**Rules that keep it honest:** simpler is better (equal score with less code is kept); no special-casing the evaluation inputs (branching on a file id/name, hard-coded expected values, reading reference/answer files at runtime) — the runner discards diffs that add a `forbid_in_diff` token, and the reviewer rejects the rest; never weaken a guard the evaluator checks. You may READ the evaluator to understand the metric, never to copy answers out of it. Paid calls are impossible by design (evaluations run without the paid keys); if an idea needs one, write it down for the owner instead.

**Running it overnight (owner, on the machine that has the data):**
```
cd <repo> && caffeinate -i env -u FAL_KEY -u PIPELINE_ANTHROPIC_KEY HARNESS_UNATTENDED=1 \
  claude -p "/dev-harness:autoloop <profile>" --permission-mode acceptEdits --allowedTools "Bash,Read,Edit,Write,Grep,Glob"
```
`HARNESS_UNATTENDED=1` makes every owner-only gate deny instead of ask, so nothing waits for a click at 3 a.m.; `env -u <KEY>` removes the paid keys listed in `paid.secret_env` (adapt the list), so even a mistake can't spend money; `caffeinate -i` keeps the Mac awake (macOS).
