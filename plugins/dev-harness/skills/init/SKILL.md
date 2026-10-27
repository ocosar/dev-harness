---
name: init
description: Set up dev-harness in the current repo — .harness/ state files, paid-call patterns and daily cap, free check script, CLAUDE.md block, .gitignore. Run once per repo.
disable-model-invocation: true
argument-hint: "[daily cap USD]"
---

Set up dev-harness in this repository. Templates are in `${CLAUDE_PLUGIN_ROOT}/templates/`.

1. **Survey (read-only):** CLAUDE.md, README, how tests run, how paid external APIs are called (CLI flags like `--pay`, SDK calls, curl hosts, MCP tools such as `mcp__fal__*`), existing ledgers/caps, mock flags, review/output conventions, protected branches. The repo's existing rules win over harness defaults — the harness only makes them enforceable.

2. **Create a branch** `claude/harness-init` (never work on main).

3. **Write `.harness/config.json`** from the template:
   - `paid.bash_patterns`: regexes that match ONLY commands that actually spend money. Match the script, not the interpreter, so `./pipeline.py`, `python3 pipeline.py` and `uv run pipeline.py` all count: e.g. `pipeline\.py\b[^;&|\n]*\s--pay\b`. Find EVERY entry point that can reach a paid API (grep the code for the provider SDK / HTTP host, and read how past paid runs were launched). Avoid false positives on `grep`, `cat`, docs. Test each regex against 3 real commands and 3 harmless commands mentioning the same words.
   - `merge`: ask the owner — `"owner"` (default: they merge on GitHub) or `"ask"` (Claude merges ONE PR at a time after a yes in chat plus a click on the prompt; no GitHub visit needed). Unattended sessions can never merge.
   - `paid.mcp_tools`: paid MCP tools available in this environment — both `mcp__<server>__<tool>` and plugin-bundled `mcp__plugin_<plugin>_<server>__<tool>` names.
   - `timezone`: the owner's IANA timezone (e.g. `Europe/Istanbul`) — the daily cap resets at their midnight, and the repo's own ledger timestamps are converted to it.
   - `paid.extra_ledgers`: the repo's own spend ledger if it has one (`{"path": ..., "ts_field": ..., "usd_field": ...}`) so the daily cap counts real spend. (Then the harness does not book paid calls itself, to avoid double counting.)
   - `paid.secret_env`: the env var names of paid API keys (e.g. `FAL_KEY`, `PIPELINE_ANTHROPIC_KEY`). Autoloop evaluations run without them, and the overnight command unsets them.
   - `paid.daily_cap_usd`: `$ARGUMENTS` if given, else the repo's existing CLI daily cap, else 10.
   - `language`: `en` (default — the owner works in English). Use another language only if the owner explicitly asks. Existing non-English docs in the repo stay as they are; do not translate them.
   The first write creates the harness, so it is NOT gated yet — list the cap, patterns, locked paths and timezone in your report so the owner approves them by merging. From then on every change to it prompts the owner.

4. **Second lock on paid entry points:** copy `${CLAUDE_PLUGIN_ROOT}/templates/paid_guard.py` to `.harness/paid_guard.py` and add ONE call to `require_approval()` in each paid CLI path, right before the first paid request (see the file's docstring; e.g. inside `if args.pay:`). Never in production code paths that have their own budget (a web worker). Add a test with the repo's mocks that the CLI refuses `--pay` without an approved request. If the paid path isn't a script you can edit (e.g. an MCP tool), say so in the report.

5. **Write `.harness/check.sh`**: the repo's free, fast, deterministic test command with mocks on (e.g. `FAL_MOCK=1 python -m pytest -q -x`). It must also work in a fresh cloud container: if the test tools may be missing, install them first inside check.sh (e.g. `python3 -c 'import pytest' 2>/dev/null || pip install -q -r requirements-dev.txt`). Run it once; it must pass on the current main. If the full suite is slow (>3 min), use a fast subset and note the full command for /dev-harness:ship.

6. **Create** `.harness/progress.md`, `.harness/lessons.md` (templates; seed lessons.md with up to 10 hard-won rules already written in the repo's CLAUDE.md/docs, each with its source), `.harness/cases.json` (template; fill it with the 3+3 commands you tested the paid regexes against, and any evaluator paths that must stay locked), `.harness/requests/.gitkeep`, and add `.harness/state/` to `.gitignore`. Add `.harness/progress.md merge=union` to `.gitattributes` (append-only log; avoids rebase conflicts). Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/selftest.py"` — it must pass. Do not create features.json yet (that comes from /dev-harness:spec).

7. **CLAUDE.md:** insert the block from `templates/CLAUDE-harness.md` (between its start/end markers; replace an existing block). Do not rewrite the repo's own rules.

8. **Self-test the gates** (free): `selftest.py` already checks this repo's paid/locked cases plus built-in merge/protected-file escapes. Also feed one real payload through the gate and show the decision, e.g.
   `echo '{"cwd":"'$PWD'","tool_name":"Bash","tool_input":{"command":"<a real paid command from the docs>"}}' | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gate_pretool.py"` → must print a `deny` (no request prepared). Never actually spend money.

9. Commit, push the branch, open a PR titled "dev-harness: init". Report in the harness format, listing: cap, paid patterns, check command, anything you could not make enforceable.
