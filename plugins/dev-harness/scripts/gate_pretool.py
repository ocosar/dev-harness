#!/usr/bin/env python3
"""PreToolUse gate for dev-harness.

Gate 1 (spec):  flipping "approved": true in .harness/features.json needs the owner's click.
Gate 2 (paid):  a Bash command / MCP tool matching the repo's paid patterns needs
                (a) a prepared request file with a verified estimate, (b) room under the daily cap,
                (c) the owner's click. One request = one call. Unattended sessions never pay.
Gate 3 (merge): agents never merge PRs, push to / commit on protected branches, or force-push them.
Also: edits to .harness/config.json (caps, patterns) need the owner's click.

No .harness/config.json above cwd → no-op (plugin is inert in other repos).
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

APPROVED_RE = re.compile(r'"approved"\s*:\s*true')


def _deny(reason: str) -> None:
    H.pretool_decision("deny", reason)


def _ask_or_deny(cfg: dict, payload: dict, reason: str) -> None:
    if H.unattended(cfg, payload):
        _deny(reason + "\n\nUnattended session: this needs the owner. Report it and stop; do not retry another way.")
    else:
        H.pretool_decision("ask", reason)


# ---------- Gate 3: merge guard ----------
PUSH_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b([^;&|\n]*)")
LOCAL_WRITE_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|merge|cherry-pick|revert|rebase)\b")


def merge_guard(root: Path, cfg: dict, cmd: str, cwd: str | None) -> str | None:
    protected = [b for b in cfg.get("protected_branches", []) if b]
    prot_alt = "|".join(re.escape(b) for b in protected) or r"(?!x)x"
    if re.search(r"\bgh\s+pr\s+merge\b", cmd) or re.search(r"\bgh\s+api\b[^\n]*/merge\b", cmd):
        return "GATE 3 — merging is the owner's job. Open/update the PR and report; never merge it yourself."
    branch = H.git(Path(cwd) if cwd else root, "rev-parse", "--abbrev-ref", "HEAD")
    for m in PUSH_RE.finditer(cmd):
        args = m.group(1)
        if re.search(r"--(all|mirror)\b", args):
            return "GATE 3 — `git push --all/--mirror` is not allowed. Push only your feature branch."
        if re.search(rf"(^|[\s:/+]){prot_alt}(\s|$)", args):
            return f"GATE 3 — pushing to a protected branch ({', '.join(protected)}) is the owner's job (via PR merge)."
        positional = [a for a in args.split() if not a.startswith("-")]
        if len(positional) <= 1 and branch in protected:
            return f"GATE 3 — you are on protected branch '{branch}'. Create a feature branch first."
    lw = LOCAL_WRITE_RE.search(cmd)
    if lw and branch in protected:
        return (f"GATE 3 — `git {lw.group(1)}` on protected branch '{branch}' is blocked. "
                "Work on a feature branch (e.g. claude/<slug>) and open a PR.")
    return None


# ---------- Gate 2: paid calls ----------
def is_paid_bash(cfg: dict, cmd: str) -> bool:
    return any(re.search(p, cmd) for p in cfg["paid"].get("bash_patterns", []) if p)


def is_paid_mcp(cfg: dict, tool: str) -> bool:
    return any(re.fullmatch(p, tool) for p in cfg["paid"].get("mcp_tools", []) if p)


def paid_gate(root: Path, cfg: dict, payload: dict, what: str) -> None:
    cap = float(cfg["paid"].get("daily_cap_usd", 0) or 0)
    spent = H.spent_today(root, cfg)
    found = H.latest_request(root, "prepared")
    if not found:
        _deny("GATE 2 — paid call blocked: no prepared approval request.\n"
              "Run the /dev-harness:paid-call procedure: write .harness/requests/<id>.json with status 'prepared', "
              "summary, payload summary, est_usd verified from the provider's pricing API, and price_source. "
              "Show the owner the inputs (frame strips, prompt, payload) first. One request covers ONE call.")
        return
    path, req = found
    try:
        est = float(req.get("est_usd"))
    except (TypeError, ValueError):
        _deny(f"GATE 2 — request {path.name} has no numeric est_usd. Verify the price first.")
        return
    now = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    req["command"] = what[:800]
    if cap and spent + est > cap + 1e-9:
        req.update(status="blocked_cap", blocked_at=now)
        H.write_json(path, req)
        _deny(f"GATE 2 — would exceed the daily cap: spent ${spent:.2f} + estimate ${est:.2f} > cap ${cap:.2f}. "
              "Raising the cap is the owner's decision (they edit .harness/config.json). Report and stop.")
        return
    if H.unattended(cfg, payload):
        req.update(status="awaiting_approval", parked_at=now)
        H.write_json(path, req)
        _deny(f"GATE 2 — unattended session: paid calls never run without the owner. Request {req.get('id', path.stem)} "
              "is parked as 'awaiting_approval'. Put it in your report (summary, estimate, review paths) and stop.")
        return
    req.update(status="asked", asked_at=now)
    H.write_json(path, req)
    H.pretool_decision("ask", (
        "GATE 2 — PAID CALL\n"
        f"{req.get('summary', '(no summary)')}\n"
        f"Estimate: ${est:.2f}  ({req.get('price_source', 'price source missing!')})\n"
        f"Today so far: ${spent:.2f} / cap ${cap:.2f}\n"
        f"Request: {req.get('id', path.stem)} — approves this ONE call only."))


# ---------- Gate 1 + config guard (file edits) ----------
def _new_text(tool: str, ti: dict) -> str:
    if tool == "Write":
        return ti.get("content", "")
    if tool == "Edit":
        return ti.get("new_string", "")
    if tool == "MultiEdit":
        return "\n".join(e.get("new_string", "") for e in ti.get("edits", []))
    return ""


def file_guard(root: Path, cfg: dict, payload: dict, tool: str, ti: dict) -> bool:
    fp = ti.get("file_path") or ""
    if not fp:
        return False
    p = Path(fp)
    p = (Path(payload.get("cwd") or root) / p) if not p.is_absolute() else p
    try:
        rel = p.resolve().relative_to(root).as_posix()
    except ValueError:
        return False
    if rel == ".harness/config.json":
        _ask_or_deny(cfg, payload, "Harness config change (caps, paid patterns, protected branches). "
                                   "Only the owner changes these — approve only if you asked for it.")
        return True
    if rel == ".harness/features.json":
        new = _new_text(tool, ti)
        if tool == "Write":
            try:
                before = p.read_text()
            except OSError:
                before = ""
        elif tool == "Edit":
            before = ti.get("old_string", "")
        else:
            before = "\n".join(e.get("old_string", "") for e in ti.get("edits", []))
        # Only the edit that INTRODUCES approval is gated; progress updates to an approved file pass.
        if APPROVED_RE.search(new) and not APPROVED_RE.search(before):
            _ask_or_deny(cfg, payload, "GATE 1 — SPEC APPROVAL. This marks the spec + feature list as approved, "
                                       "so agents may start building. Approve only if you read and accept the spec.")
            return True
    return False


def main() -> None:
    payload = H.read_stdin_json()
    root = H.find_root(payload.get("cwd"))
    if not root:
        return
    cfg = H.load_config(root)
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        reason = merge_guard(root, cfg, cmd, payload.get("cwd"))
        if reason:
            _deny(reason)
            return
        if is_paid_bash(cfg, cmd):
            paid_gate(root, cfg, payload, cmd)
    elif tool.startswith("mcp__"):
        if is_paid_mcp(cfg, tool):
            paid_gate(root, cfg, payload, f"{tool} {str(ti)[:600]}")
    elif tool in ("Edit", "Write", "MultiEdit"):
        file_guard(root, cfg, payload, tool, ti)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail visible, never silently open: a human sees the prompt
        H.pretool_decision("ask", f"dev-harness gate error ({type(e).__name__}: {e}). Review this action manually.")
