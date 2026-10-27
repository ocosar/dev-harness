"""v0.6.1: one-click harness update; approved directories cover their subtree; change+commit in one command."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins" / "dev-harness" / "scripts"


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def hook(name, repo, cmd, unattended=False):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    if unattended:
        e["HARNESS_UNATTENDED"] = "1"
    r = subprocess.run([sys.executable, str(SCRIPTS / name)], capture_output=True, text=True, env=e,
                       input=json.dumps({"cwd": str(repo), "tool_name": "Bash", "tool_use_id": "u1",
                                         "tool_input": {"command": cmd}, "tool_response": {}}))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def sh(repo, cmd):
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    g(tmp_path, "init", "-q", "-b", "main")
    g(tmp_path, "config", "user.email", "t@t")
    g(tmp_path, "config", "user.name", "t")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.json").write_text(json.dumps({"timezone": "UTC"}))
    subprocess.run([sys.executable, str(REPO / "install_project.py"), str(tmp_path)], check=True, capture_output=True)
    # pretend an older install: stale content in a vendored script and an old dh- skill
    (tmp_path / ".claude" / "dev-harness" / "scripts" / "book.py").write_text("# old\n")
    (tmp_path / ".claude" / "skills" / "dh-old").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "dh-old" / "SKILL.md").write_text("---\nname: dh-old\n---\n")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-qm", "base")
    g(tmp_path, "checkout", "-qb", "claude/upd")
    return tmp_path


def test_installer_asks_once_and_survives(repo):
    cmd = f"python3 {REPO}/install_project.py ."
    out = hook("gate_pretool.py", repo, cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "HARNESS UPDATE" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert sh(repo, cmd).returncode == 0                                  # owner clicked yes
    post = hook("post_tool.py", repo, cmd)
    assert post is None or "reverted" not in json.dumps(post)
    assert (repo / ".claude/dev-harness/scripts/book.py").read_text() != "# old\n"
    assert not (repo / ".claude/skills/dh-old").exists()


def test_installer_into_another_dir_or_chained_is_not_special(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    out = hook("gate_pretool.py", repo, f"python3 {REPO}/install_project.py {other}")
    assert "HARNESS UPDATE" not in json.dumps(out or {})
    out = hook("gate_pretool.py", repo, f"python3 {REPO}/install_project.py . && echo x > .harness/config.json")
    assert "HARNESS UPDATE" not in json.dumps(out)


def test_installer_denied_unattended(repo):
    out = hook("gate_pretool.py", repo, f"python3 {REPO}/install_project.py .", unattended=True)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_approved_directory_covers_subtree(repo):
    cmd = "cp -r /tmp/nothing .claude/dev-harness/"
    out = hook("gate_pretool.py", repo, cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    (repo / ".claude/dev-harness/scripts/book.py").write_text("# owner-approved copy\n")
    hook("post_tool.py", repo, cmd)
    assert (repo / ".claude/dev-harness/scripts/book.py").read_text() == "# owner-approved copy\n"


def test_change_and_commit_in_one_command_is_restored(repo):
    (repo / "tool.py").write_text("open('.harness/config.json','w').write('{\"paid\": {\"daily_cap_usd\": 999}}')\n")
    g(repo, "add", "tool.py")
    g(repo, "commit", "-qm", "tool")
    cmd = "python3 tool.py && git commit -qam sneaky"
    assert hook("gate_pretool.py", repo, cmd) is None                    # not detectable up front
    assert sh(repo, cmd).returncode == 0
    post = hook("post_tool.py", repo, cmd)
    assert "reverted .harness/config.json" in json.dumps(post) and "commit that restore" in json.dumps(post)
    assert "999" not in (repo / ".harness/config.json").read_text()


def test_checkout_of_other_branch_is_not_reverted(repo):
    g(repo, "checkout", "-qb", "claude/other")
    (repo / ".harness/config.json").write_text(json.dumps({"timezone": "Europe/Istanbul"}))
    g(repo, "commit", "-qam", "owner changed tz")
    g(repo, "checkout", "-q", "claude/upd")
    cmd = "git checkout claude/other"
    hook("gate_pretool.py", repo, cmd)
    sh(repo, cmd)
    post = hook("post_tool.py", repo, cmd)
    assert "reverted" not in json.dumps(post or {})
    assert "Istanbul" in (repo / ".harness/config.json").read_text()
