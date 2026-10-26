---
name: status
description: Owner-readable status of the harness in this repo — spec approval, feature progress, spend today vs cap, parked paid requests, open PRs, what is needed from the owner. Use when the owner asks "where are we?".
---

Build a short status report from repo state only (no guessing):
- Spec: path, approved or not (`.harness/features.json`).
- Features: table id | title | status | passes | evidence link.
- Money: today's spend vs cap (`.harness/ledger/*.jsonl` + configured extra ledgers), this week's total, parked requests in `.harness/requests/` with status `awaiting_approval` or `asked`.
- Git: current branch, last 5 commits, open PRs (`gh pr list` if available).
- Last 3 entries of `.harness/progress.md`.
- **Needs owner:** a numbered list of decisions/approvals, or "nothing".

Write it in the configured language (default English), under 25 lines.
