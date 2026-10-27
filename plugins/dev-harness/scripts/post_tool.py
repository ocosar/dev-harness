#!/usr/bin/env python3
"""PostToolUse for dev-harness.

1. Backstop for protected files: if a shell command changed a harness control file, the spec contract (beyond
   progress fields) or a locked evaluator/reference file WITHOUT the owner having approved that command, the file is
   restored from the snapshot gate_pretool.py took just before, and Claude is told why. Commands that move HEAD
   (checkout, merge, pull, commit, rebase) are not undone — branch content is the owner's via PR review — but any
   protected change they carry is logged.
2. Paid bookkeeping: a paid call that ran (the owner clicked Yes) marks its request 'executed' and — unless the repo
   keeps its own ledger — is booked at its estimate right away; book.py records the actual amount later.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
from gate_pretool import is_paid_bash, is_paid_mcp, paid_script  # noqa: E402


def restore(root: Path, rel: str, before: dict | None) -> bool:
    p = root / rel
    try:
        if before is None:  # created by the command
            if p.is_file():
                p.unlink()
            return True
        if "text" in before:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(before["text"])
            return True
        if "b64" in before:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(before["b64"]))
            return True
        r = subprocess.run(["git", "checkout", "HEAD", "--", rel], cwd=root, capture_output=True, text=True)
        return r.returncode == 0
    except OSError:
        return False


def guard_files(root: Path, payload: dict) -> list[str]:
    tid = payload.get("tool_use_id") or "last"
    pre_path = root / H.HARNESS_DIR / "state" / "pre" / f"{tid}.json"
    try:
        pre = json.loads(pre_path.read_text())
    except (OSError, ValueError):
        return []
    finally:
        for old in (root / H.HARNESS_DIR / "state" / "pre").glob("*.json"):  # also sweep stale snapshots (>1 day)
            try:
                if old == pre_path or time.time() - old.stat().st_mtime > 86400:
                    old.unlink()
            except OSError:
                pass
    before = pre.get("snap") or {}
    after = H.snapshot(root, patterns=pre.get("patterns") or [])
    ok_pats = [p + "*" if p.endswith("/") else p for p in (pre.get("owner_ok") or [])]  # "dir/" = its subtree
    changed = [r for r in H.snapshot_diff(root, before, after) if not H.matches_any(r, ok_pats)]
    if not changed:
        return []
    cmd = (payload.get("tool_input") or {}).get("command", "")
    head_moved = H.git(root, "rev-parse", "HEAD") != pre.get("head")
    if head_moved and not re.search(r"\bgit\b[^\n;&|]*\bcommit\b", cmd):
        # HEAD moved but the command made no commit (checkout/switch/pull/merge/reset): git legitimately brought
        # other versions of protected files. Don't fight that — log and leave it.
        H.log_event(root, "protected_changed_by_git", files=changed[:8], input=cmd)
        return []
    # A commit in the command (incl. --amend and soft-reset-then-commit, which rewrite HEAD rather than extend it)
    # may have carried an unapproved change into HEAD. Restore the WORKING TREE regardless of ancestry, so the
    # change does not take effect in-session, and flag the commit below.
    reverted, kept, failed = [], [], []
    main_refs = [r for b in (H.load_config(root).get("protected_branches") or ["main", "master"])
                 for r in (f"origin/{b}", b) if H.git(root, "rev-parse", "--verify", "--quiet", r)]
    for rel in changed:
        b = before.get(rel)
        if head_moved and main_refs:
            # Same command pulled/merged the owner's main AND committed: a protected file whose content now equals
            # main's version came in through the owner's merge — keep it (e.g. right after an update PR is merged).
            cur = H.git(root, "hash-object", "--", rel)
            if cur and any(H.git(root, "rev-parse", "--quiet", "--verify", f"{ref}:{rel}") == cur for ref in main_refs):
                kept.append(rel)
                continue
        if rel == H.FEATURES:
            try:
                now = (root / rel).read_text()
            except OSError:
                now = None
            ok, _ = H.features_change_ok(b.get("text") if b else None, now)
            if ok:
                kept.append(rel)
                continue
        (reverted if restore(root, rel, b) else failed).append(rel)
    if reverted or failed:
        H.log_event(root, "protected_reverted", files=reverted[:8], unrestorable=failed[:8], input=cmd)
    out = [f"reverted {r}" for r in reverted] + [f"COULD NOT restore {r}" for r in failed]
    if reverted and H.git(root, "rev-parse", "HEAD") != pre.get("head"):
        out.append("the commit you just made contains these unapproved changes — the working tree now has the approved "
                   "versions back; commit that restore before anything else")
    return out


def book_paid(root: Path, payload: dict) -> None:
    cfg = H.load_config(root)
    if cfg.get("_broken"):
        return
    tool, ti = payload.get("tool_name", ""), payload.get("tool_input") or {}
    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        if not (is_paid_bash(cfg, cmd) or paid_script(root, cfg, cmd, payload.get("cwd"))):
            return
        key = cmd
    elif tool.startswith("mcp__") and is_paid_mcp(cfg, tool):
        key = tool
    else:
        return
    found = [(p, r) for p, r in H.find_requests(root, ("asked", "consumed"))
             if H.norm_cmd(str(r.get("command", ""))) == H.norm_cmd(key)]
    if not found:
        H.log_event(root, "paid_ran_without_request", tool=tool, input=key)
        return
    path, req = found[-1]
    now = H.now_iso(cfg)
    req.update(status="executed", executed_at=now)
    if tool == "Bash" and H.is_builtin_paid(key):
        req["builtin"] = True  # harness's own spend (e.g. second opinion): never in the repo's ledger
    if not cfg["paid"].get("extra_ledgers") or req.get("builtin"):
        H.append_jsonl(H.ledger_path(root), {"ts": now, "request": path.stem, "kind": "estimate",
                                             "usd": float(req.get("est_usd") or 0), "note": "auto-booked at estimate"})
        req["booked_estimate"] = True
    H.write_json(path, req)
    H.log_event(root, "paid_executed", request=path.stem, est=req.get("est_usd"))


def main() -> None:
    if H.yield_to_repo_copy():
        return
    payload = H.read_stdin_json()
    notes = []
    for root in H.candidate_roots(payload):
        if payload.get("tool_name") == "Bash":
            notes += guard_files(root, payload)
        book_paid(root, payload)
    if notes:
        H.emit({"decision": "block",
                "reason": "dev-harness: that command changed protected files without the owner's approval — "
                          + "; ".join(notes) + ". Harness control files, the approved spec contract and locked "
                          "evaluators only change with the owner's click (Edit/Write prompts for it). Don't retry "
                          "another way; if the change is needed, say so in your report.",
                "systemMessage": "dev-harness: " + "; ".join(notes)})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
