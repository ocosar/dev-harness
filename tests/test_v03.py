"""v0.3 hardening (self-review 2026-10-26): shell writes to protected files, git/MCP merge escapes, paid scripts,
deterministic paid-call booking, autoloop gaming/plateau/secret stripping/holdout, autoloop keep-going Stop hook."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "scripts"
PAY_RE = r"python[0-9.]*\s+\S*pipeline\.py\b[^;&|]*\s--pay\b"


def hook(script, payload, env=None):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPTS / script)], input=json.dumps(payload),
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def decision(out):
    return None if out is None else out["hookSpecificOutput"]["permissionDecision"]


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def script(repo, name, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=repo, capture_output=True, text=True,
                          timeout=120, env=e)


@pytest.fixture
def repo(tmp_path):
    g(tmp_path, "init", "-q", "-b", "main")
    g(tmp_path, "config", "user.email", "t@t")
    g(tmp_path, "config", "user.name", "t")
    (tmp_path / ".harness" / "requests").mkdir(parents=True)
    cfg = {"paid": {"bash_patterns": [PAY_RE], "mcp_tools": ["mcp__fal__submit_job"], "daily_cap_usd": 10,
                    "secret_env": ["FAKE_PAID_KEY"]}, "check": {"command": ""},
           "locked_paths": ["refdata/*"]}
    (tmp_path / ".harness" / "config.json").write_text(json.dumps(cfg))
    (tmp_path / "refdata").mkdir()
    (tmp_path / "refdata" / "a.json").write_text("{}")
    (tmp_path / "README.md").write_text("x")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-q", "-m", "init")
    g(tmp_path, "checkout", "-q", "-b", "claude/work")
    return tmp_path


def bash(repo, cmd, **kw):
    return {"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": cmd}, "permission_mode": "default", **kw}


# ---------- protected files via the shell ----------
@pytest.mark.parametrize("cmd", [
    "sed -i 's/\"approved\": false/\"approved\": true/' .harness/features.json",
    "echo '{}' > .harness/config.json",
    "cp /tmp/x.json refdata/a.json",
    "rm -rf refdata",
    "python3 -c \"open('.harness/features.json','w').write('{}')\"",
    "git checkout HEAD~1 -- refdata/a.json",
    "jq '.approved=true' .harness/features.json | tee .harness/features.json",
])
def test_shell_writes_to_protected_files_need_owner(repo, cmd):
    assert decision(hook("gate_pretool.py", bash(repo, cmd))) == "ask"
    assert decision(hook("gate_pretool.py", bash(repo, cmd), env={"HARNESS_UNATTENDED": "1"})) == "deny"


@pytest.mark.parametrize("cmd", [
    "cat .harness/features.json",
    "python tools/compare.py refdata/a.json out/listing.json > out/cmp.txt 2>&1",
    "jq . .harness/config.json",
    "cp refdata/a.json /tmp/copy.json",
    "python3 -c \"import json; print(json.load(open('.harness/features.json')))\"",
    "grep -n approved .harness/features.json",
])
def test_shell_reads_of_protected_files_pass(repo, cmd):
    assert hook("gate_pretool.py", bash(repo, cmd)) is None


# ---------- merge escapes ----------
@pytest.mark.parametrize("cmd", ["git -c core.x=y push origin main", "git --no-pager push origin HEAD:main",
                                 "git -C . push origin +HEAD:refs/heads/main", "gh api -X PUT repos/o/r/pulls/3/merge"])
def test_git_escapes_denied(repo, cmd):
    assert decision(hook("gate_pretool.py", bash(repo, cmd))) == "deny"


def test_bare_push_from_branch_tracking_main_denied(repo):
    g(repo, "remote", "add", "origin", "https://example.invalid/r.git")
    g(repo, "config", "branch.claude/work.remote", "origin")
    g(repo, "config", "branch.claude/work.merge", "refs/heads/main")
    g(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    out = hook("gate_pretool.py", bash(repo, "git push"))
    assert decision(out) == "deny" and "tracks" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert hook("gate_pretool.py", bash(repo, "git push -u origin HEAD:claude/work")) is None


@pytest.mark.parametrize("tool,ti,expect", [
    ("mcp__github__merge_pull_request", {"pull_number": 3}, "deny"),
    ("mcp__github__create_or_update_file", {"branch": "main", "path": "x"}, "deny"),
    ("mcp__github__push_files", {"branch": "refs/heads/master"}, "deny"),
    ("mcp__github__create_or_update_file", {"branch": "claude/work", "path": "x"}, None),
    ("mcp__github__create_pull_request", {"base": "main", "head": "claude/work"}, None),
])
def test_mcp_merge_and_push(repo, tool, ti, expect):
    assert decision(hook("gate_pretool.py", {"cwd": str(repo), "tool_name": tool, "tool_input": ti})) == expect


# ---------- paid: scripts, prompt content, booking ----------
def test_script_wrapping_paid_command_is_paid(repo):
    (repo / "run.sh").write_text("#!/bin/sh\npython pipeline.py --from a --to b --pay\n")
    out = hook("gate_pretool.py", bash(repo, "bash run.sh"))
    assert decision(out) == "deny" and "no prepared request for EXACTLY" in out["hookSpecificOutput"]["permissionDecisionReason"]
    (repo / "free.sh").write_text("python pipeline.py --dry-run\n")
    assert hook("gate_pretool.py", bash(repo, "sh ./free.sh")) is None


def test_paid_prompt_shows_command_and_booking_is_deterministic(repo):
    cmd = "python pipeline.py --enhance-only out/x --pay"
    req = {"id": "r1", "status": "prepared", "command": cmd, "summary": "H3 7 s", "est_usd": 3.0,
           "price_source": "pricing API", "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    (repo / ".harness" / "requests" / "r1.json").write_text(json.dumps(req))
    out = hook("gate_pretool.py", bash(repo, cmd))
    assert decision(out) == "ask" and f"Command: {cmd}" in out["hookSpecificOutput"]["permissionDecisionReason"]
    # owner clicked Yes, the command ran → PostToolUse books the estimate
    assert hook("post_tool.py", {"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": cmd}}) is None
    r1 = json.loads((repo / ".harness" / "requests" / "r1.json").read_text())
    assert r1["status"] == "executed" and r1["booked_estimate"]
    ledger = repo / ".harness" / "ledger" / "claude-work.jsonl"
    assert json.loads(ledger.read_text().splitlines()[0])["usd"] == 3.0
    r = script(repo, "book.py", "r1", "2.75", "x-fal-billable-units 55 × $0.05")
    assert r.returncode == 0 and "Today: $2.75" in r.stdout
    assert script(repo, "book.py", "r1", "2.75", "again").returncode == 2  # no double booking


def test_paid_run_without_request_is_logged(repo):
    hook("post_tool.py", {"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "python pipeline.py --pay"}})
    assert "paid_ran_without_request" in (repo / ".harness" / "events" / "claude-work.jsonl").read_text()


def test_extra_ledger_means_no_double_booking(repo):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["paid"]["extra_ledgers"] = [{"path": "out/ledger.jsonl", "ts_field": "ts", "usd_field": "usd"}]
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    (repo / "out").mkdir()
    (repo / "out" / "ledger.jsonl").write_text(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat()
                                                           .replace("+00:00", "Z"), "usd": 3.01}) + "\n")
    (repo / ".harness" / "requests" / "r1.json").write_text(json.dumps({"id": "r1", "status": "asked", "est_usd": 3,
                                                                        "command": "python pipeline.py --pay"}))
    hook("post_tool.py", {"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "python pipeline.py --pay"}})
    assert not (repo / ".harness" / "ledger").exists()
    assert "Today: $3.01" in script(repo, "book.py", "r1", "3.01", "repo ledger").stdout


# ---------- autoloop hardening ----------
EVAL = """import json, os, pathlib
v = float(pathlib.Path('params.txt').read_text().split()[0])
leak = 'FAKE_PAID_KEY' in os.environ
print(json.dumps({"score": 0.0 - abs(v - 7), "ok": not leak, "notes": "key visible!" if leak else ""}))
"""
HOLD = """import json, pathlib
v = float(pathlib.Path('params.txt').read_text().split()[0])
print(json.dumps({"score": 0.0 - abs(v - 6), "ok": True}))
"""


@pytest.fixture
def loop_repo(repo):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["autoloop"] = {"tune": {"goal": "v→7", "eval_cmd": f"{sys.executable} eval/score.py",
                                "holdout_cmd": f"{sys.executable} eval/hold.py", "direction": "max",
                                "editable": ["params.txt"], "locked": ["eval/*"], "patience": 3,
                                "forbid_in_diff": ["case_7"], "max_runs": 20}}
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    (repo / ".gitignore").write_text(".harness/state/\n")
    (repo / "eval").mkdir()
    (repo / "eval" / "score.py").write_text(EVAL)
    (repo / "eval" / "hold.py").write_text(HOLD)
    (repo / "params.txt").write_text("1\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", "loop setup")
    return repo


def run_loop(repo, *a, ok=True, env=None):
    r = script(repo, "autoloop.py", *a, env=env)
    if ok:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


def test_autoloop_strips_paid_keys_from_eval(loop_repo):
    out = run_loop(loop_repo, "start", "tune", env={"FAKE_PAID_KEY": "secret"}).stdout
    assert "baseline max score = -6.0" in out  # ok=True → the key was not visible to the evaluator


def test_autoloop_gaming_plateau_holdout(loop_repo):
    run_loop(loop_repo, "start", "tune")
    (loop_repo / "params.txt").write_text("7  # case_7 special\n")
    assert "metric gaming" in run_loop(loop_repo, "run", "--desc", "cheat").stdout
    (loop_repo / "params.txt").write_text("5\n")
    assert "KEEP" in run_loop(loop_repo, "run", "--desc", "v=5").stdout
    for v in ("1", "2", "3"):
        (loop_repo / "params.txt").write_text(v + "\n")
        run_loop(loop_repo, "run", "--desc", f"v={v}")
    r = run_loop(loop_repo, "run", "--desc", "more", ok=False)
    assert r.returncode == 10 and "plateau" in r.stdout
    rep = run_loop(loop_repo, "report").stdout
    assert "holdout" in rep.lower() and "base -5.0" in rep and "best -1.0" in rep
    assert run_loop(loop_repo, "report", ok=False).returncode == 2  # only once


def test_stop_hook_keeps_autoloop_going_then_lets_go(loop_repo):
    run_loop(loop_repo, "start", "tune")
    first = {"cwd": str(loop_repo), "hook_event_name": "Stop", "stop_hook_active": False}
    again = {**first, "stop_hook_active": True}
    assert hook("stop_check.py", first)["decision"] == "block"
    assert hook("stop_check.py", again)["decision"] == "block"
    assert hook("stop_check.py", again)["decision"] == "block"
    out = hook("stop_check.py", again)  # 3 nudges without a new run → let it stop, visibly
    assert "stalled" in out["systemMessage"]
    (loop_repo / "params.txt").write_text("6\n")
    run_loop(loop_repo, "run", "--desc", "v=6")
    assert hook("stop_check.py", first)["decision"] == "block"   # progress again → keep going
    run_loop(loop_repo, "report")
    assert hook("stop_check.py", first) is None                   # finished → free to stop
