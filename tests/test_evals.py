"""v0.5 evals: eval packet + manual verdicts, API second opinion (paid, gated), product evals vs. baseline."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "dev-harness" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import harness_lib as H  # noqa: E402
import llm_client as L  # noqa: E402


def g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def run(repo, name, *args, inp=None, env=None):
    e = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    e.update(env or {})
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=repo, capture_output=True, text=True,
                          input=inp, timeout=120, env=e)


def gate(repo, cmd, env=None):
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPTS / "gate_pretool.py")], capture_output=True, text=True, env=e,
                       input=json.dumps({"cwd": str(repo), "tool_name": "Bash", "tool_use_id": "t",
                                         "tool_input": {"command": cmd}}))
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None


@pytest.fixture
def repo(tmp_path):
    g(tmp_path, "init", "-q", "-b", "main")
    g(tmp_path, "config", "user.email", "t@t")
    g(tmp_path, "config", "user.name", "t")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.json").write_text(json.dumps({"timezone": "Europe/Istanbul"}))
    (tmp_path / "app.py").write_text("print('v1')\n")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-qm", "base")
    g(tmp_path, "checkout", "-qb", "claude/feat")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text("# Spec\nPrint v2.\n")
    (tmp_path / ".harness" / "features.json").write_text(json.dumps({
        "spec": "docs/spec.md", "approved": True,
        "features": [{"id": "F1", "title": "v2", "acceptance": ["python app.py prints v2"], "status": "done",
                      "passes": True, "evidence": "out.txt"}]}))
    (tmp_path / "app.py").write_text("API_KEY = 'sk-abcdefghijklmnop'\nprint('v2')\n")
    g(tmp_path, "add", "-A")
    g(tmp_path, "commit", "-qm", "v2")
    return tmp_path


def set_cfg(repo, **evals):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg.setdefault("evals", {}).update(evals)
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))


# ---------- 1. manual packet ----------
def test_packet_contains_spec_criteria_diff_questions_and_redacts(repo):
    r = run(repo, "eval_packet.py", "build", "--base", "main")
    assert r.returncode == 0, r.stderr
    text = Path(r.stdout.strip()).read_text()
    assert "Print v2." in text and "python app.py prints v2" in text and "+print('v2')" in text
    assert "VERDICT: PASS" in text and "different model family" in text
    assert "sk-abcdefghijklmnop" not in text and "[REDACTED]" in text
    assert Path(r.stdout.strip()) == repo / ".harness" / "evals" / "claude-feat" / "packet.md"


@pytest.mark.parametrize("answer,verdict", [
    ("BLOCKER: x\n\nVERDICT: FAIL", "FAIL"),
    ("Looks fine.\n**Verdict:** PASS", "PASS"),
    ("SHOULD: y\nVERDICT - concerns", "CONCERNS"),
    ("I think it's ok", "UNPARSED"),
])
def test_record_parses_verdicts(repo, answer, verdict):
    r = run(repo, "eval_packet.py", "record", "--model", "GPT-5 (chat)", inp=answer)
    assert r.stdout.strip() == verdict
    rows = list(H._rows(repo / ".harness" / "evals" / "claude-feat" / "verdicts.jsonl"))
    assert rows[-1]["model"] == "GPT-5 (chat)" and rows[-1]["verdict"] == verdict and rows[-1]["source"] == "manual"
    assert "GPT-5" in run(repo, "eval_packet.py", "status").stdout


# ---------- 2. API second opinion ----------
@pytest.mark.parametrize("provider", ["openai", "openrouter", "anthropic", "gemini"])
def test_build_request_shapes(provider, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    url, headers, body = L.build_request(provider, "m-1", "hello", "sys", [str(img)], 100, "KEY")
    assert url.startswith("https://") and "KEY" in json.dumps(headers) and "KEY" not in json.dumps(body)
    assert "hello" in json.dumps(body) and "iVBORw" in json.dumps(body)


def test_parse_response_all_providers():
    assert L.parse_response("openai", {"choices": [{"message": {"content": "hi"}}],
                                       "usage": {"prompt_tokens": 3, "completion_tokens": 1}}) == ("hi", {"in": 3, "out": 1})
    assert L.parse_response("anthropic", {"content": [{"type": "text", "text": "hi"}],
                                          "usage": {"input_tokens": 3, "output_tokens": 1}}) == ("hi", {"in": 3, "out": 1})
    assert L.parse_response("gemini", {"candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                                       "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}) == ("hi", {"in": 3, "out": 1})


def test_second_opinion_send_is_paid_and_gated(repo):
    cmd = f'python3 "{SCRIPTS}/second_opinion.py" --send'
    assert gate(repo, cmd) == "deny"                                    # no prepared request
    assert gate(repo, f'python3 "{SCRIPTS}/llm_client.py" --provider openai --model x --prompt-file p --send') == "deny"
    assert gate(repo, f'python3 "{SCRIPTS}/second_opinion.py"') is None  # dry run is free
    assert gate(repo, f'python3 "{SCRIPTS}/eval_packet.py" build') is None


def test_second_opinion_dry_run_estimate(repo):
    set_cfg(repo, second_opinion={"mode": "api", "provider": "openai", "model": "gpt-x",
                                  "usd_per_mtok_in": 2.0, "usd_per_mtok_out": 8.0})
    r = run(repo, "second_opinion.py", "--base=main")
    d = json.loads(r.stdout)
    assert d["dry_run"] and d["est_usd"] > 0 and d["model"] == "gpt-x"


class _Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers["Authorization"] == "Bearer test-key"
        assert "VERDICT" in json.dumps(body)
        out = json.dumps({"choices": [{"message": {"content": "SHOULD: add a test\nVERDICT: CONCERNS"}}],
                          "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_second_opinion_send_records_verdict_with_fake_api(repo, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    set_cfg(repo, second_opinion={"mode": "api", "provider": "openai", "model": "gpt-x", "key_env": "T_KEY",
                                  "usd_per_mtok_in": 2.0, "usd_per_mtok_out": 8.0})
    # run in-process so ENDPOINT can point at the fake server
    monkeypatch.chdir(repo)
    monkeypatch.setenv("T_KEY", "test-key")
    monkeypatch.setitem(L.ENDPOINT, "openai", f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(sys, "argv", ["second_opinion.py", "--send", "--base=main"])
    import second_opinion
    assert second_opinion.main() == 0
    srv.shutdown()
    rows = list(H._rows(repo / ".harness" / "evals" / "claude-feat" / "verdicts.jsonl"))
    assert rows[-1]["verdict"] == "CONCERNS" and rows[-1]["source"] == "api"


def test_builtin_paid_booked_even_with_repo_ledger(repo):
    """The harness's own spend is never in the repo's ledger, so it is booked even when extra_ledgers is set."""
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["paid"] = {"extra_ledgers": [{"path": "out/ledger.jsonl", "ts_field": "ts", "usd_field": "usd"}]}
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    cmd = f'python3 "{SCRIPTS}/second_opinion.py" --send'
    H.write_json(repo / ".harness" / "requests" / "r1.json",
                 {"status": "asked", "command": cmd, "est_usd": 0.05, "created_at": H.now_iso()})
    e = dict(os.environ)
    subprocess.run([sys.executable, str(SCRIPTS / "post_tool.py")], capture_output=True, text=True, env=e,
                   input=json.dumps({"cwd": str(repo), "tool_name": "Bash", "tool_use_id": "t",
                                     "tool_input": {"command": cmd}, "tool_response": {}}))
    assert H.spent_today(repo, H.load_config(repo)) == pytest.approx(0.05)


# ---------- 3. product evals ----------
def write_eval(repo, score, ok=True, metrics=None):
    (repo / "ev.py").write_text("import json\nprint('working')\nprint(json.dumps(%r))\n"
                                % {"score": score, "ok": ok, "metrics": metrics or {}})


def test_product_eval_baseline_and_regression(repo):
    set_cfg(repo, product={"enabled": True, "cmd": "python3 ev.py", "direction": "max", "tolerance": 0.01})
    write_eval(repo, 0.80, metrics={"a": 0.9, "b": 0.7})
    r = run(repo, "product_eval.py", "run")
    assert r.returncode == 0 and "no baseline yet" in r.stdout
    prop = run(repo, "product_eval.py", "propose-baseline")
    base = json.loads(prop.stdout)
    assert base["score"] == 0.80
    (repo / ".harness" / "evals").mkdir(exist_ok=True)
    (repo / ".harness" / "evals" / "baseline.json").write_text(json.dumps(base))
    write_eval(repo, 0.795, metrics={"a": 0.9, "b": 0.69})       # within tolerance
    assert run(repo, "product_eval.py", "run").returncode == 0
    write_eval(repo, 0.70, metrics={"a": 0.9, "b": 0.5})          # regression
    r = run(repo, "product_eval.py", "run")
    assert r.returncode == 1 and "FAIL" in r.stdout and "b" in r.stdout
    write_eval(repo, 0.90, ok=False)                               # better score but not ok
    assert run(repo, "product_eval.py", "run").returncode == 1


def test_product_eval_free_run_strips_secrets(repo):
    cfg = json.loads((repo / ".harness" / "config.json").read_text())
    cfg["paid"] = {"secret_env": ["T_SECRET"]}
    cfg["evals"] = {"product": {"enabled": True, "cmd": "python3 ev.py"}}
    (repo / ".harness" / "config.json").write_text(json.dumps(cfg))
    (repo / "ev.py").write_text("import json, os\nprint(json.dumps({'score': 1 if (os.environ.get('T_SECRET') or os.environ.get('OPENAI_API_KEY')) else 0, 'ok': True, "
                                "'metrics': {'judge': os.environ.get('HARNESS_JUDGE_MODEL')}}))\n")
    run(repo, "product_eval.py", "run", env={"T_SECRET": "x", "OPENAI_API_KEY": "y"})
    res = json.loads((repo / ".harness" / "evals" / "claude-feat" / "product.json").read_text())
    assert res["score"] == 0


def test_paid_product_eval_goes_through_gate(repo):
    set_cfg(repo, product={"enabled": True, "cmd": "python3 ev.py --all-routes", "paid": True,
                           "judge": {"provider": "gemini", "model": "g"}})
    r = run(repo, "product_eval.py", "run")
    assert r.returncode == 2 and "paid gate" in r.stdout
    assert gate(repo, "python3 ev.py --all-routes > .harness/evals/claude-feat/product-out.txt") == "deny"
    (repo / "out.txt").write_text("log\n" + json.dumps({"score": 3, "ok": True}) + "\n")
    assert run(repo, "product_eval.py", "compare", "--from", "out.txt").returncode == 0


def test_baseline_is_a_control_file(repo):
    assert H.matches_any(".harness/evals/baseline.json", H.CONTROL_FILES)
    e = dict(os.environ)
    e.pop("HARNESS_UNATTENDED", None)
    r = subprocess.run([sys.executable, str(SCRIPTS / "gate_pretool.py")], capture_output=True, text=True, env=e,
                       input=json.dumps({"cwd": str(repo), "tool_name": "Write", "tool_use_id": "t",
                                         "tool_input": {"file_path": str(repo / ".harness/evals/baseline.json"),
                                                        "content": "{}"}}))
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
