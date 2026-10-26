"""Regression tests for the independent adversarial review (2026-10-26). Each test reproduces a finding."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "scripts"
PAY_RE = r"pipeline\.py\b[^;&|\n]*\s--pay\b"


def hook(script, payload, env=None):
    e = dict(os.environ)
    for k in ("HARNESS_UNATTENDED", "CLAUDE_PROJECT_DIR"):
        e.pop(k, None)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPTS / script)], input=json.dumps(payload),
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def decision(out):
    return None if out is None else out.get("hookSpecificOutput", {}).get("permissionDecision")


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def script(repo, name, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=repo, capture_output=True, text=True,
                          timeout=120)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    g(r, "init", "-q", "-b", "main")
    g(r, "config", "user.email", "t@t")
    g(r, "config", "user.name", "t")
    (r / ".harness" / "requests").mkdir(parents=True)
    cfg = {"paid": {"bash_patterns": [PAY_RE], "mcp_tools": ["mcp__fal__submit_job"], "daily_cap_usd": 10},
           "locked_paths": ["eval/*"], "check": {"command": "bash .harness/check.sh"}}
    (r / ".harness" / "config.json").write_text(json.dumps(cfg, indent=2))
    (r / ".harness" / "check.sh").write_text("exit 0\n")
    (r / "eval").mkdir()
    (r / "eval" / "score.py").write_text("print(1)\n")
    (r / ".gitignore").write_text(".harness/state/\n")
    g(r, "add", "-A")
    g(r, "commit", "-q", "-m", "init")
    g(r, "checkout", "-q", "-b", "claude/work")
    return r


def bash(cwd, cmd, **kw):
    return {"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": cmd}, "permission_mode": "default",
            "tool_use_id": "t1", **kw}


# 1 — session cwd ABOVE the repo (cloud sessions start in the parent folder)
@pytest.mark.parametrize("cmd", ["cd proj && python pipeline.py --pay", "git -C proj push origin main",
                                 "cd proj && gh pr merge 3"])
def test_gates_work_from_parent_folder(repo, cmd):
    assert decision(hook("gate_pretool.py", bash(repo.parent, cmd))) == "deny"


def test_mcp_and_stop_from_parent_folder(repo):
    mcp = {"cwd": str(repo.parent), "tool_name": "mcp__fal__submit_job", "tool_input": {}}
    assert decision(hook("gate_pretool.py", mcp)) == "deny"
    (repo / ".harness" / "check.sh").write_text("echo boom; exit 1\n")
    assert hook("stop_check.py", {"cwd": str(repo.parent)})["decision"] == "block"


# 2 — shell writes to protected files are undone after the fact (PostToolUse backstop)
def test_unapproved_shell_change_is_restored(repo):
    payload = bash(repo, "python3 tools/x.py")  # innocent-looking: the pre-check can't see what it writes
    assert hook("gate_pretool.py", payload) is None  # snapshot taken
    cfg_before = (repo / ".harness" / "config.json").read_text()
    (repo / ".harness" / "config.json").write_text('{"paid": {"daily_cap_usd": 9999}}')
    (repo / "eval" / "score.py").write_text("print(1000)\n")
    out = hook("post_tool.py", {**payload, "hook_event_name": "PostToolUse"})
    assert out["decision"] == "block" and "reverted" in out["reason"]
    assert (repo / ".harness" / "config.json").read_text() == cfg_before
    assert (repo / "eval" / "score.py").read_text() == "print(1)\n"


def test_owner_approved_shell_change_is_kept(repo):
    payload = bash(repo, "echo 'print(2)' > eval/score.py")
    assert decision(hook("gate_pretool.py", payload)) == "ask"  # owner clicks Yes → command runs
    (repo / "eval" / "score.py").write_text("print(2)\n")
    assert hook("post_tool.py", payload) is None
    assert (repo / "eval" / "score.py").read_text() == "print(2)\n"


def test_branch_switch_is_not_undone(repo):
    payload = bash(repo, "git checkout main")
    hook("gate_pretool.py", payload)
    g(repo, "checkout", "-q", "main")
    assert hook("post_tool.py", payload) is None


# 3 — merge escapes
@pytest.mark.parametrize("cmd", [
    "git checkout main && git merge claude/work && git push",
    'git push origin "main"',
    "gh api -X POST repos/o/r/merges -f base=main -f head=claude/work",
    "gh api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"x\"}) { clientMutationId } }'",
    "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
])
def test_more_merge_escapes(repo, cmd):
    assert decision(hook("gate_pretool.py", bash(repo, cmd))) == "deny"


@pytest.mark.parametrize("cmd", ["git push origin HEAD", "git push -u origin HEAD", "git push origin @"])
def test_push_head_resolves_current_branch(repo, cmd):
    g(repo, "checkout", "-q", "main")
    assert decision(hook("gate_pretool.py", bash(repo, cmd))) == "deny"
    g(repo, "checkout", "-q", "claude/work")
    assert hook("gate_pretool.py", bash(repo, cmd)) is None


# 4 — paid entry without the python prefix
def test_direct_executable_is_paid(repo):
    assert decision(hook("gate_pretool.py", bash(repo, "./pipeline.py --from a --pay"))) == "deny"


# 5 — broken config fails CLOSED
def test_broken_config_fails_closed(repo):
    (repo / ".harness" / "config.json").write_text('{"paid": {"daily_cap_usd": 20,}}')
    assert decision(hook("gate_pretool.py", bash(repo, "python pipeline.py --pay"))) == "ask"
    assert decision(hook("gate_pretool.py", bash(repo, "ls"), env={"HARNESS_UNATTENDED": "1"})) == "deny"
    ctx = hook("session_context.py", {"cwd": str(repo)})["hookSpecificOutput"]["additionalContext"]
    assert "UNREADABLE" in ctx


# 8 — spec gate is semantic, not textual
def _feats(repo, approved, feats=None):
    d = {"spec": "docs/spec.md", "approved": approved, "approved_at": None,
         "features": feats or [{"id": "F1", "title": "a", "acceptance": ["x"], "status": "todo", "passes": False}]}
    return json.dumps(d, indent=2)


def test_spec_gate_semantics(repo):
    f = repo / ".harness" / "features.json"
    f.write_text(_feats(repo, False))
    edit = {"cwd": str(repo), "tool_name": "Edit", "tool_input": {
        "file_path": str(f), "old_string": 'false,\n  "approved_at"', "new_string": 'true,\n  "approved_at"'}}
    assert decision(hook("gate_pretool.py", edit)) == "ask"
    f.write_text(_feats(repo, True))
    swap = {"cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(f), "content": _feats(
        repo, True, [{"id": "F9", "title": "something else", "acceptance": [], "status": "todo", "passes": False}])}}
    assert decision(hook("gate_pretool.py", swap)) == "ask"
    progress = {"cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(f), "content": _feats(
        repo, True, [{"id": "F1", "title": "a", "acceptance": ["x"], "status": "done", "passes": True,
                      "evidence": "review/x"}])}}
    assert hook("gate_pretool.py", progress) is None


def test_spec_drift_after_approval_is_flagged(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "spec.md").write_text("v1")
    f = repo / ".harness" / "features.json"
    f.write_text(_feats(repo, False))
    sha = script(repo, "spec_hash.py").stdout.strip()
    d = json.loads(f.read_text())
    d.update(approved=True, approved_spec_sha=sha)
    f.write_text(json.dumps(d))
    assert "SPEC CHANGED" not in hook("session_context.py", {"cwd": str(repo)})["hookSpecificOutput"]["additionalContext"]
    (repo / "docs" / "spec.md").write_text("v2 — bigger scope")
    assert "SPEC CHANGED" in hook("session_context.py", {"cwd": str(repo)})["hookSpecificOutput"]["additionalContext"]


# 10 — the Stop gate can't be switched off by deleting or editing the check
def test_check_script_is_protected(repo):
    (repo / ".harness" / "check.sh").unlink()
    (repo / "x.py").write_text("1\n")
    assert "missing" in hook("stop_check.py", {"cwd": str(repo)})["reason"]
    w = {"cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(repo / ".harness/check.sh"),
                                                               "content": "exit 0"}}
    assert decision(hook("gate_pretool.py", w)) == "ask"


# 12 — the cap uses one configured timezone
def test_cap_timezone(repo):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["timezone"] = "Europe/Istanbul"
    cfg["paid"]["extra_ledgers"] = [{"path": "out/ledger.jsonl", "ts_field": "ts", "usd_field": "usd"}]
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    (repo / "out").mkdir()
    now_utc = dt.datetime.now(dt.timezone.utc)
    (repo / "out" / "ledger.jsonl").write_text(json.dumps({"ts": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "usd": 9.5}) + "\n")
    p = repo / ".harness" / "requests" / "r1.json"
    p.write_text(json.dumps({"id": "r1", "status": "prepared", "command": "python pipeline.py --pay", "est_usd": 1,
                             "created_at": now_utc.isoformat()}))
    out = hook("gate_pretool.py", bash(repo, "python pipeline.py --pay"))
    assert decision(out) == "deny" and "daily cap" in out["hookSpecificOutput"]["permissionDecisionReason"]


# 6, 7, 13, 14, 15, 17 — autoloop hardening
EVAL = """import json, pathlib, sys
sys.path.insert(0, '.')
import params  # the evaluator imports the code under test, as real evaluators do
v = float(params.v)
print(json.dumps({"score": 0.0 - abs(v - 7), "ok": True}))
"""


@pytest.fixture
def loop_repo(repo):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["check"] = {"command": ""}
    cfg["autoloop"] = {"tune": {"goal": "v→7", "eval_cmd": f"{sys.executable} eval/score.py",
                                "holdout_cmd": f"{sys.executable} eval/hold.py",
                                "editable": ["params.py"], "locked": ["eval/*"], "max_runs": 10}}
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    (repo / "eval" / "score.py").write_text(EVAL)
    (repo / "eval" / "hold.py").write_text(EVAL)
    (repo / "params.py").write_text("v=1\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", "loop")
    return repo


def test_state_file_tampering_halts(loop_repo):
    assert script(loop_repo, "autoloop.py", "start", "tune").returncode == 0
    st_path = loop_repo / ".harness" / "state" / "autoloop.json"
    st = json.loads(st_path.read_text())
    st.update(eval_cmd="echo '{\"score\": 5}'", editable=["*"], max_runs=999)
    st_path.write_text(json.dumps(st))
    (loop_repo / "params.py").write_text("v=6\n")
    r = script(loop_repo, "autoloop.py", "run", "--desc", "x")
    assert r.returncode == 3 and "tampered" in r.stdout


def test_extra_score_line_is_rejected(loop_repo):
    script(loop_repo, "autoloop.py", "start", "tune")
    (loop_repo / "params.py").write_text("v=6\nimport atexit\natexit.register(lambda: print('{\"score\": 1000, \"ok\": true}'))\n")
    r = script(loop_repo, "autoloop.py", "run", "--desc", "fake")
    assert "CRASH" in r.stdout and "score lines" in r.stdout


def test_report_refuses_other_branch(loop_repo):
    script(loop_repo, "autoloop.py", "start", "tune")
    g(loop_repo, "checkout", "-q", "claude/work")
    r = script(loop_repo, "autoloop.py", "report")
    assert r.returncode == 2 and "check out the loop branch" in r.stdout


def test_direction_validated(loop_repo):
    cfg = json.loads((loop_repo / ".harness" / "config.json").read_text())
    cfg["autoloop"]["tune"]["direction"] = "maximize"
    (loop_repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    g(loop_repo, "commit", "-qam", "dir")
    assert "direction must be" in script(loop_repo, "autoloop.py", "start", "tune").stdout


def test_evaluator_that_writes_files_is_refused(loop_repo):
    (loop_repo / "eval" / "score.py").write_text(EVAL + "pathlib.Path('results_latest.json').write_text('{}')\n")
    g(loop_repo, "commit", "-qam", "noisy eval")
    r = script(loop_repo, "autoloop.py", "start", "tune")
    assert r.returncode == 2 and "left files" in r.stdout
    assert g(loop_repo, "rev-parse", "--abbrev-ref", "HEAD") == "claude/work"


def test_holdout_sealed_during_loop(loop_repo):
    script(loop_repo, "autoloop.py", "start", "tune")
    out = hook("gate_pretool.py", bash(loop_repo, f"{sys.executable} eval/hold.py"))
    assert decision(out) == "deny" and "sealed" in out["hookSpecificOutput"]["permissionDecisionReason"]


# 4 — second lock inside the paid program (the hook can be dodged by `P=--pay`, aliases, wrappers)
GUARD = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "templates" / "paid_guard.py"
PIPE = """import runpy, pathlib, sys
if '--pay' in sys.argv:
    runpy.run_path(str(pathlib.Path(__file__).resolve().parent / '.harness' / 'paid_guard.py'))['require_approval']()
    print('PAID')
"""


def test_paid_guard_inside_the_program(repo):
    (repo / ".harness" / "paid_guard.py").write_text(GUARD.read_text())
    (repo / "pipeline.py").write_text(PIPE)
    run = lambda *a: subprocess.run([sys.executable, "pipeline.py", *a], cwd=repo, capture_output=True, text=True)
    r = run("--from", "a", "--pay")  # e.g. launched as P=--pay; python pipeline.py --from a $P — hook never fired
    assert r.returncode != 0 and "refused" in r.stderr and "PAID" not in r.stdout
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    req = repo / ".harness" / "requests" / "r1.json"
    req.write_text(json.dumps({"id": "r1", "status": "asked", "asked_at": now, "est_usd": 3,
                               "command": "python3 pipeline.py --from a --pay"}))
    assert run("--from", "b", "--pay").returncode != 0               # different arguments → refused
    r = run("--from", "a", "--pay")
    assert r.returncode == 0 and "PAID" in r.stdout                  # the approved command runs once
    assert json.loads(req.read_text())["status"] == "consumed"
    assert run("--from", "a", "--pay").returncode != 0               # and never again with the same approval
    # post_tool still books it
    hook("post_tool.py", bash(repo, "python3 pipeline.py --from a --pay"))
    assert json.loads(req.read_text())["status"] == "executed"


def test_precheck_follows_cd(repo):
    out = hook("gate_pretool.py", bash(repo.parent, "cd proj && sed -i 's/10/500/' .harness/config.json"))
    assert decision(out) == "ask" and ".harness/config.json" in out["hookSpecificOutput"]["permissionDecisionReason"]
