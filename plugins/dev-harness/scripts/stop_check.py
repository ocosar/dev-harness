#!/usr/bin/env python3
"""Stop hook: the agent can't declare "done" while the repo's check command fails.

- Runs config.check.command (default: bash .harness/check.sh) only when the working tree or HEAD changed
  since the last passing run (chatty turns cost nothing).
- On failure: blocks the stop with the tail of the output, at most `max_consecutive_blocks` times in a row,
  then lets the session stop and records the failure (no infinite loops / token burn).
- No .harness/config.json or no check script → no-op.
"""
from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

STATE = Path(H.HARNESS_DIR) / "state" / "check.json"


def fingerprint(root: Path) -> str:
    head = H.git(root, "rev-parse", "HEAD")
    # .harness/ bookkeeping (progress, ledger, requests, state) never triggers a re-run
    diff = H.git(root, "diff", "HEAD", "--", ".", ":(exclude).harness")
    untracked = H.git(root, "ls-files", "--others", "--exclude-standard", "--", ".", ":(exclude).harness")
    return hashlib.sha256(f"{head}\n{diff}\n{untracked}".encode()).hexdigest()[:16]


def main() -> None:
    payload = H.read_stdin_json()
    if payload.get("stop_hook_active"):
        return
    root = H.find_root(payload.get("cwd"))
    if not root:
        return
    cfg = H.load_config(root)
    chk = cfg.get("check") or {}
    command = chk.get("command") or ""
    last = shlex.split(command)[-1] if command.strip() else ""
    if last.endswith(".sh") and not (root / last).is_file():
        command = ""  # check script not set up yet
    has_cases = (root / H.HARNESS_DIR / "cases.json").is_file()
    if not command and not has_cases:
        return
    state_path = root / STATE
    try:
        import json
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}
    fp = fingerprint(root)
    if state.get("last_pass_fp") == fp:
        return
    ok, out = True, ""
    if has_cases:  # repo's own gate regression cases (paid patterns etc.)
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "selftest.py")], cwd=root,
                           capture_output=True, text=True, timeout=60)
        ok, out = r.returncode == 0, r.stdout + r.stderr
    if ok and command:
        try:
            r = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True,
                               timeout=int(chk.get("timeout_s", 600)))
            ok, out = r.returncode == 0, (r.stdout + "\n" + r.stderr)
        except subprocess.TimeoutExpired:
            ok, out = False, f"check timed out after {chk.get('timeout_s', 600)} s"
    if ok:
        state.update(last_pass_fp=fp, consecutive_blocks=0, last_result="pass")
        H.write_json(state_path, state)
        return
    n = int(state.get("consecutive_blocks", 0)) + 1
    tail = "\n".join(out.strip().splitlines()[-40:])
    state.update(consecutive_blocks=n, last_result="fail", last_fail_fp=fp)
    H.write_json(state_path, state)
    H.log_event(root, "check_fail", attempt=n, tail=out.strip().splitlines()[-1] if out.strip() else "")
    if n > int(chk.get("max_consecutive_blocks", 3)):
        state["consecutive_blocks"] = 0
        H.write_json(state_path, state)
        H.log_event(root, "check_gave_up", attempts=n - 1)
        H.emit({"systemMessage": f"dev-harness: checks still failing after {n - 1} fix attempts — stopping. "
                                 "The failure must be in your report."})
        return
    H.emit({"decision": "block",
            "reason": f"dev-harness check failed (`{command}`, attempt {n}). Fix it before finishing, "
                      f"or if it is out of scope say so explicitly in your report.\n\n{tail}"})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a broken Stop hook must never trap the session
