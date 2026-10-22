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

Subagents: `dev-harness:planner` (Opus), `builder` (session model), `evaluator` and `reviewer` (Sonnet, read-only, fresh context).

## Unattended runs (cloud scheduled tasks, `claude -p`)

Set `HARNESS_UNATTENDED=1` in the environment. Gates 1 and 2 then deny instead of asking, paid requests are parked as `awaiting_approval`, and the run reports what it needs. The Stop hook still requires `.harness/check.sh` to pass. Keep Claude-token spend bounded with `--max-budget-usd` / `--max-turns`; those caps do not cover third-party APIs, which is why Gate 2 exists.

## Layout

```
.claude-plugin/marketplace.json      marketplace "ocosar-harness"
plugins/dev-harness/
  .claude-plugin/plugin.json
  hooks/hooks.json                   SessionStart briefing · PreToolUse gates · Stop test gate
  scripts/                           stdlib-only Python hook scripts
  agents/                            planner, builder, evaluator, reviewer
  skills/                            init, spec, build, paid-call, experiment, ship, status
  templates/                         config.json, features.json, check.sh, progress.md, CLAUDE.md block
tests/test_gates.py                  gate behaviour tests (python3 -m pytest -q)
```

## Design references

- Anthropic, *Building effective agents* — fixed workflow with human checkpoints over a free-roaming agent.
- Anthropic, *Effective harnesses for long-running agents* — initializer + progress file + JSON feature list where agents only flip `passes`.
- Anthropic, *Harness design for long-running application development* — planner / generator / evaluator separation.
- Claude Code docs — hooks (`permissionDecision`), subagents, skills, plugins & marketplaces.
- github/spec-kit, Kiro, OpenSpec — spec → plan → tasks before code.
- obra/superpowers, mattpocock/skills — TDD, subagent-driven development, fresh-context review.

Every component encodes an assumption about what models can't yet do reliably; revisit the harness when models improve and delete what is no longer needed.

MIT licence.
