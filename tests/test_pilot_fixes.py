"""v0.6.5: fixes from the route-to-drone update session (selftest vs merge modes, heredoc false positive, old names)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins" / "dev-harness" / "scripts"
M = "gh pr " + "merge"


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    g(tmp_path, "init", "-q", "-b", "main")
    g(tmp_path, "config", "user.email", "t@t")
    g(tmp_path, "config", "user.name", "t")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "cases.json").write_text("{}")
    (tmp_path / "a").write_text("a")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-qm", "base")
    g(tmp_path, "checkout", "-qb", "claude/x")
    return tmp_path


def setcfg(repo, **kw):
    (repo / ".harness" / "config.json").write_text(json.dumps({"timezone": "UTC", **kw}))


@pytest.mark.parametrize("kw", [{}, {"merge": "ask"}, {"merge": "auto", "autonomy": "autopilot"},
                                {"merge": "auto", "autonomy": "autopilot", "deploys_on_merge": True}])
def test_selftest_passes_in_every_merge_mode(repo, kw):
    setcfg(repo, **kw)
    r = subprocess.run([sys.executable, str(SCRIPTS / "selftest.py")], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def gate(repo, cmd):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    r = subprocess.run([sys.executable, str(SCRIPTS / "gate_pretool.py")], capture_output=True, text=True, env=e,
                       input=json.dumps({"cwd": str(repo), "tool_name": "Bash", "tool_use_id": "t",
                                         "tool_input": {"command": cmd}}))
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None


def test_heredoc_data_mentioning_merge_is_not_a_merge(repo):
    setcfg(repo)
    note = f"cat >> notes.md <<'EOF'\n- merged with `{M} 24 --squash`\nEOF"
    assert gate(repo, note) != "deny"
    tee = f"tee notes.md <<EOF\n{M} 24 --squash\nEOF"
    assert gate(repo, tee) != "deny"


def test_heredoc_that_runs_still_counts(repo):
    setcfg(repo)
    for cmd in [f"cat <<'EOF' | bash\n{M} 24 --squash\nEOF", f"bash <<'EOF'\n{M} 24 --squash\nEOF",
                f"{M} 24 --squash"]:
        assert gate(repo, cmd) == "deny", cmd


def test_installer_renames_old_command_names(repo):
    setcfg(repo)
    (repo / ".harness" / "paid_guard.py").write_text('MSG = "run the /dh-paid-call procedure"\n')
    (repo / ".harness" / "check.sh").write_text("#!/bin/sh\n# /dh-init fills this in\nexit 0\n")
    (repo / "CLAUDE.md").write_text("Keep /dh-old in my own notes.\n<!-- dev-harness:start -->\nUse /dh-ship and "
                                    "/dev-harness:retro.\n<!-- dev-harness:end -->\n")
    subprocess.run([sys.executable, str(REPO / "install_project.py"), str(repo)], check=True, capture_output=True)
    assert "/dev-harness-paid-call" in (repo / ".harness/paid_guard.py").read_text()
    assert "/dev-harness-init" in (repo / ".harness/check.sh").read_text()
    md = (repo / "CLAUDE.md").read_text()
    assert "Keep /dh-old in my own notes." in md                     # outside the managed block: untouched
    assert "/dev-harness-ship" in md and "/dev-harness-retro" in md and "dev-harness:start" in md


def test_upgrade_from_older_copy_leaves_protected_files_alone(repo):
    """An installed copy older than 0.6.5 would revert renames in .harness/paid_guard.py / check.sh — skip them."""
    setcfg(repo)
    (repo / ".claude" / "dev-harness").mkdir(parents=True)
    (repo / ".claude" / "dev-harness" / "VERSION").write_text("dev-harness 0.6.2 (1fc62ac)\n")
    (repo / ".harness" / "paid_guard.py").write_text('MSG = "run the /dh-paid-call procedure"\n')
    subprocess.run([sys.executable, str(REPO / "install_project.py"), str(repo)], check=True, capture_output=True)
    assert "/dh-paid-call" in (repo / ".harness/paid_guard.py").read_text()
