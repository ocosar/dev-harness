"""v0.8.0: start-up step per fresh machine (.harness/init.sh) and the leave-it-clean reminder at stop."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "scripts"


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def run(script, payload, cwd, env=None):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    e.pop("CLAUDE_CODE_REMOTE", None)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPTS / script)], input=json.dumps(payload), capture_output=True,
                       text=True, env=e, cwd=cwd, timeout=120)
    return json.loads(r.stdout) if r.stdout.strip() else None


@pytest.fixture
def repo(tmp_path):
    g(tmp_path, "init", "-q", "-b", "main")
    g(tmp_path, "config", "user.email", "t@t")
    g(tmp_path, "config", "user.name", "t")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.json").write_text(json.dumps({"timezone": "UTC"}))
    (tmp_path / ".gitignore").write_text(".harness/state/\n")
    (tmp_path / ".harness" / "check.sh").write_text("test -f tools_ready || { echo 'pytest missing'; exit 1; }\n")
    (tmp_path / ".harness" / "init.sh").write_text("touch tools_ready\necho installed\n")
    (tmp_path / ".gitignore").write_text(".harness/state/\ntools_ready\n")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-qm", "base")
    g(tmp_path, "checkout", "-qb", "claude/x")
    return tmp_path


def stop(repo, active=False):
    return run("stop_check.py", {"cwd": str(repo), "hook_event_name": "Stop", "stop_hook_active": active}, repo)


def test_fresh_machine_runs_init_before_the_first_check(repo):
    brief = run("session_context.py", {"cwd": str(repo), "source": "startup"}, repo)
    assert "bash .harness/init.sh" in json.dumps(brief)
    assert stop(repo) is None                          # init installed the tools, so the check passed first time
    assert (repo / "tools_ready").exists()
    st = json.loads((repo / ".harness/state/init.json").read_text())
    assert st["machine"] and st["sha"]
    (repo / "tools_ready").unlink()                    # same machine, init already done → not re-run
    (repo / "x.py").write_text("1\n"); g(repo, "add", "x.py"); g(repo, "commit", "-qm", "x")
    out = stop(repo)
    assert out and "pytest missing" in out["reason"]


def test_init_reruns_when_script_changes(repo):
    stop(repo)
    (repo / ".harness" / "init.sh").write_text("touch tools_ready\necho v2\n")
    brief = run("session_context.py", {"cwd": str(repo), "source": "startup"}, repo)
    assert "bash .harness/init.sh" in json.dumps(brief)


def test_clean_state_reminds_once_then_allows(repo):
    stop(repo)                                         # init + green check on a clean tree
    (repo / "feature.py").write_text("x = 1\n")
    out = stop(repo)
    assert out and out["decision"] == "block" and "feature.py" in out["reason"] and "progress.md" in out["reason"]
    assert stop(repo, active=True) is None             # second stop in the same sequence is allowed


def test_bookkeeping_alone_is_not_dirty(repo):
    stop(repo)
    (repo / ".harness" / "events").mkdir(parents=True, exist_ok=True)
    (repo / ".harness" / "events" / "claude-x.jsonl").write_text('{"kind": "x"}\n')
    assert stop(repo) is None


def test_unpushed_commits_get_a_push_reminder(repo, tmp_path_factory):
    remote = tmp_path_factory.mktemp("remote")
    g(remote, "init", "-q", "--bare")
    g(repo, "remote", "add", "origin", str(remote))
    g(repo, "push", "-q", "-u", "origin", "claude/x")
    stop(repo)
    (repo / "f.py").write_text("1\n"); g(repo, "add", "f.py"); g(repo, "commit", "-qm", "f")
    out = stop(repo)
    assert out and "not pushed" in out["reason"]


def test_no_reminder_on_protected_branch_or_when_disabled(repo):
    g(repo, "checkout", "-q", "main")
    stop(repo)
    (repo / "y.py").write_text("1\n")
    assert stop(repo) is None
    g(repo, "checkout", "-q", "claude/x")
    cfg = json.loads((repo / ".harness/config.json").read_text()); cfg["clean_state"] = False
    (repo / ".harness/config.json").write_text(json.dumps(cfg))
    assert stop(repo) is None


def test_assisted_runs_owner_scripts_without_prompt(repo):
    cfg = json.loads((repo / ".harness/config.json").read_text()); cfg["autonomy"] = "assisted"
    (repo / ".harness/config.json").write_text(json.dumps(cfg))
    for cmd in ["bash .harness/init.sh", "bash .harness/check.sh"]:
        out = run("gate_pretool.py", {"cwd": str(repo), "tool_name": "Bash", "tool_use_id": "t",
                                      "tool_input": {"command": cmd}}, repo)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", cmd
    out = run("gate_pretool.py", {"cwd": str(repo), "tool_name": "Write", "tool_use_id": "t",
                                  "tool_input": {"file_path": str(repo / ".harness/init.sh"), "content": "x"}}, repo)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"   # changing init.sh needs the owner


def test_cloud_sessions_leave_the_reminder_to_claude_code(repo):
    run("stop_check.py", {"cwd": str(repo), "hook_event_name": "Stop"}, repo)
    (repo / "z.py").write_text("1\n")
    assert run("stop_check.py", {"cwd": str(repo), "hook_event_name": "Stop"}, repo, env={"CLAUDE_CODE_REMOTE": "true"}) is None
    cfg = json.loads((repo / ".harness/config.json").read_text()); cfg["clean_state"] = "always"
    (repo / ".harness/config.json").write_text(json.dumps(cfg))
    out = run("stop_check.py", {"cwd": str(repo), "hook_event_name": "Stop"}, repo, env={"CLAUDE_CODE_REMOTE": "true"})
    assert out and "z.py" in out["reason"]
