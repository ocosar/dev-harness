"""Gate behaviour tests: run the hook scripts exactly as Claude Code does (JSON on stdin, JSON on stdout)."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "scripts"
PAY_RE = r"python[0-9.]*\s+\S*pipeline\.py\b[^;&|]*\s--pay\b"


def run(script, payload, env=None):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPTS / script)], input=json.dumps(payload),
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def decision(out):
    return None if out is None else out["hookSpecificOutput"]["permissionDecision"]


def no_clean_reminder(repo):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["clean_state"] = False
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))


@pytest.fixture
def repo(tmp_path):
    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (tmp_path / ".harness" / "requests").mkdir(parents=True)
    cfg = {"paid": {"bash_patterns": [PAY_RE], "mcp_tools": ["mcp__fal__(run_model|submit_job)"],
                    "daily_cap_usd": 10,
                    "extra_ledgers": [{"path": "out/ledger.jsonl", "ts_field": "ts", "usd_field": "usd"}]},
           "check": {"command": "bash .harness/check.sh", "timeout_s": 30, "max_consecutive_blocks": 2}}
    (tmp_path / ".harness" / "config.json").write_text(json.dumps(cfg))
    (tmp_path / "README.md").write_text("x")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    g("checkout", "-q", "-b", "claude/work")
    return tmp_path


def bash(repo, cmd, **extra):
    return {"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": cmd}, "permission_mode": "default", **extra}


def prepare(repo, cmd="python pipeline.py --pay", est=3.0, rid="r1", age_h=0):
    p = repo / ".harness" / "requests" / f"{rid}.json"
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_h)).isoformat()
    p.write_text(json.dumps({"id": rid, "status": "prepared", "command": cmd, "summary": "H3 run", "est_usd": est,
                             "price_source": "fal pricing API", "created_at": created}))
    return p


# ---- inert outside harness repos
def test_noop_without_config(tmp_path):
    assert run("gate_pretool.py", bash(tmp_path, "python pipeline.py --pay")) is None


# ---- Gate 2
def test_harmless_commands_pass(repo):
    for cmd in ["ls -la", "grep -n -- --pay pipeline.py", "python pipeline.py --dry-run", "cat docs/PLAN.md"]:
        assert run("gate_pretool.py", bash(repo, cmd)) is None, cmd


def test_paid_without_request_denied(repo):
    out = run("gate_pretool.py", bash(repo, "python pipeline.py --from a --to b --pay"))
    assert decision(out) == "deny" and "no prepared request for EXACTLY" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_paid_with_request_asks_once(repo):
    cmd = "python3 pipeline.py --enhance-only out/x --pay"
    p = prepare(repo, cmd)
    out = run("gate_pretool.py", bash(repo, cmd))
    assert decision(out) == "ask"
    assert "$3.00" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert json.loads(p.read_text())["status"] == "asked"
    # same request cannot be reused for a second call
    assert decision(run("gate_pretool.py", bash(repo, cmd))) == "deny"


def test_request_must_name_the_exact_command(repo):
    prepare(repo, "python pipeline.py --duration 5 --pay", est=0.7)
    out = run("gate_pretool.py", bash(repo, "python pipeline.py --duration 15 --tier pro --pay"))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision(out) == "deny" and "--duration 5" in reason  # shows what WAS prepared


def test_stale_zero_and_cap_zero_requests_denied(repo):
    prepare(repo, age_h=30)
    assert decision(run("gate_pretool.py", bash(repo, "python pipeline.py --pay"))) == "deny"
    prepare(repo, est=0, rid="r2")
    assert "positive est_usd" in run("gate_pretool.py", bash(repo, "python pipeline.py --pay"))["hookSpecificOutput"]["permissionDecisionReason"]
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["paid"]["daily_cap_usd"] = 0
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    prepare(repo, rid="r3")
    assert "cap is 0" in run("gate_pretool.py", bash(repo, "python pipeline.py --pay"))["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", ["for i in 1 2 3; do python pipeline.py --pay; done",
                                 "python pipeline.py --a --pay && python pipeline.py --b --pay"])
def test_several_paid_calls_per_command_denied(repo, cmd):
    prepare(repo, cmd)
    out = run("gate_pretool.py", bash(repo, cmd))
    assert decision(out) == "deny" and "ONE paid call" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_cap_counts_repo_ledger(repo):
    (repo / "out").mkdir()
    now = dt.datetime.now().astimezone().isoformat()
    (repo / "out" / "ledger.jsonl").write_text(json.dumps({"ts": now, "usd": 8.5}) + "\n")
    prepare(repo, est=3.0)
    out = run("gate_pretool.py", bash(repo, "python pipeline.py --pay"))
    assert decision(out) == "deny" and "daily cap" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_unattended_never_pays(repo):
    p = prepare(repo)
    out = run("gate_pretool.py", bash(repo, "python pipeline.py --pay"), env={"HARNESS_UNATTENDED": "1"})
    assert decision(out) == "deny"
    assert json.loads(p.read_text())["status"] == "awaiting_approval"


def test_bypass_mode_treated_as_unattended(repo):
    prepare(repo)
    out = run("gate_pretool.py", bash(repo, "python pipeline.py --pay", permission_mode="bypassPermissions"))
    assert decision(out) == "deny"


def test_paid_mcp_tool(repo):
    payload = {"cwd": str(repo), "tool_name": "mcp__fal__submit_job", "tool_input": {"endpoint_id": "x"}}
    assert decision(run("gate_pretool.py", payload)) == "deny"
    prepare(repo, "mcp__fal__submit_job")
    assert decision(run("gate_pretool.py", payload)) == "ask"
    free = {"cwd": str(repo), "tool_name": "mcp__fal__get_pricing", "tool_input": {}}
    assert run("gate_pretool.py", free) is None


# ---- Gate 3
@pytest.mark.parametrize("cmd", ["gh pr merge 12 --squash", "git push origin main", "git push origin HEAD:main",
                                 "git push --force origin master", "git push --all"])
def test_merge_and_protected_push_denied(repo, cmd):
    assert decision(run("gate_pretool.py", bash(repo, cmd))) == "deny"


def test_feature_branch_push_allowed(repo):
    assert run("gate_pretool.py", bash(repo, "git push -u origin claude/work")) is None
    assert run("gate_pretool.py", bash(repo, "git commit -m wip")) is None


def test_commit_on_main_denied(repo):
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    assert decision(run("gate_pretool.py", bash(repo, "git commit -m x"))) == "deny"
    assert decision(run("gate_pretool.py", bash(repo, "git push"))) == "deny"


# ---- Gate 1 + config guard
def test_spec_approval_needs_click(repo):
    f = repo / ".harness" / "features.json"
    f.write_text('{"approved": false, "features": []}')
    edit = {"cwd": str(repo), "tool_name": "Edit",
            "tool_input": {"file_path": str(f), "old_string": '"approved": false', "new_string": '"approved": true'}}
    assert decision(run("gate_pretool.py", edit)) == "ask"
    assert decision(run("gate_pretool.py", edit, env={"HARNESS_UNATTENDED": "1"})) == "deny"
    f.write_text('{"approved": true, "features": [{"id": "F1", "passes": false}]}')
    progress = {"cwd": str(repo), "tool_name": "Write",
                "tool_input": {"file_path": str(f), "content": '{"approved": true, "features": [{"id": "F1", "passes": true}]}'}}
    assert run("gate_pretool.py", progress) is None


def test_config_edit_needs_click(repo):
    w = {"cwd": str(repo), "tool_name": "Write",
         "tool_input": {"file_path": ".harness/config.json", "content": "{}"}}
    assert decision(run("gate_pretool.py", w)) == "ask"


# ---- Stop hook
def test_stop_blocks_on_failing_check_then_gives_up(repo):
    """Mirrors Claude Code: the stop right after a block arrives with stop_hook_active=true."""
    no_clean_reminder(repo)
    (repo / ".harness" / "check.sh").write_text("echo boom; exit 1\n")
    (repo / "x.py").write_text("print(1)\n")
    first = {"cwd": str(repo), "hook_event_name": "Stop", "stop_hook_active": False}
    again = {**first, "stop_hook_active": True}
    assert run("stop_check.py", first)["decision"] == "block"      # attempt 1
    assert run("stop_check.py", again)["decision"] == "block"      # attempt 2 (continuation)
    out = run("stop_check.py", again)                               # 3rd > max 2 → stop, visibly
    assert "decision" not in out and "still fails" in out["systemMessage"]
    assert run("stop_check.py", first) is None                     # next turn, same code: no nagging
    (repo / "x.py").write_text("print(2)\n")                       # code changed → checked again
    assert run("stop_check.py", first)["decision"] == "block"


def test_stop_passes_and_caches(repo):
    no_clean_reminder(repo)
    marker = repo.parent / f"{repo.name}-ran.txt"  # outside the repo, so it doesn't change the fingerprint
    (repo / ".harness" / "check.sh").write_text(f"echo run >> {marker}; exit 0\n")
    stop = {"cwd": str(repo), "hook_event_name": "Stop"}
    assert run("stop_check.py", stop) is None
    assert run("stop_check.py", stop) is None
    assert marker.read_text().count("run") == 1  # second stop with no code change did not re-run


# ---- SessionStart
def test_session_context_briefing(repo):
    (repo / ".harness" / "features.json").write_text(json.dumps(
        {"spec": "docs/specs/x/spec.md", "approved": True,
         "features": [{"id": "F1", "title": "a", "passes": True}, {"id": "F2", "title": "b", "paid": True}]}))
    (repo / ".harness" / "progress.md").write_text("## 2026-10-22 — F1\n- Done: a\n")
    out = run("session_context.py", {"cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "1/2" in ctx and "F2" in ctx and "(PAID)" in ctx and "Spend today" in ctx
