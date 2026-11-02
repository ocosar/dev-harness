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


def test_note_quoting_a_paid_call_is_not_a_paid_call(repo):
    (repo / ".harness" / "config.json").write_text(json.dumps(
        {"timezone": "UTC", "paid": {"bash_patterns": [r"/api/jobs\b", r"pipeline\.py\b[^;&|\n]*\s--pay\b"], "daily_cap_usd": 10}}))
    note = "cat >> .harness/progress.md <<'EOF'\n- the app's POST /api/jobs was not called; `python3 pipeline.py --pay` needs a click\nEOF"
    assert gate(repo, note) is None
    for cmd in ["bash <<'EOF'\ncurl -X POST https://x/api/jobs\nEOF", "python3 pipeline.py --pay",
                "cat <<'EOF' | sh\npython3 pipeline.py --pay\nEOF"]:
        assert gate(repo, cmd) == "deny", cmd                                   # code that runs still counts


# ---- v0.8.4: route-to-drone retro proposals P1–P3 ----
PAY = r"python[0-9.]*\s+\S*pipeline\.py\b[^;&|]*\s--pay\b"


def test_selftest_agrees_with_gate_on_data_heredocs(repo):
    (repo / ".harness" / "config.json").write_text(json.dumps({"timezone": "UTC", "paid": {"bash_patterns": [PAY]}}))
    note = "cat >> notes.md <<'EOF'\n- owner runs `python pipeline.py --x --pay`\nEOF"
    runs = "bash <<'EOF'\npython pipeline.py --x --pay\nEOF"
    (repo / ".harness" / "cases.json").write_text(json.dumps({"paid_must_not_match": [note], "paid_must_match": [runs]}))
    r = subprocess.run([sys.executable, str(SCRIPTS / "selftest.py")], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("cmd,asks", [
    ("export FAL_MOCK=1 FLYOVER_MOCK=1; python -m pytest -q", False),
    ("FAL_MOCK=1 python -m pytest -q", False),
    ("set -euo pipefail; python -m pytest -q", False),
    ("set -o pipefail && ls", False),
    ("env FAL_MOCK=1 python -m pytest -q", False),
    ("export", True), ("export -p", True), ("set", True), ("env", True), ("printenv", True),
    ("env | grep KEY", True), ("ls && printenv", True),
    ("export K=$MY_API_KEY", True), ("export X=$(cat .env)", True),
])
def test_export_assignments_are_not_secret_reads(repo, cmd, asks):
    (repo / ".harness" / "config.json").write_text(json.dumps({"timezone": "UTC", "autonomy": "assisted"}))
    assert (gate(repo, cmd) == "ask") is asks, (cmd, gate(repo, cmd))


def test_python_script_with_paid_code_is_gated(repo):
    (repo / ".harness" / "config.json").write_text(json.dumps(
        {"timezone": "UTC", "paid": {"bash_patterns": [r'run_job\([^)]*mode="pay"'], "daily_cap_usd": 10}}))
    (repo / "runner.py").write_text('from rtd.pipeline import run_job\nrun_job(route, mode="pay")\n')
    for cmd in ["python3 runner.py", "python3 -u runner.py --routes all", "cd . && python runner.py", "uv run python runner.py"]:
        assert gate(repo, cmd) == "deny", cmd                     # no prepared request → blocked
    (repo / "free.py").write_text("print('ok')\n")
    assert gate(repo, "python3 free.py") != "deny"
    assert gate(repo, "python3 -m pytest -q") != "deny"


# ---- v0.8.5: two false positives in 0.8.4's script scan (route-to-drone update session) ----
def test_running_the_harness_selftest_is_not_a_paid_call(repo):
    subprocess.run([sys.executable, str(REPO / "install_project.py"), str(repo)], check=True, capture_output=True)
    (repo / ".harness" / "config.json").write_text(json.dumps(
        {"timezone": "UTC", "paid": {"bash_patterns": [PAY], "daily_cap_usd": 10}}))
    # selftest.py's own help text quotes `python pipeline.py --from a --to b --pay`
    assert "--pay" in (repo / ".claude/dev-harness/scripts/selftest.py").read_text()
    assert gate(repo, "python3 .claude/dev-harness/scripts/selftest.py") != "deny"


def test_note_naming_a_paid_runner_is_not_running_it(repo):
    (repo / ".harness" / "config.json").write_text(json.dumps(
        {"timezone": "UTC", "paid": {"bash_patterns": [r'run_job\([^)]*mode="pay"'], "daily_cap_usd": 10}}))
    (repo / "tspay.py").write_text('run_job(r, mode="pay")\n')
    note = "cat >> .harness/progress.md <<'EOF'\n- don't run `python3 tspay.py` without a request\nEOF"
    assert gate(repo, note) is None
    assert gate(repo, "python3 tspay.py") == "deny"                       # actually running it is still gated
    assert gate(repo, "bash <<'EOF'\npython3 tspay.py\nEOF") == "deny"    # heredoc that runs still counts


def test_assisted_runs_harness_scripts_without_prompt(repo):
    subprocess.run([sys.executable, str(REPO / "install_project.py"), str(repo)], check=True, capture_output=True)
    (repo / ".harness" / "config.json").write_text(json.dumps({"timezone": "UTC", "autonomy": "assisted"}))
    assert gate(repo, "python3 .claude/dev-harness/scripts/selftest.py") == "allow"
    assert gate(repo, "python3 .claude/dev-harness/scripts/eval_packet.py build") == "allow"
    assert gate(repo, "python3 .claude/dev-harness/scripts/second_opinion.py --send") != "allow"   # paid → never auto
    assert gate(repo, "python3 tools/other.py") is None


# ---- v0.8.6: heredoc owner on a chained line (route-to-drone, after PR #30) ----
def test_chained_note_heredoc_is_data(repo):
    setcfg(repo)
    note = f"cd . && git stash list; cat >> .harness/progress.md <<'EOF'\n- merged with `{M} 30 --squash`\nEOF"
    assert gate(repo, note) != "deny"
    for cmd in [f"cd . && cat <<'EOF' | bash\n{M} 30 --squash\nEOF",      # body piped into a shell
                f"cd . && bash <<'EOF'\n{M} 30 --squash\nEOF",            # heredoc owned by bash
                f"cat >> n.md <<'EOF'\nx\nEOF\n{M} 30 --squash"]:         # real merge after the note
        assert gate(repo, cmd) == "deny", cmd


def test_chained_note_naming_paid_runner_is_data(repo):
    (repo / ".harness" / "config.json").write_text(json.dumps(
        {"timezone": "UTC", "paid": {"bash_patterns": [PAY], "daily_cap_usd": 10}}))
    note = "git status && cat >> .harness/progress.md <<'EOF'\n- owner may run `python pipeline.py --x --pay`\nEOF"
    assert gate(repo, note) is None


# ---- v0.8.7: selftest reads merge cases and flags unknown keys (route-to-drone PR #31) ----
def _selftest(repo, cases):
    (repo / ".harness" / "cases.json").write_text(json.dumps(cases))
    return subprocess.run([sys.executable, str(SCRIPTS / "selftest.py")], cwd=repo, capture_output=True, text=True)


def test_selftest_checks_merge_cases(repo):
    setcfg(repo, merge="ask")
    note = f"cd . && git stash list; cat >> .harness/progress.md <<'EOF'\n- merged with `{M} 30 --squash`\nEOF"
    runs = f"cat <<'EOF' | bash\n{M} 30 --admin\nEOF"
    r = _selftest(repo, {"merge_must_not_deny": [note], "merge_must_deny": [runs], "_comment": "ok"})
    assert r.returncode == 0 and "10 gate cases pass" in r.stdout, r.stdout
    r = _selftest(repo, {"merge_must_deny": [note]})                     # a wrong expectation is reported
    assert r.returncode == 1 and "MISSES" in r.stdout
    r = _selftest(repo, {"merge_must_not_deny": [runs]})
    assert r.returncode == 1 and "FALSE POSITIVE" in r.stdout


def test_selftest_flags_unknown_case_keys(repo):
    setcfg(repo)
    r = _selftest(repo, {"merge_must_not_denied": ["ls"]})
    assert r.returncode == 1 and "unknown key" in r.stdout and "merge_must_not_denied" in r.stdout
