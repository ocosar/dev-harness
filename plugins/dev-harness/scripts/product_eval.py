#!/usr/bin/env python3
"""Product evals (optional, evals.product.enabled): a fixed eval set scored by the repo's own command, compared with
an owner-approved baseline. It evaluates what the product OUTPUTS (videos, extractions, answers), not the code.

The repo's eval command (evals.product.cmd) must print, as its LAST stdout line:
  {"score": <number>, "ok": <true|false>, "metrics": {...optional per-case numbers...}, "notes": "..."}
If it uses a model as judge, it gets HARNESS_JUDGE_PROVIDER / HARNESS_JUDGE_MODEL from evals.product.judge and can
call llm_client.call(...) — any provider. Then the eval spends money: set evals.product.paid = true.

usage:
  product_eval.py run                → FREE evals only: runs cmd, compares with the baseline, writes the result
  product_eval.py compare --from F   → PAID evals: run the cmd yourself through the paid gate (output saved to F),
                                       then compare that output with the baseline
  product_eval.py propose-baseline   → prints the JSON for .harness/evals/baseline.json from this branch's last
                                       result; writing that file is a control-file change (owner click)
Exit code: 0 = no regression, 1 = regression or not ok, 2 = usage/config error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
from eval_packet import root_or_die  # noqa: E402


def last_json(out: str) -> dict | None:
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return d if isinstance(d, dict) and "score" in d else None
            except json.JSONDecodeError:
                return None
    return None


def compare(cfg: dict, result: dict, baseline: dict | None) -> dict:
    pe = cfg["evals"]["product"]
    direction = pe.get("direction", "max")
    tol = float(pe.get("tolerance") or 0)
    score = float(result["score"])
    verdict = {"score": score, "ok": bool(result.get("ok", True)), "baseline": None, "delta": None,
               "regression": False, "metrics": result.get("metrics"), "notes": str(result.get("notes", ""))[:2000]}
    if baseline is not None:
        b = float(baseline["score"])
        delta = score - b if direction == "max" else b - score   # positive = better
        verdict.update(baseline=b, delta=round(delta, 6), regression=delta < -tol)
        bm, rm = baseline.get("metrics") or {}, result.get("metrics") or {}
        worse = []
        for k in sorted(set(bm) & set(rm)):
            try:
                d = float(rm[k]) - float(bm[k]) if direction == "max" else float(bm[k]) - float(rm[k])
            except (TypeError, ValueError):
                continue
            if d < -tol:
                worse.append({"case": k, "baseline": bm[k], "now": rm[k]})
        verdict["worse_cases"] = worse[:20]
    verdict["pass"] = verdict["ok"] and not verdict["regression"]
    return verdict


def save(root: Path, cfg: dict, verdict: dict, raw: dict) -> Path:
    d = H.evals_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"ts": H.now_iso(cfg), "head": H.git(root, "rev-parse", "--short", "HEAD"), **verdict, "raw": raw}
    (d / "product.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    H.append_jsonl(d / "product-history.jsonl", {k: rec[k] for k in ("ts", "head", "score", "baseline", "delta", "pass")})
    H.log_event(root, "product_eval", score=verdict["score"], baseline=verdict["baseline"], passed=verdict["pass"])
    return d / "product.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    c = sub.add_parser("compare")
    c.add_argument("--from", dest="src", required=True)
    sub.add_parser("propose-baseline")
    a = ap.parse_args()
    root = root_or_die()
    cfg = H.load_config(root)
    pe = cfg["evals"]["product"]
    if not pe.get("enabled") or not pe.get("cmd"):
        print("product evals are off (evals.product.enabled / cmd in .harness/config.json)")
        return 2
    try:
        baseline = json.loads((root / H.EVAL_BASELINE).read_text())
    except (OSError, ValueError):
        baseline = None
    if a.cmd == "propose-baseline":
        try:
            last = json.loads((H.evals_dir(root) / "product.json").read_text())
        except (OSError, ValueError):
            print("no result on this branch yet — run the eval first")
            return 2
        print(json.dumps({"score": last["score"], "metrics": last.get("metrics") or {}, "set_at": H.now_iso(cfg),
                          "head": last.get("head"), "cmd": pe["cmd"]}, indent=2, ensure_ascii=False))
        print(f"\n→ write this to {H.EVAL_BASELINE} with the Write tool; the owner approves the change.", file=sys.stderr)
        return 0
    if a.cmd == "run":
        if pe.get("paid") or any(__import__("re").search(p, pe["cmd"]) for p in H.paid_patterns(cfg)):
            print("this eval spends money (evals.product.paid or a paid pattern). Run it through the paid gate: "
                  f"prepare ONE request for exactly `{pe['cmd']} > {H.EVALS_DIR}/<branch>/product-out.txt`, run it, "
                  "then `product_eval.py compare --from <that file>`.")
            return 2
        env = dict(os.environ, HARNESS_JUDGE_PROVIDER=(pe.get("judge") or {}).get("provider", ""),
                   HARNESS_JUDGE_MODEL=(pe.get("judge") or {}).get("model", ""), PYTHONDONTWRITEBYTECODE="1")
        import llm_client as L
        so_key = cfg["evals"]["second_opinion"].get("key_env") or ""
        names = set(cfg["paid"].get("secret_env") or []) | set(L.DEFAULT_KEY_ENV.values()) | ({so_key} if so_key else set())
        names |= {k for k in env if re.search(r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)$|_(?:KEY|TOKEN|SECRET)_", k)}
        for name in names:   # a free eval must not reach paid APIs — strip configured + any KEY/TOKEN/SECRET-named var
            env.pop(name, None)
        try:
            r = subprocess.run(pe["cmd"], shell=True, cwd=root, capture_output=True, text=True, env=env,
                               timeout=int(pe.get("timeout_s") or 3600))
            out = r.stdout
        except subprocess.TimeoutExpired:
            out = ""
    else:
        out = Path(a.src).read_text(errors="replace")
    raw = last_json(out)
    if raw is None:
        print("the eval command did not print a last line {\"score\": …, \"ok\": …}")
        return 2
    verdict = compare(cfg, raw, baseline)
    path = save(root, cfg, verdict, raw)
    b = "no baseline yet" if baseline is None else f"baseline {verdict['baseline']} → Δ {verdict['delta']:+g}"
    print(f"{'PASS' if verdict['pass'] else 'FAIL'}: score {verdict['score']:g} ({b}); ok={verdict['ok']}"
          + (f"; worse cases: {', '.join(w['case'] for w in verdict.get('worse_cases', []))}" if verdict.get("worse_cases") else "")
          + f"\nresult: {path}")
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
