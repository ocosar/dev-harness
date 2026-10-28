#!/usr/bin/env python3
"""Stop hook.

1. Autoloop keep-going: while an autoloop is active on its branch and budget remains, the session may not stop
   (overnight runs otherwise end whenever the model decides to). Bounded: if 3 blocks in a row bring no new run,
   the stop is allowed.
2. Test gate: the agent can't declare "done" while the repo's selftest cases or check command fail.
   - Runs only when the working tree or HEAD changed since the last passing run (chatty turns cost nothing).
   - Blocks at most `max_consecutive_blocks` times per turn, then lets the session stop with a visible message
     and does not nag again until the code changes.

Claude Code sets `stop_hook_active` on every stop that follows a block, so the per-turn counter restarts only
when it is false (a fresh turn). No .harness/config.json → no-op.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

STATE = Path(H.HARNESS_DIR) / "state" / "check.json"
AUTOLOOP = Path(H.HARNESS_DIR) / "state" / "autoloop.json"


def fingerprint(root: Path) -> str:
    head = H.git(root, "rev-parse", "HEAD")
    # .harness/ bookkeeping (progress, ledger, requests, events, state) never triggers a re-run
    diff = H.git(root, "diff", "HEAD", "--", ".", ":(exclude).harness")
    h = hashlib.sha256(f"{head}\n{diff}\n".encode())
    for rel in H.git(root, "ls-files", "--others", "--exclude-standard", "--", ".", ":(exclude).harness").splitlines():
        f = root / rel
        try:  # new files count by content, not just by name
            st = f.stat()
            h.update(rel.encode() + (f.read_bytes() if st.st_size < 5_000_000 else f"{st.st_size}:{st.st_mtime}".encode()))
        except OSError:
            continue
    return h.hexdigest()[:16]


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def autoloop_keep_going(root: Path, payload: dict, state: dict) -> dict | None:
    """A hook output if this stop must be blocked to keep an active autoloop running (or a message when it stalls)."""
    al = load(root / AUTOLOOP)
    if not al or al.get("finished") or al.get("halted"):
        return None
    if H.git(root, "rev-parse", "--abbrev-ref", "HEAD") != al.get("branch"):
        return None
    from autoloop import budget_reason  # same directory
    if budget_reason(al):
        reason = (f"Autoloop budget is used up ({budget_reason(al)}). Run the report now: "
                  "`autoloop.py report`, push the branch, open the PR. Then stop.")
    else:
        reason = (f"Autoloop {al['run_id']} is still running: {al['runs']}/{al['max_runs']} runs, best {al['best']} "
                  f"(baseline {al['baseline']}). Do not stop — the owner is away. Pick the next idea and call "
                  "`autoloop.py run --desc ...`. The runner itself ends the loop (budget or plateau).")
    last_runs = state.get("autoloop_runs_at_block")
    stuck = state.get("autoloop_stuck", 0) + 1 if (payload.get("stop_hook_active") and last_runs == al["runs"]) else 0
    if stuck >= 3:
        state.update(autoloop_stuck=0)
        H.write_json(root / STATE, state)
        H.log_event(root, "autoloop_stalled", run=al["run_id"], runs=al["runs"])
        return {"systemMessage": f"dev-harness: autoloop {al['run_id']} stalled (3 nudges, no new run) — "
                                 "stopping. Run `/dev-harness:autoloop report` to finish it."}
    state.update(autoloop_runs_at_block=al["runs"], autoloop_stuck=stuck)
    H.write_json(root / STATE, state)
    return {"decision": "block", "reason": reason}


def clean_state(root: Path, cfg: dict, payload: dict) -> dict | None:
    """Leave the repo resumable: one reminder per stop sequence (never a loop), and never on a protected branch
    (work there is the gates' business)."""
    if not cfg.get("clean_state", True) or payload.get("stop_hook_active"):
        return None
    branch = H.git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD" or branch in (cfg.get("protected_branches") or []):
        return None
    dirty = []
    try:  # -z: exact paths, no trimming/quoting surprises
        raw = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"], cwd=root,
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        raw = ""
    entries = raw.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        code, rel = e[:2], e[3:]
        if code[0] in "RC":
            i += 1  # rename/copy: the next entry is the old path
        if not H.matches_any(rel, H.BOOKKEEPING) and not H.matches_any(rel.rstrip("/") + "/x", H.BOOKKEEPING):
            dirty.append(rel)
    ahead = 0
    if H.git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
        try:
            ahead = int(H.git(root, "rev-list", "--count", "@{u}..HEAD") or 0)
        except ValueError:
            ahead = 0
    if not dirty and not ahead:
        return None
    parts = []
    if dirty:
        parts.append(f"{len(dirty)} uncommitted file(s) ({', '.join(dirty[:5])}{'…' if len(dirty) > 5 else ''}). "
                     "Commit finished work (harness log files may ride along in the same commit). If something is "
                     "deliberately left unfinished, write in .harness/progress.md what is left and why, then commit that")
    if ahead:
        parts.append(f"{ahead} commit(s) not pushed — push the branch")
    H.log_event(root, "clean_state_reminder", dirty=len(dirty), ahead=ahead)
    return {"decision": "block", "reason": "dev-harness: leave the repo resumable before stopping — " + "; ".join(parts)
            + ". (One reminder only; if you stop again it is allowed.)"}


def main() -> None:
    if H.yield_to_repo_copy():
        return
    payload = H.read_stdin_json()
    outs = [o for o in (check_root(r, payload) for r in H.candidate_roots(payload)) if o]
    blocks = [o for o in outs if o.get("decision") == "block"]
    if blocks:
        H.emit({"decision": "block", "reason": "\n\n".join(o["reason"] for o in blocks)})
    elif outs:
        H.emit({"systemMessage": " · ".join(o["systemMessage"] for o in outs if o.get("systemMessage"))})


def check_root(root: Path, payload: dict) -> dict | None:
    cfg = H.load_config(root)
    if cfg.get("_broken"):
        return {"decision": "block", "reason": f"dev-harness: {root.name}/.harness/config.json is unreadable "
                                               f"({cfg['_broken']}). Tell the owner; nothing is checked until it is fixed."} \
            if not payload.get("stop_hook_active") else {"systemMessage": f"dev-harness: {root.name} config unreadable"}
    state_path = root / STATE
    state = load(state_path)
    keep = autoloop_keep_going(root, payload, state)
    if keep:
        return keep
    chk = cfg.get("check") or {}
    command = chk.get("command") or ""
    last = shlex.split(command)[-1] if command.strip() else ""
    missing = last.endswith(".sh") and not (root / last).is_file()
    has_cases = (root / H.HARNESS_DIR / "cases.json").is_file()
    if not command and not has_cases:
        return clean_state(root, cfg, payload)
    fp = fingerprint(root)
    if state.get("last_pass_fp") == fp or state.get("gave_up_fp") == fp:
        return clean_state(root, cfg, payload)  # already green here (or already reported failing for this code)
    ok, out, what = True, "", ""
    if H.init_needed(root):  # fresh machine: install/verify tools first, so the first check doesn't fail on setup
        ok, out = H.run_init(root, cfg)
        what = H.INIT_SCRIPT
    if ok and has_cases:  # repo's own gate regression cases (paid patterns etc.)
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "selftest.py")], cwd=root,
                           capture_output=True, text=True, timeout=60)
        ok, out, what = r.returncode == 0, r.stdout + r.stderr, "selftest (.harness/cases.json)"
    if ok and missing:
        ok, out, what = False, f"{last} is missing — the check script was deleted or never created (run /dev-harness:init)", last
    elif ok and command:
        t0 = time.time()
        try:
            r = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True,
                               timeout=int(chk.get("timeout_s", 600)))
            ok, out, what = r.returncode == 0, (r.stdout + "\n" + r.stderr), command
        except subprocess.TimeoutExpired:
            ok, out, what = False, f"check timed out after {time.time() - t0:.0f} s", command
    if ok:
        state.update(last_pass_fp=fp, blocks_this_turn=0, gave_up_fp=None, last_result="pass")
        H.write_json(state_path, state)
        return clean_state(root, cfg, payload)
    n = (int(state.get("blocks_this_turn", 0)) if payload.get("stop_hook_active") else 0) + 1
    tail = "\n".join(out.strip().splitlines()[-40:])
    state.update(blocks_this_turn=n, last_result="fail", last_fail_fp=fp)
    H.log_event(root, "check_fail", attempt=n, check=what, tail=out.strip().splitlines()[-1] if out.strip() else "")
    if n > int(chk.get("max_consecutive_blocks", 3)):
        state.update(blocks_this_turn=0, gave_up_fp=fp)
        H.write_json(state_path, state)
        H.log_event(root, "check_gave_up", attempts=n - 1, check=what)
        return {"systemMessage": f"dev-harness: `{what}` in {root.name} still fails after {n - 1} fix attempts — "
                                 "stopping. The failure must be in the report; nothing should be shipped like this."}
    H.write_json(state_path, state)
    return {"decision": "block",
            "reason": f"dev-harness check failed in {root.name} (`{what}`, attempt {n}). Fix it before finishing, "
                      f"or if it is out of scope say so explicitly in your report.\n\n{tail}"}


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a broken Stop hook must never trap the session
