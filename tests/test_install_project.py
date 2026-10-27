import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "dev-harness" / "scripts"))
import harness_lib as H  # noqa: E402


def _install(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Bash(ls:*)"]},
         "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
    for _ in range(2):  # idempotent
        r = subprocess.run([sys.executable, str(REPO / "install_project.py"), str(tmp_path)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    return tmp_path / ".claude"


def test_install_layout_and_settings(tmp_path):
    c = _install(tmp_path)
    s = json.loads((c / "settings.json").read_text())
    assert s["permissions"]["allow"] == ["Bash(ls:*)"]
    assert {k: len(v) for k, v in s["hooks"].items()} == {"Stop": 2, "SessionStart": 1, "UserPromptSubmit": 1,
                                                          "PreToolUse": 1, "PostToolUse": 1}
    cmds = [h["command"] for g in sum(s["hooks"].values(), []) for h in g["hooks"]]
    assert "echo mine" in cmds
    for cmd in cmds:
        if cmd != "echo mine":
            path = cmd.split('"')[1].replace("$CLAUDE_PROJECT_DIR", str(tmp_path))
            assert Path(path).is_file(), cmd
    for name in ["init", "spec", "build", "paid-call", "experiment", "autoloop", "retro", "ship", "status"]:
        text = (c / "skills" / f"dev-harness-{name}" / "SKILL.md").read_text()
        assert f"name: dev-harness-{name}\n" in text
    for name in ["planner", "builder", "evaluator", "reviewer"]:
        assert f"name: dev-harness-{name}\n" in (c / "agents" / f"dev-harness-{name}.md").read_text()
    for f in c.rglob("*"):
        if f.is_file() and f.suffix in (".md", ".py", ".json", ".sh"):
            t = f.read_text()
            assert "CLAUDE_PLUGIN_ROOT" not in t and "/dev-harness:" not in t and "`dev-harness:" not in t, f


def test_installed_files_are_control_files(tmp_path):
    c = _install(tmp_path)
    for rel in [".claude/settings.json", ".claude/skills/dev-harness-build/SKILL.md", ".claude/agents/dev-harness-builder.md",
                ".claude/dev-harness/scripts/gate_pretool.py"]:
        assert H.matches_any(rel, H.CONTROL_FILES), rel
    assert not H.matches_any(".claude/skills/other/SKILL.md", H.CONTROL_FILES)
    snap = H.snapshot(tmp_path, {})
    assert ".claude/dev-harness/scripts/gate_pretool.py" in snap


def test_installed_gate_denies_push_to_main(tmp_path):
    c = _install(tmp_path)
    payload = {"cwd": str(tmp_path), "tool_name": "Bash", "tool_use_id": "t",
               "tool_input": {"command": "git push origin HEAD:" + "main"}}
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.json").write_text("{}")
    r = subprocess.run([sys.executable, "-B", str(c / "dev-harness" / "scripts" / "gate_pretool.py")],
                       input=json.dumps(payload), capture_output=True, text=True)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
