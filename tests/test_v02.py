"""v0.2: locked evaluator guard, event log, LESSON capture, repo selftest cases, autoloop runner."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "scripts"
PAY_RE = r"python[0-9.]*\s+\S*pipeline\.py\b[^;&|]*\s--pay\b"

EVAL = """import json, pathlib
v = float(pathlib.Path('params.txt').read_text().split()[0])
print('noise line')
print(json.dumps({"score": 0.0 - abs(v - 7), "ok": v < 100, "notes": f"v={v}"}))
"""


def hook(script, payload, env=None):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPTS / script)], input=json.dumps(payload),
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def loop(repo, *args, ok=True):
    r = subprocess.run([sys.executable, str(SCRIPTS / "autoloop.py"), *args], cwd=repo, capture_output=True, text=True, timeout=120)
    if ok:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    g(tmp_path, "init", "-q", "-b", "main")
    g(tmp_path, "config", "user.email", "t@t")
    g(tmp_path, "config", "user.name", "t")
    (tmp_path / ".harness").mkdir()
    cfg = {"paid": {"bash_patterns": [PAY_RE]},
           "locked_paths": ["refdata/*"],
           "autoloop": {"tune": {"goal": "get v close to 7", "eval_cmd": f"{sys.executable} eval/score.py",
                                 "direction": "max", "editable": ["params.txt", "src/*"], "locked": ["eval/*"],
                                 "time_budget_s": 30, "max_runs": 6, "max_hours": 1}}}
    (tmp_path / ".harness" / "config.json").write_text(json.dumps(cfg))
    (tmp_path / ".gitignore").write_text(".harness/state/\n")
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "score.py").write_text(EVAL)
    (tmp_path / "params.txt").write_text("1\n")
    (tmp_path / "other.txt").write_text("x\n")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def edit_payload(repo, rel, tool="Write"):
    return {"cwd": str(repo), "tool_name": tool, "tool_input": {"file_path": str(repo / rel), "content": "x"}}


# ---------- locked files + events ----------
def test_locked_evaluator_needs_owner_and_is_logged(repo):
    out = hook("gate_pretool.py", edit_payload(repo, "eval/score.py"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    out = hook("gate_pretool.py", edit_payload(repo, "refdata/a.json"), env={"HARNESS_UNATTENDED": "1"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook("gate_pretool.py", edit_payload(repo, "params.txt")) is None
    ev = [json.loads(l) for l in (repo / ".harness" / "events" / "main.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in ev] == ["gate_ask", "gate_deny"] and ev[0]["input"].endswith("eval/score.py")


# ---------- LESSON capture ----------
def test_lesson_capture(repo):
    out = hook("capture_lesson.py", {"cwd": str(repo), "prompt": "fix it\nLESSON: always test inside the Docker image\nthanks"})
    assert "captured 1" in out["hookSpecificOutput"]["additionalContext"]
    assert "always test inside the Docker image" in (repo / ".harness" / "lessons-inbox" / "main.md").read_text()
    assert hook("capture_lesson.py", {"cwd": str(repo), "prompt": "no lesson here"}) is None
    (repo / ".harness" / "lessons.md").write_text("# Lessons\n\n- ffmpeg: test in image (source: PR 9)\n")
    ctx = hook("session_context.py", {"cwd": str(repo)})["hookSpecificOutput"]["additionalContext"]
    assert "ffmpeg: test in image" in ctx and "uncurated owner lesson" in ctx


# ---------- selftest cases ----------
def test_selftest_cases(repo):
    cases = {"paid_must_match": ["python pipeline.py --pay"], "paid_must_not_match": ["grep -- --pay pipeline.py"],
             "locked_must_match": ["eval/score.py"]}
    (repo / ".harness" / "cases.json").write_text(json.dumps(cases))
    r = subprocess.run([sys.executable, str(SCRIPTS / "selftest.py")], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    cases["paid_must_not_match"].append("python pipeline.py --from a --pay")  # contradicts the pattern
    (repo / ".harness" / "cases.json").write_text(json.dumps(cases))
    r = subprocess.run([sys.executable, str(SCRIPTS / "selftest.py")], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 1 and "FALSE POSITIVE" in r.stdout
    # and the Stop hook surfaces it
    (repo / "params.txt").write_text("2\n")
    assert hook("stop_check.py", {"cwd": str(repo)})["decision"] == "block"


# ---------- autoloop ----------
def tsv(repo):
    d = next((repo / ".harness" / "state" / "autoloop").iterdir())
    return [l.split("\t") for l in (d / "results.tsv").read_text().splitlines()[1:]]


def test_autoloop_keep_discard_report(repo):
    out = loop(repo, "start", "tune").stdout
    assert "baseline max score = -6.0" in out
    branch = g(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch.startswith("autoloop/tune-")
    (repo / "params.txt").write_text("5\n")
    assert "#1 KEEP" in loop(repo, "run", "--desc", "v=5").stdout
    (repo / "params.txt").write_text("2\n")
    assert "#2 DISCARD" in loop(repo, "run", "--desc", "v=2 worse").stdout
    assert (repo / "params.txt").read_text() == "5\n"  # reset to best
    (repo / "params.txt").write_text("500\n")
    assert "GUARD_FAIL" in loop(repo, "run", "--desc", "guard").stdout
    (repo / "other.txt").write_text("y\n")
    assert "out of scope" in loop(repo, "run", "--desc", "scope").stdout
    assert (repo / "other.txt").read_text() == "x\n"
    (repo / "params.txt").write_text("7\n")
    assert "KEEP" in loop(repo, "run", "--desc", "v=7").stdout
    (repo / "params.txt").write_text("8\n")
    loop(repo, "run", "--desc", "v=8")
    r = loop(repo, "run", "--desc", "over budget", ok=False)
    assert r.returncode == 10
    statuses = [row[4] for row in tsv(repo)]
    assert statuses == ["baseline", "keep", "discard", "guard_fail", "out_of_scope", "keep", "discard"]
    rep = loop(repo, "report").stdout
    assert "-6.0 → best 0.0" in rep
    md = next((repo / "docs" / "autoloop").glob("tune-*.md")).read_text()
    assert "| 5 |" in md and "v=7" in md
    assert g(repo, "status", "--porcelain") == ""  # report committed, tree clean
    assert "autoloop_report" in "".join(f.read_text() for f in (repo / ".harness" / "events").glob("*.jsonl"))


def test_autoloop_halts_on_tampering(repo):
    loop(repo, "start", "tune")
    (repo / "params.txt").write_text("6\n")
    (repo / "eval" / "score.py").write_text("print('{\"score\": 999}')\n")
    r = loop(repo, "run", "--desc", "cheat", ok=False)
    assert r.returncode == 3 and "HALT" in r.stdout
    assert (repo / "eval" / "score.py").read_text() == EVAL  # restored
    assert loop(repo, "run", "--desc", "again", ok=False).returncode == 3
    assert tsv(repo)[-1][4] == "tampered"


def test_autoloop_crash_and_simpler(repo):
    loop(repo, "start", "tune")
    (repo / "params.txt").write_text("not-a-number\n")
    assert "CRASH" in loop(repo, "run", "--desc", "typo").stdout
    (repo / "params.txt").write_text("1\n# a\n# b\n")
    loop(repo, "run", "--desc", "noise")  # same score, more lines -> discard
    assert tsv(repo)[-1][4] == "discard"


def test_autoloop_refuses_paid_eval_and_missing_lock(repo):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["autoloop"]["paid"] = dict(cfg["autoloop"]["tune"], eval_cmd="python pipeline.py --pay")
    cfg["autoloop"]["nolock"] = dict(cfg["autoloop"]["tune"], locked=["nothing/*"])
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    g(repo, "commit", "-qam", "cfg")
    assert "FREE evaluations only" in loop(repo, "start", "paid", ok=False).stdout
    assert "frozen evaluator is required" in loop(repo, "start", "nolock", ok=False).stdout
