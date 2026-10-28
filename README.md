# dev-harness

A Claude Code plugin that turns "I have an idea" into reviewed pull requests **without the owner writing code** — while keeping the three decisions that matter in human hands:

| Gate | What the owner does | How it is enforced |
|---|---|---|
| **1. Spec** | Reads a short spec + feature list, says yes | Setting `"approved": true` in `.harness/features.json` triggers a Yes/No prompt (denied in unattended runs) |
| **2. Paid call** | Looks at the inputs + estimate, clicks Yes — once per call | Commands/tools matching the repo's paid patterns are blocked unless a request file with a verified estimate exists, the daily cap has room, and the owner clicks. Unattended runs never pay |
| **3. Merge** | Reviews the PR, clicks Merge on GitHub — or, optionally (`"merge": "ask"`), says yes in chat and clicks the merge prompt | Agents can't merge on their own, push to / commit on `main`, or force-push it |

Everything else — planning, building test-first, evaluating, reviewing, logging — is done by agents, and **state lives in the repo** (`docs/specs/`, `.harness/features.json`, `.harness/progress.md`, `.harness/ledger.jsonl`), so any new session (cloud, laptop, phone) picks up where the last one stopped. No copy-pasting between chats.

## What it can and can't guarantee

Three layers, from soft to hard:

1. **Hooks** (guard rails). They stop the normal paths:
   - the paid command, and a shell script that wraps one;
   - several paid calls in one command;
   - `gh pr merge`, GitHub-API merges, and `git push` to main in any form (`HEAD`, `@`, quoted, `-c …`, checkout-then-merge);
   - MCP merge/push tools;
   - approving or re-scoping the spec;
   - editing control or locked files with Edit/Write or a shell command.

   They also work when the session starts in the folder above the repo, which is how cloud sessions start, and they **fail closed** if `.harness/config.json` is unreadable. A PostToolUse hook **restores** protected files that a shell command changed without your approval. An agent that deliberately obfuscates can still get past a text pattern, which is why the next two layers exist.
2. **Locks inside the program.** `.harness/paid_guard.py` sits in the repo's paid CLI path. It runs only a command you approved through the prompt in the last 30 minutes, and only once. The autoloop runner (not the agent) decides keep or discard. It cross-checks its state against the approved config and a mirror copy inside `.git`.
3. **Hard backstops.**
   - **No paid keys in unattended runs** (`env -u …`) or in autoloop evaluations (`paid.secret_env`).
   - **A spend limit at each provider**, if it offers one. The harness cap only sees spend recorded in this checkout.
   - **Your merge.** GitHub branch protection would make Gate 3 server-side, but it needs GitHub Pro for private repos. Without it, keep an eye on the PR list.

Requirements: `python3` 3.9+ (macOS's built-in one is fine), `git`, and Claude Code 2.1.295+ (for `onFailure: block`). The hooks do nothing in repos without `.harness/config.json`.

## Install

### Option A — inside the repo (works everywhere, incl. Claude Code cloud sessions)

Cloud sessions don't load plugins ("Plugins aren't available in this environment"). Install the harness into the repo instead; Claude Code loads `.claude/settings.json` hooks, `.claude/skills/` and `.claude/agents/` from any checkout:

```
git clone https://github.com/ocosar/dev-harness /tmp/dev-harness
python3 /tmp/dev-harness/install_project.py .      # run at the repo root, on a branch
```

Commit the result via a PR; it is active from the next session. Commands are `/dev-harness-init`, `/dev-harness-spec`, `/dev-harness-build`, `/dev-harness-paid-call`, `/dev-harness-experiment`, `/dev-harness-autoloop`, `/dev-harness-retro`, `/dev-harness-ship`, `/dev-harness-status`; subagents are `dev-harness-planner`, `dev-harness-builder`, `dev-harness-evaluator`, `dev-harness-reviewer`. Re-run the installer to update (idempotent; other settings are kept). Inside a repo that already has the harness, that run asks you once (since 0.6.1). The installed copy is a protected control file — changes go through this repo and a re-install PR. If the plugin is also enabled, it stays silent in repos that have their own copy (since 0.3.3). In the rest of this README, `/dev-harness:x` = `/dev-harness-x` for repo installs.

### Option B — plugin (local Claude Code on your machine)

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
| `/dev-harness:paid-call <why>` | Prepare → **Gate 2** → run → book exactly one paid call (`scripts/book.py` records the actual cost; the estimate is booked automatically when the call runs) |
| `/dev-harness:experiment <goal>` | Variant tuning: one variable per run, free dry-run first, owner picks the winner |
| `/dev-harness:eval` | Second opinion from another model (paste-in packet by default, API optional) and product evals vs. a baseline (optional) |
| `/dev-harness:ship` | Final checks, PR with evidence → **Gate 3** (you merge) |
| `/dev-harness:status` | "Where are we?" in under 25 lines |
| `/dev-harness:autoloop <profile>` | Overnight tuning for a **free**, script-measured goal: locked evaluator, keep/reset by the runner, report PR |
| `/dev-harness:retro` | Self-improvement: score the period, curate lessons, propose harness PRs with regression tests |

Subagents: `dev-harness:planner` (Opus), `builder` (session model), `evaluator` and `reviewer` (Sonnet, read-only, fresh context).

## How the harness improves itself

The harness never changes itself silently; it learns through files and PRs you merge.

1. **Capture (automatic).** Every gate deny/ask, check failure, paid call and autoloop event goes to `.harness/events/<branch>.jsonl`. Any line you type starting with `LESSON:` lands in `.harness/lessons-inbox/<branch>.md` and applies at once. One file per branch, so the harness never creates merge conflicts for you.
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

The evaluator's last stdout line is `{"score": <num>, "ok": <guards passed>, "notes": "..."}`. `scripts/autoloop.py` (not the agent):
- commits each attempt and checks that locked files are byte-identical (otherwise it HALTs);
- discards edits outside `editable`, and diffs that add a `forbid_in_diff` token (e.g. test-file ids = metric gaming);
- runs the evaluator without the keys in `paid.secret_env`;
- keeps only improvements (or an equal score with less code) and git-resets the rest;
- stops at `max_runs` / `max_hours` / `patience` runs without a keep;
- at the end scores the holdout once on the base and once on the best commit (overfitting shows), and writes `docs/autoloop/<run>.md`.

A Stop hook keeps an unattended session going while budget remains, and gives up visibly after 3 nudges that produce no new run. Overnight:

```
caffeinate -i env -u FAL_KEY -u PIPELINE_ANTHROPIC_KEY HARNESS_UNATTENDED=1 \
  claude -p "/dev-harness:autoloop area-accuracy" --permission-mode acceptEdits --allowedTools "Bash,Read,Edit,Write,Grep,Glob"
```

## Unattended runs (cloud scheduled tasks, `claude -p`)

Set `HARNESS_UNATTENDED=1` in the environment. Gates 1 and 2 then deny instead of asking, paid requests are parked as `awaiting_approval`, and the run reports what it needs. The Stop hook still requires `.harness/check.sh` to pass. Keep Claude-token spend bounded with `--max-budget-usd` / `--max-turns`; those caps do not cover third-party APIs, which is why Gate 2 exists.

## Layout

```
.claude-plugin/marketplace.json      marketplace "ocosar-harness"
plugins/dev-harness/
  .claude-plugin/plugin.json
  hooks/hooks.json                   SessionStart briefing · LESSON capture · PreToolUse gates · PostToolUse restore + booking · Stop gate
  scripts/                           stdlib-only Python: hooks, autoloop.py, book.py, spec_hash.py, selftest.py
  agents/                            planner, builder, evaluator, reviewer
  skills/                            init, spec, build, paid-call, experiment, autoloop, retro, ship, status
  templates/                         config.json, features.json, check.sh, progress.md, lessons.md, cases.json, paid_guard.py, CLAUDE.md block
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

- **0.7.0** (2026-10-28). Agent instructions strengthened with condensed practices from [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) (MIT, see THIRD_PARTY_NOTICES.md). Reviewer: intent first, tests before code with a throwaway-worktree mutation check, five review axes, a security-and-money checklist (paid paths, secrets, untrusted data incl. LLM output, destructive derived paths, SSRF, personal data, dependencies), change sizing, ordered findings with concrete fixes. Builder: see the test fail for the right reason, prove-it for bugs, scope discipline ("noticed, not touched"), test-double order, stop-the-line debugging, error output as data, an excuse table. Planner: scope check (who / why / success / constraint), Always–Ask first–Never boundaries, vertical slices riskiest-first, sources not memory, a riskiest-assumptions list. Spec skill: one-sentence reading with a confidence number before asking. Evaluator: distrust easy wins. Not adopted: the upstream slash commands and skills themselves (they would compete with this harness's gated workflow).
- **0.6.5** (2026-10-28). From the route-to-drone update session: (1) the built-in self-test expected every merge to be blocked, so `"merge": "ask"` failed it and the Stop hook blocked every stop — it now checks each merge mode's intended behaviour (owner: blocked; ask/auto: a plain one-PR merge reaches the click/rule; `--admin` and pushes to main blocked in all modes); (2) the merge check no longer reads heredoc text that is only written to a file (`cat > f <<EOF`, `tee f <<EOF`); heredocs fed to a shell still count; (3) the installer renames leftover `/dh-…` and `/dev-harness:…` command names in `.harness/paid_guard.py`, `.harness/check.sh` and the managed CLAUDE.md block (the two protected files only when upgrading from 0.6.5 or later, whose gate allows it).
- **0.6.4** (2026-10-27). Re-review of 0.6.3: (1) the allow-list no longer auto-approves `make`, `npx`, or package-manager `run`/`exec`/`build` (they run arbitrary repo targets), nor reads/writes of absolute paths outside the repo; (2) fixed a false positive in 0.6.3's commit-restore: a command that merges/pulls main and commits no longer undoes harness files the owner merged into main. Honest limit, stated plainly: test runners and package installs stay auto-approved in assisted mode and they execute repo code; the backstops for money are the paid guard inside paid CLIs, paid keys kept out of unattended runs, and provider spend limits.
- **0.6.3** (2026-10-27). Independent review of the 0.4–0.6 changes. Fixes: (1) assisted/autopilot auto-approval is now an allow-list of known-safe commands, not a block-list — interpreter one-liners, env dumps, writes outside the repo and deploy CLIs fall through to the normal permission prompt; (2) a protected file changed and folded into HEAD by `commit --amend` or soft-reset-then-commit is now restored (was only logged); (3) a paid request can't be rewound from a spent status to `prepared` to re-run the call; (4) the spend ledger and request files are guarded against shell truncation/rewrite; (5) `llm_client.py` uses `allow_abbrev=False` so `--sen` can't send; (6) free product evals strip every KEY/TOKEN/SECRET-named env var by default; (7) a repo-local `install_project.py` doesn't get the one-click whole-install approval.
- **0.6.2** (2026-10-27). Ship skill: in Claude Code cloud sessions merge with the REST call (`gh pr merge` needs GraphQL, which is blocked there).
- **0.6.1** (2026-10-27). From the route-to-drone pilot: running `install_project.py .` inside a repo that has the harness asks the owner ONCE ("HARNESS UPDATE") instead of being reverted file by file. Approving a protected directory now covers the files inside it. Closed a gap: a single command that changed protected files and committed them in the same step was only logged; now the files are restored and the agent is told to commit the restore. Upgrading from 0.3.x–0.6.0: copy `harness_lib.py`, `gate_pretool.py`, `post_tool.py` one by one into `.claude/dev-harness/scripts/` (one click each), then run the installer (one click).
- **0.6.0** (2026-10-27). Autonomy levels (`.harness/config.json → autonomy`):
  - `manual` (default) — as before.
  - `assisted` — free, low-risk tool calls (tests, git on feature branches, edits inside the repo, package installs) run without Claude Code's permission prompts. Risky ones (rm -r, sudo, curl/ssh, deploy CLIs, force-push, writes outside the repo) keep the normal prompt; anything that may read secrets is asked explicitly. All three gates unchanged.
  - `autopilot` — assisted + free specs approve themselves after the reviewer; paid calls run without a click inside the budget the owner approved with the spec (`paid_budget_usd`, `max_call_usd` in features.json; daily cap still wins; changing the budget or the spec needs the owner again); `merge: "auto"` lets Claude merge green PRs — never when `deploys_on_merge` is true.
- **0.5.0** (2026-10-27). Evals beyond Claude grading Claude — `/dev-harness:eval`:
  1. **Second opinion, manual (default):** at ship time the harness writes `.harness/evals/<branch>/packet.md` (spec, acceptance criteria, evidence, diff, fixed reviewer questions; secret-looking values redacted). Paste it into any other model, paste the answer back, and the verdict (PASS / CONCERNS / FAIL) is recorded and shown in the PR.
  2. **Second opinion, API (optional):** `second_opinion.py --send` sends the same packet to OpenAI, Gemini, OpenRouter or Anthropic. It is a paid call (request + estimate from configured prices + cap + click), booked even when the repo has its own ledger. `required: true` makes a FAIL stop the ship.
  3. **Product evals (optional):** the repo's eval command scores a fixed set; `product_eval.py` compares it with `.harness/evals/baseline.json` (a control file — moving it needs the owner). A pluggable judge model is passed to the command (`HARNESS_JUDGE_PROVIDER/MODEL`, `llm_client.py` speaks to all four providers). Paid eval sets go through Gate 2; free ones run without paid-API keys.
- **0.4.0** (2026-10-27). Optional merge by click: with `"merge": "ask"` in `.harness/config.json`, Claude may merge one PR at a time (`gh pr merge <N>` alone, or the GitHub MCP merge tool) after the owner's yes in chat and a click on the prompt — no GitHub visit needed. Default stays `"owner"`. `--admin`, `--auto`, chained merges, direct pushes to main and unattended merges stay blocked.
- **0.3.3** (2026-10-27). From the first route-to-drone pilot step: when a repo has its own installed copy, the plugin copy stays silent (hooks ran twice); skills use the session's assigned `claude/…` branch; init makes `check.sh` install its test tools in a fresh cloud container.
- **0.3.2** (2026-10-27). Repo installs use the full name: `/dev-harness-init` etc. (was `/dh-init`); re-installing removes the old `dh-*` copies.
- **0.3.1** (2026-10-26). `install_project.py`: install the harness into a repo's `.claude/` (no plugin system needed — cloud sessions). The installed hooks, skills, agents and `.claude/settings.json` are control files. `/dev-harness:status` reads the per-branch ledger.
- **0.3.0** (2026-10-26). Self-review plus an independent adversarial review (20 findings).
  - Gates follow the session into child repos (cloud sessions start in the parent folder).
  - A broken config fails closed.
  - Spec approval is checked semantically: re-scoping an approved spec needs the owner, and `approved_spec_sha` flags edits made after approval.
  - Shell writes to protected files are asked about up front and restored afterwards.
  - Merge escapes closed: `HEAD`/`@`, quoted names, `-c`/`--no-pager`, checkout-then-merge, `gh api` merges/refs/GraphQL, MCP merge/push tools.
  - Paid requests must name the exact command, expire after 12 h, and allow one call per command (no loops). A cap of 0 means off, not unlimited. Calls are auto-booked at the estimate; `book.py` records actuals.
  - `paid_guard.py` adds a second lock inside the program.
  - Stop hook: fixed retries (Claude Code sets `stop_hook_active` on the stop after a block), new files counted by content, a deleted check script now blocks, and unattended autoloops keep going.
  - Autoloop:
    - state is cross-checked against the config and a `.git` mirror;
    - a single score line is enforced;
    - bytecode is purged;
    - `direction` is validated;
    - noisy evaluators are refused;
    - `report` runs only on the loop branch;
    - the holdout is sealed during the loop and scored on base and best;
    - plateau stop, `forbid_in_diff`, and paid keys stripped.
  - Events, lessons inbox and ledger are now one file per branch (no merge conflicts).
  - Configured timezone; NotebookEdit is gated; case-insensitive paths on macOS. 90 tests.
- **0.2.0** (2026-10-26) — autoloop (locked evaluator, runner-decided keep/reset, holdout, report PR); self-improvement loop (events log, `LESSON:` capture, lessons in every briefing, repo `cases.json` selftest in the Stop hook, `/dev-harness:retro`); locked paths guard.
- **0.1.1** (2026-10-23) — English as default working language.
- **0.1.0** (2026-10-22) — spec / paid-call / merge gates, build loop, experiment discipline.

MIT licence.
