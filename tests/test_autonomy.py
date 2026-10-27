"""v0.6 autonomy levels: manual / assisted / autopilot."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import harness_lib as H  # noqa: E402

PAY = r"pipeline\.py\b[^;&|\n]*\s--pay\b"


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def gate(repo, tool="Bash", cmd=None, ti=None, unattended=False):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    if unattended:
        e["HARNESS_UNATTENDED"] = "1"
    r = subprocess.run([sys.executable, str(SCRIPTS / "gate_pretool.py")], capture_output=True, text=True, env=e,
                       input=json.dumps({"cwd": str(repo), "tool_name": tool, "tool_use_id": "t",
                                         "tool_input": ti or {"command": cmd}}))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None


def cfg(repo, **kw):
    base = {"timezone": "UTC", "paid": {"bash_patterns": [PAY], "daily_cap_usd": 10}}
    base.update(kw)
    (repo / ".harness" / "config.json").write_text(json.dumps(base))


@pytest.fixture
def repo(tmp_path):
    g(tmp_path, "init", "-q", "-b", "main")
    g(tmp_path, "config", "user.email", "t@t")
    g(tmp_path, "config", "user.name", "t")
    (tmp_path / ".harness").mkdir()
    cfg(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-qm", "base")
    g(tmp_path, "checkout", "-qb", "claude/x")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text("# spec\n")
    return tmp_path


# allow-list policy: only clearly-safe, in-repo, non-paid commands are auto-approved in assisted/autopilot.
SAFE = ["python3 -m pytest -q", "ls -la", "git status", "git add -A && git commit -m wip", "pip install -q pytest",
        "git push -u origin HEAD:claude/x", "grep -rn foo .", "mkdir -p build && touch build/x", "ruff check ."]
# secret reads → explicit "ask"; everything else here → no decision (Claude Code's own prompt)
SECRET = ["cat .env", "printenv", "echo $FAL_KEY", "env"]
PROMPT = ["rm -rf build", "sudo apt install x", "curl https://x.y", "git push --force origin claude/x",
          "railway up", "gh secret set X", "git reset --hard HEAD~1", "echo hi > /etc/hosts",
          "gh api -X POST repos/o/r/issues", "gh pr create --fill", "python3 tools/x.py --dry-run",
          "python3 -c 'import os;print(os.environ)'", "cp secrets ~/backup", "git config alias.x '!sh'",
          "node -e 'process.exit(0)'", "sed -i s/a/b/ a.py"]


def test_manual_never_auto_allows(repo):
    for c in SAFE + SECRET + PROMPT:
        assert gate(repo, cmd=c) is None, c
    assert gate(repo, "Edit", ti={"file_path": str(repo / "a.py"), "old_string": "1", "new_string": "2"}) is None


def test_assisted_allows_only_low_risk(repo):
    cfg(repo, autonomy="assisted")
    for c in SAFE:
        assert gate(repo, cmd=c) == "allow", c
    for c in SECRET:
        assert gate(repo, cmd=c) == "ask", c
    for c in PROMPT:
        assert gate(repo, cmd=c) is None, c                               # falls through to Claude Code's prompt
    assert gate(repo, "Edit", ti={"file_path": str(repo / "a.py"), "old_string": "1", "new_string": "2"}) == "allow"
    assert gate(repo, "Write", ti={"file_path": str(repo / ".env"), "content": "X=1"}) is None
    assert gate(repo, "Write", ti={"file_path": "/etc/passwd", "content": "x"}) is None
    # gates still win
    assert gate(repo, cmd="python3 pipeline.py --pay") == "deny"
    assert gate(repo, cmd="git push origin HEAD:" + "main") == "deny"
    assert gate(repo, "Edit", ti={"file_path": str(repo / ".harness/config.json"), "old_string": "a",
                                  "new_string": "b"}) == "ask"


def feats(repo, approved=True, paid=False, budget=0, max_call=0, sha=True):
    d = {"spec": "docs/spec.md", "approved": approved,
         "features": [{"id": "F1", "title": "t", "acceptance": ["x"], "paid": paid, "status": "todo", "passes": False}]}
    if budget:
        d.update(paid_budget_usd=budget, max_call_usd=max_call)
    if approved and sha:
        d["approved_spec_sha"] = H.spec_sha(repo, d)
    return d


def test_autopilot_self_approves_free_spec_only(repo):
    cfg(repo, autonomy="autopilot")
    (repo / ".harness" / "features.json").write_text(json.dumps(feats(repo, approved=False)))
    ti = {"file_path": str(repo / ".harness/features.json"), "content": json.dumps(feats(repo))}
    assert gate(repo, "Write", ti=ti) == "allow"
    paid = feats(repo, paid=True, budget=8, max_call=4)
    (repo / ".harness" / "features.json").write_text(json.dumps({**paid, "approved": False}))
    assert gate(repo, "Write", ti={"file_path": ti["file_path"], "content": json.dumps(paid)}) == "ask"
    cfg(repo, autonomy="assisted")
    (repo / ".harness" / "features.json").write_text(json.dumps(feats(repo, approved=False)))
    assert gate(repo, "Write", ti=ti) == "ask"


def req(repo, rid, cmd, est):
    H.write_json(repo / ".harness" / "requests" / f"{rid}.json",
                 {"status": "prepared", "command": cmd, "est_usd": est, "summary": "s", "price_source": "api",
                  "created_at": H.now_iso()})


def test_autopilot_paid_budget_envelope(repo):
    cfg(repo, autonomy="autopilot")
    (repo / ".harness" / "features.json").write_text(json.dumps(feats(repo, paid=True, budget=8, max_call=4)))
    c1, c2, c3 = "python3 pipeline.py --pay --run a", "python3 pipeline.py --pay --run b", "python3 pipeline.py --pay --run c"
    req(repo, "r1", c1, 3.6)
    assert gate(repo, cmd=c1, unattended=True) == "allow"           # inside budget, even unattended
    req(repo, "r2", c2, 4.5)
    assert gate(repo, cmd=c2) == "ask"                              # above max per call → owner
    req(repo, "r3", c3, 3.6)
    assert gate(repo, cmd=c3) == "allow"                            # 7.2 of 8
    req(repo, "r4", "python3 pipeline.py --pay --run d", 3.6)
    assert gate(repo, cmd="python3 pipeline.py --pay --run d") == "ask"   # 10.8 > 8 → owner
    assert H.envelope_spent(repo, feats(repo, paid=True, budget=8, max_call=4)["approved_spec_sha"]) >= 7.2
    # spec text changed after approval → no envelope
    (repo / "docs" / "spec.md").write_text("# spec v2\n")
    req(repo, "r5", "python3 pipeline.py --pay --run e", 0.1)
    assert gate(repo, cmd="python3 pipeline.py --pay --run e") == "ask"


def test_autopilot_cap_still_wins(repo):
    cfg(repo, autonomy="autopilot", paid={"bash_patterns": [PAY], "daily_cap_usd": 3})
    (repo / ".harness" / "features.json").write_text(json.dumps(feats(repo, paid=True, budget=8, max_call=4)))
    req(repo, "r1", "python3 pipeline.py --pay", 3.6)
    assert gate(repo, cmd="python3 pipeline.py --pay") == "deny"


def test_auto_merge_only_in_autopilot_without_deploy(repo):
    m = "gh pr " + "merge 7 --squash"
    cfg(repo, autonomy="autopilot", merge="auto")
    assert gate(repo, cmd=m) == "allow"
    assert gate(repo, cmd=m, unattended=True) == "allow"
    assert gate(repo, cmd="gh pr " + "merge 7 --admin") == "deny"
    cfg(repo, autonomy="autopilot", merge="auto", deploys_on_merge=True)
    assert gate(repo, cmd=m) == "ask"
    cfg(repo, autonomy="assisted", merge="auto")
    assert gate(repo, cmd=m) == "ask"
    cfg(repo, autonomy="autopilot")
    assert gate(repo, cmd=m) == "deny"                              # merge: owner (default)


# ---- v0.6.3: findings from the independent review of the autonomy/merge/eval changes ----
def test_assisted_does_not_auto_allow_env_dump_oneliner(repo):
    cfg(repo, autonomy="assisted")
    for c in ["python3 -c 'import os;print(os.environ)'", "python3 - <<'PY'\nimport os;print(os.environ)\nPY",
              "bash -c 'printenv'", "ls /proc/1/environ", "cat ~/.aws/credentials", "node -e 'console.log(process.env)'"]:
        assert gate(repo, cmd=c) in (None, "ask"), c                      # never "allow"


def test_assisted_does_not_auto_allow_paid_via_subprocess_or_shortflag(repo):
    cfg(repo, autonomy="assisted", paid={"bash_patterns": [PAY], "daily_cap_usd": 10})
    for c in ["python3 -c \"import subprocess;subprocess.run(['./pipeline.py','--pay'])\"",
              "python3 pipeline.py --pay", "FLAG=--pay python3 pipeline.py $FLAG"]:
        assert gate(repo, cmd=c) != "allow", c


@pytest.mark.parametrize("amend_cmd", ["git commit -qam sneaky --amend",
                                       "git reset --soft HEAD~1 && git commit -qam sneaky"])
def test_history_rewriting_commit_restores_protected(repo, amend_cmd):
    # The config change happens DURING the command (so the pre-hook can't pre-flag it), then the command folds it
    # into HEAD by rewriting history. post_tool must still restore the working tree.
    cfg(repo, autonomy="assisted")
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "clean")                                     # config is committed & clean at pre time
    (repo / "edit.py").write_text("open('.harness/config.json','w').write('{\"paid\": {\"daily_cap_usd\": 999}}')\n")
    g(repo, "add", "edit.py")
    g(repo, "commit", "-qm", "tool")
    cmd = f"python3 edit.py && {amend_cmd}"
    pre = {"cwd": str(repo), "tool_name": "Bash", "tool_use_id": "amd", "tool_input": {"command": cmd}}
    e = dict(os.environ); e.pop("HARNESS_UNATTENDED", None)
    d = subprocess.run([sys.executable, str(SCRIPTS / "gate_pretool.py")], input=json.dumps(pre), capture_output=True, text=True, env=e)
    assert '"permissionDecision": "deny"' not in d.stdout                 # clean at pre time → not pre-flagged
    subprocess.run(cmd, shell=True, cwd=repo, check=True)
    post = subprocess.run([sys.executable, str(SCRIPTS / "post_tool.py")], input=json.dumps(pre), capture_output=True, text=True, env=e)
    assert "999" not in (repo / ".harness" / "config.json").read_text()   # working tree restored
    assert "reverted .harness/config.json" in post.stdout


def test_request_cannot_be_rewound_to_reuse_budget(repo):
    cfg(repo, autonomy="autopilot")
    import harness_lib as H
    rp = repo / ".harness" / "requests" / "r1.json"
    H.write_json(rp, {"status": "executed", "command": "python3 pipeline.py --pay", "est_usd": 4.0})
    ti = {"file_path": str(rp), "content": json.dumps({"status": "prepared", "command": "python3 pipeline.py --pay", "est_usd": 4.0})}
    assert gate(repo, "Write", ti=ti) == "ask"
    # a brand-new request file is NOT gated
    ti2 = {"file_path": str(repo / ".harness" / "requests" / "r2.json"),
           "content": json.dumps({"status": "prepared", "command": "python3 pipeline.py --pay --b", "est_usd": 4.0})}
    assert gate(repo, "Write", ti=ti2) in (None, "allow")


def test_ledger_truncation_is_gated(repo):
    cfg(repo, autonomy="assisted")
    for c in ["rm .harness/ledger/claude-x.jsonl", "echo '' > .harness/ledger/claude-x.jsonl",
              "truncate -s0 .harness/ledger/claude-x.jsonl"]:
        assert gate(repo, cmd=c) in ("ask", "deny"), c                    # never allowed/silent



def test_assisted_prompts_for_arbitrary_repo_targets_and_outside_paths(repo):
    cfg(repo, autonomy="assisted")
    for c in ["make deploy", "npm run release", "pnpm exec something", "npx some-tool", "cat /etc/hostname",
              "ls /home"]:
        assert gate(repo, cmd=c) is None, c
    for c in ["ls /tmp/x", f"ls {repo}/docs", "npm test", "pip install -q requests"]:
        assert gate(repo, cmd=c) == "allow", c


def test_pull_and_commit_keeps_harness_files_merged_on_main(repo):
    """Right after the owner merges a harness update into main, `git pull … && git commit …` must not undo it."""
    g(repo, "add", "-A"); g(repo, "commit", "-qm", "wip")
    g(repo, "checkout", "-q", "main")
    (repo / ".harness" / "config.json").write_text(json.dumps({"timezone": "Europe/Istanbul", "merge": "ask"}))
    g(repo, "commit", "-qam", "owner merged harness update")
    g(repo, "checkout", "-q", "claude/x")
    cmd = "git merge -q --no-edit main && git commit -q --allow-empty -m after"
    pre = {"cwd": str(repo), "tool_name": "Bash", "tool_use_id": "pm", "tool_input": {"command": cmd}}
    e = dict(os.environ); e.pop("HARNESS_UNATTENDED", None)
    subprocess.run([sys.executable, str(SCRIPTS / "gate_pretool.py")], input=json.dumps(pre), capture_output=True, text=True, env=e)
    subprocess.run(cmd, shell=True, cwd=repo, check=True)
    post = subprocess.run([sys.executable, str(SCRIPTS / "post_tool.py")], input=json.dumps(pre), capture_output=True, text=True, env=e)
    assert '"merge": "ask"' in (repo / ".harness" / "config.json").read_text(), post.stdout
    assert "reverted" not in post.stdout
