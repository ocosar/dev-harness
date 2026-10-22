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
   - `paid.bash_patterns`: regexes that match ONLY commands that actually spend money (e.g. `python[0-9.]*\s+\S*pipeline\.py\b[^;&|]*\s--pay\b`). Avoid false positives on `grep`, `cat`, docs. Test each regex against 3 real commands from the repo's history/docs and 3 harmless commands mentioning the same words.
   - `paid.mcp_tools`: paid MCP tools available in this environment.
   - `paid.extra_ledgers`: the repo's own spend ledger if it has one (`{"path": ..., "ts_field": ..., "usd_field": ...}`) so the daily cap counts real spend.
   - `paid.daily_cap_usd`: `$ARGUMENTS` if given, else the repo's existing CLI daily cap, else 10.
   - `language`: the language the owner uses in CLAUDE.md / chat.
   Writing this file triggers an approval prompt — that is expected; tell the owner what the cap and patterns are.

4. **Write `.harness/check.sh`**: the repo's free, fast, deterministic test command with mocks on (e.g. `FAL_MOCK=1 python -m pytest -q -x`). Run it once; it must pass on the current main. If the full suite is slow (>3 min), use a fast subset and note the full command for /dev-harness:ship.

5. **Create** `.harness/progress.md` (template), `.harness/requests/.gitkeep`, and add `.harness/state/` to `.gitignore`. Do not create features.json yet (that comes from /dev-harness:spec).

6. **CLAUDE.md:** insert the block from `templates/CLAUDE-harness.md` (between its start/end markers; replace an existing block). Do not rewrite the repo's own rules.

7. **Self-test the gates** (free): run a harmless command that matches a paid pattern only in a dry form if one exists, or explain how you verified the regexes; confirm `git commit` on main would be denied by checking out main read-only and stating the hook's expected response. Do not actually spend money.

8. Commit, push the branch, open a PR titled "dev-harness: init". Report in the harness format, listing: cap, paid patterns, check command, anything you could not make enforceable.
