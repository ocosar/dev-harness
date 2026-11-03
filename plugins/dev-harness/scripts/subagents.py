#!/usr/bin/env python3
"""Subagent tracker (PreToolUse on Agent/Task, and SubagentStop).

A builder or evaluator subagent may still be editing files when the main session stops. The Stop hook's check and
its clean-state reminder would then judge half-written work (a failing test file the builder hasn't finished, files
"left uncommitted" that the builder commits when done). This keeps a count of running subagents in
.harness/state/subagents.json: +1 when one is launched, -1 when one stops. Entries older than MAX_AGE_S are ignored,
so a missed SubagentStop can't switch the check off for long. Never decides anything; prints nothing.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

STATE = Path(H.HARNESS_DIR) / "state" / "subagents.json"
MAX_AGE_S = 2 * 3600
AGENT_TOOLS = ("Agent", "Task")


def _load(root: Path) -> list[dict]:
    try:
        import json
        rows = json.loads((root / STATE).read_text()).get("running") or []
    except (OSError, ValueError):
        rows = []
    now = time.time()
    return [r for r in rows if isinstance(r, dict) and now - float(r.get("at", 0)) < MAX_AGE_S]


def running(root: Path) -> int:
    return len(_load(root))


def main() -> None:
    if H.yield_to_repo_copy():
        return
    payload = H.read_stdin_json()
    event = payload.get("hook_event_name", "")
    for root in H.candidate_roots(payload):
        rows = _load(root)
        if event == "SubagentStop":
            rows = rows[1:]  # oldest first: the count is what matters
        elif payload.get("tool_name") in AGENT_TOOLS:
            ti = payload.get("tool_input") or {}
            rows.append({"at": time.time(), "id": payload.get("tool_use_id") or "",
                         "what": str(ti.get("description") or ti.get("subagent_type") or "")[:80]})
        else:
            continue
        H.write_json(root / STATE, {"running": rows})


if __name__ == "__main__":
    main()
