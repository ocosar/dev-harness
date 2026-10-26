# dev-harness

A Claude Code plugin that turns "I have an idea" into reviewed pull requests **without the owner writing code** — while keeping the three decisions that matter in human hands:

| Gate | What the owner does | How it is enforced |
|---|---|---|
| **1. Spec** | Reads a short spec + feature list, says yes | Setting `"approved": true` in `.harness/features.json` triggers a Yes/No prompt (denied in unattended runs) |
| **2. Paid call** | Looks at the inputs + estimate, clicks Yes — once per call | Commands/tools matching the repo's paid patterns are blocked unless a request file with a verified estimate exists, the daily cap has room, and the owner clicks. Unattended runs never pay |
| **3. Merge** | Reviews the PR, clicks Merge | Agents can't `gh pr merge`, push to / commit on `main`, or force-push it |

Everything else — planning, building test-first, evaluating, reviewing, logging — is done by agents, and **state lives in the repo** (`docs/specs/`, `.harness/features.json`, `.harness/progress.md`, `.harness/ledger.jsonl`), so any new session (cloud, laptop, phone) picks up where the last one stopped. No copy-pasting between chats.

## Install (once per machine / cloud environment)

```
/plugin marketplace add ocosar/dev-harness
/plugin install dev-harness@ocosar-harness
```

To make every session in a project load it automatically (incl. cloud sessions), commit this to the project's `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "ocosar-harness": { "source": { "source": "github", "repo": "ocosar/dev-harness" } }
  },
  "enabledPlugins": { "dev-harness@ocosar-harness": true }
}
```

## Use

| Command | When |
|---|---|
| `/dev-harness:init [cap]` | Once per repo: paid patterns, daily cap, free check script, CLAUDE.md block |
| `/dev-harness:spec <idea>` | New feature/change → spec + feature list → **Gate 1** |
| `/dev-harness:build [id\|all]` | Builder → evaluator → reviewer loop, one feature at a time |
| `/dev-harness:paid-call <why>` | Prepare → **Gate 2** → run → book exactly one paid call |
| `/dev-harness:experiment <goal>` | Variant tuning: one variable per run, free dry-run first, owner picks the winner |
| `/dev-harness:ship` | Final checks, PR with evidence → **Gate 3** (you merge) |
| `/dev-harness:status` | "Where are we?" in under 25 lines |
| `/dev-harness:autoloop <profile>` | Overnight tuning for a **free**, script-measured goal: locked evaluator, keep/reset by the runner, report PR |
| `/dev-harness:retro` | Self-improvement: score the period, curate lessons, propose harness PRs with regression tests |

Subagents: `dev-harness:planner` (Opus), `builder` (session model), `evaluator` and `reviewer` (Sonnet, read-only, fresh context).

## How the harness improves itself

The harness never changes itself silently; it learns through files and PRs you merge.

1. **Capture (automatic).** Every gate deny/ask, check failure and autoloop event goes to `.harness/events.jsonl`. Any line you type starting with `LESSON:` lands in `.harness/lessons-inbox.md` and applies at once.
2. **Remember (every session).** `.harness/lessons.md` (≤ 30 curated rules) is injected into each session's briefing and checked by the planner and reviewer.
3. **Guard against regressions.** False positives and misses become cases in `.harness/cases.json`; the Stop hook runs `selftest` so a config change can't bring them back. Harness-level fixes come with a test in `tests/`.
4. **Retro (you trigger it, or schedule it weekly).** `/dev-harness:retro` scores the period (corrections, false-positive denies, relays, $ per feature), curates lessons, and opens PRs — including deletions of parts that caught nothing.
5. **You merge.** No retro may widen what agents can do without you (spend, merge, approve specs, edit locked evaluators).

## Autoloop (autoresearch-style)

For goals where every run is free and a script computes the score. The owner adds a profile to `.harness/config.json` (that click approves goal, metric, evaluator, editable files, budget):

```json
"autoloop": { "area-accuracy": {
  "goal": "more rooms within ±5% of the plan's area labels",
  "eval_cmd": "python ilan-poc/tools/autoloop_eval.py --set ayar",
  "holdout_cmd": "python ilan-poc/tools/autoloop_eval.py --set test",
  "direction": "max", "editable": ["ilan-poc/ilanpoc/rooms.py"], "locked": ["ilan-poc/tools/autoloop_eval.py", "ilan-poc/regression/*"],
  "time_budget_s": 300, "max_runs": 80, "max_hours": 8 } }
```

The evaluator's last stdout line is `{"score": <num>, "ok": <guards passed>, "notes": "..."}`. `scripts/autoloop.py` (not the agent) commits each attempt, verifies locked files are byte-identical (else HALT), rejects out-of-scope edits, keeps only improvements (or equal score with less code), resets the rest, logs `results.tsv`, runs the holdout once at the end and writes `docs/autoloop/<run>.md`. Overnight:

```
caffeinate -i env HARNESS_UNATTENDED=1 claude -p "/dev-harness:autoloop area-accuracy" --permission-mode acceptEdits --allowedTools "Bash,Read,Edit,Write,Grep,Glob"
```

## Unattended runs (cloud scheduled tasks, `claude -p`)

Set `HARNESS_UNATTENDED=1` in the environment. Gates 1 and 2 then deny instead of asking, paid requests are parked as `awaiting_approval`, and the run reports what it needs. The Stop hook still requires `.harness/check.sh` to pass. Keep Claude-token spend bounded with `--max-budget-usd` / `--max-turns`; those caps do not cover third-party APIs, which is why Gate 2 exists.

## Layout

```
.claude-plugin/marketplace.json      marketplace "ocosar-harness"
plugins/dev-harness/
  .claude-plugin/plugin.json
  hooks/hooks.json                   SessionStart briefing · LESSON capture · PreToolUse gates · Stop test + selftest gate
  scripts/                           stdlib-only Python hook scripts
  agents/                            planner, builder, evaluator, reviewer
  skills/                            init, spec, build, paid-call, experiment, autoloop, retro, ship, status
  templates/                         config.json, features.json, check.sh, progress.md, lessons.md, cases.json, CLAUDE.md block
tests/                              gate, autoloop and learning tests (python3 -m pytest -q)
```

## Design references

- Anthropic, *Building effective agents* — fixed workflow with human checkpoints over a free-roaming agent.
- Anthropic, *Effective harnesses for long-running agents* — initializer + progress file + JSON feature list where agents only flip `passes`.
- Anthropic, *Harness design for long-running application development* — planner / generator / evaluator separation.
- Claude Code docs — hooks (`permissionDecision`), subagents, skills, plugins & marketplaces.
- github/spec-kit, Kiro, OpenSpec — spec → plan → tasks before code.
- obra/superpowers, mattpocock/skills — TDD, subagent-driven development, fresh-context review.

Every component encodes an assumption about what models can't yet do reliably; revisit the harness when models improve and delete what is no longer needed.

- karpathy/autoresearch — fixed-budget experiment loop, single editable file, read-only evaluator, keep or git-reset, results.tsv, "simpler is better".
- Memento (Memento-Teams) — learn from stored cases instead of retraining; small, curated memory works best → `lessons.md`.

## Changelog

- **0.2.0** (2026-10-26) — autoloop (locked evaluator, runner-decided keep/reset, holdout, report PR); self-improvement loop (events log, `LESSON:` capture, lessons in every briefing, repo `cases.json` selftest in the Stop hook, `/dev-harness:retro`); locked paths guard.
- **0.1.1** (2026-10-23) — English as default working language.
- **0.1.0** (2026-10-22) — spec / paid-call / merge gates, build loop, experiment discipline.

MIT licence.
