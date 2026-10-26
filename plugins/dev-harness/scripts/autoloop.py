#!/usr/bin/env python3
"""dev-harness autoloop — autoresearch-style overnight tuning for FREE, measurable goals.

The agent only edits code and describes the idea; this script decides, deterministically:
  - scope:   only `editable` files may change (anything else → discard),
  - honesty: `locked` files (evaluator, references, test data) must be byte-identical to loop start (else HALT),
  - result:  run `eval_cmd` within `time_budget_s`; its LAST stdout line must be JSON {"score": <num>, "ok": <bool>, "notes": "..."};
             keep the commit if ok and better than the best by more than `min_delta` (or equal and simpler), else `git reset --hard`,
  - budget:  stop after `max_runs` runs or `max_hours`.
Results go to .harness/state/autoloop/<run>/results.tsv (untracked: survives resets); `report` writes docs/autoloop/<run>.md.

Profile (in .harness/config.json → "autoloop": {"<name>": {...}}; editing it needs the owner's click):
  goal, eval_cmd, direction ("max"|"min"), editable [globs], locked [globs], time_budget_s=900, max_runs=50,
  max_hours=8, min_delta=0, holdout_cmd (optional: run ONCE at report time, never during the loop)

Usage: autoloop.py start <profile> | run --desc "<idea>" | status | report
Exit codes: 0 ok · 2 usage/precondition · 3 HALT (tampering) · 10 budget reached (run `report`)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
from gate_pretool import is_paid_bash  # noqa: E402

STATE_REL = Path(H.HARNESS_DIR) / "state" / "autoloop.json"
TSV_HEAD = "run\tcommit\tscore\tbest\tstatus\tseconds\tdescription\tnotes\n"


def sh(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(list(args), cwd=root, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"autoloop: `{' '.join(args)}` failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def die(msg: str, code: int = 2) -> None:
    print(f"autoloop: {msg}")
    sys.exit(code)


def ensure_ignored(root: Path) -> None:
    probe = ".harness/state/x"
    if subprocess.run(["git", "check-ignore", "-q", probe], cwd=root).returncode != 0:
        excl = root / ".git" / "info" / "exclude"
        excl.parent.mkdir(parents=True, exist_ok=True)
        with excl.open("a") as f:
            f.write("\n.harness/state/\n")


def repo_files(root: Path) -> list[str]:
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "node_modules", ".venv")]
        for fn in files:
            out.append((Path(d) / fn).relative_to(root).as_posix())
    return out


def hash_locked(root: Path, patterns: list[str]) -> dict:
    res = {}
    for rel in repo_files(root):
        if rel.startswith(".harness/state/"):
            continue
        if H.matches_any(rel, patterns):
            res[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    return res


def dirty(root: Path) -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    files, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            i += 1  # -z: the rename source follows as its own field
        if not path.startswith(".harness/"):
            files.append(path)
    return files


def reset_to(root: Path, commit: str) -> None:
    """git reset --hard <commit>, but keep harness bookkeeping (progress, events, lessons) and drop stray files."""
    keep = {}
    for rel in sh(root, "git", "diff", "--name-only", "HEAD", "--", ".harness").splitlines():
        p = root / rel
        if p.is_file():
            keep[rel] = p.read_bytes()
    sh(root, "git", "reset", "-q", "--hard", commit)
    sh(root, "git", "clean", "-q", "-fd", "-e", ".harness")
    for rel, data in keep.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)


def run_eval(root: Path, cmd: str, timeout: int) -> tuple[str, float | None, bool, str, float]:
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "crash", None, False, f"timeout > {timeout}s", time.time() - t0
    secs = time.time() - t0
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    try:
        res = json.loads(lines[-1])
        score = round(float(res["score"]), 6) + 0.0  # stable decimals, no -0.0
    except Exception:
        tail = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["no output"])[-1]
        return "crash", None, False, f"exit {r.returncode}; no JSON score line; {tail[:160]}", secs
    if r.returncode != 0:
        return "crash", score, False, f"exit {r.returncode}", secs
    return "ok", score, bool(res.get("ok", True)), str(res.get("notes", ""))[:300], secs


def load(root: Path) -> dict:
    try:
        return json.loads((root / STATE_REL).read_text())
    except (OSError, json.JSONDecodeError):
        die("no active autoloop — run `start <profile>` first")


def tsv_path(root: Path, st: dict) -> Path:
    return root / H.HARNESS_DIR / "state" / "autoloop" / st["run_id"] / "results.tsv"


def append(root: Path, st: dict, row: list) -> None:
    p = tsv_path(root, st)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a") as f:
        if new:
            f.write(TSV_HEAD)
        f.write("\t".join("" if v is None else str(v).replace("\t", " ").replace("\n", " ") for v in row) + "\n")


def better(st: dict, score: float) -> bool:
    d = float(st["min_delta"])
    return score > st["best"] + d if st["direction"] == "max" else score < st["best"] - d


def cmd_start(root: Path, cfg: dict, name: str) -> None:
    prof = (cfg.get("autoloop") or {}).get(name)
    if not prof:
        die(f"no autoloop profile '{name}' in .harness/config.json (the owner adds it)")
    for k in ("eval_cmd", "editable", "locked"):
        if not prof.get(k):
            die(f"profile '{name}' needs '{k}'")
    for k in ("eval_cmd", "holdout_cmd"):
        if prof.get(k) and is_paid_bash(cfg, prof[k]):
            die(f"{k} matches a paid pattern — autoloop is for FREE evaluations only. Use /dev-harness:experiment.")
    if (root / STATE_REL).exists() and not json.loads((root / STATE_REL).read_text()).get("finished"):
        die("an autoloop is already active — `report` it first (or the owner deletes .harness/state/autoloop.json)")
    if dirty(root):
        die(f"working tree not clean: {dirty(root)[:5]} — commit or stash first")
    locked = hash_locked(root, prof["locked"])
    if not locked:
        die("no file matches 'locked' — a frozen evaluator is required (otherwise the metric can be gamed)")
    ensure_ignored(root)
    run_id = f"{name}-{H._dt.datetime.now().strftime('%Y%m%d-%H%M')}"
    sh(root, "git", "checkout", "-q", "-b", f"autoloop/{run_id}")
    st = {"run_id": run_id, "profile": name, "goal": prof.get("goal", ""), "branch": f"autoloop/{run_id}",
          "direction": prof.get("direction", "max"), "min_delta": float(prof.get("min_delta", 0)),
          "editable": prof["editable"], "locked": prof["locked"], "locked_hashes": locked,
          "eval_cmd": prof["eval_cmd"], "holdout_cmd": prof.get("holdout_cmd"),
          "time_budget_s": int(prof.get("time_budget_s", 900)), "max_runs": int(prof.get("max_runs", 50)),
          "max_hours": float(prof.get("max_hours", 8)), "started": time.time(), "runs": 0,
          "base_commit": sh(root, "git", "rev-parse", "HEAD")}
    status, score, ok, notes, secs = run_eval(root, st["eval_cmd"], st["time_budget_s"])
    if status != "ok" or not ok:
        sh(root, "git", "checkout", "-q", "-")
        sh(root, "git", "branch", "-q", "-D", st["branch"])
        die(f"baseline evaluation failed ({status}, ok={ok}): {notes} — fix the evaluator before looping")
    st.update(best=score, baseline=score, best_commit=st["base_commit"])
    H.write_json(root / STATE_REL, st)
    append(root, st, [0, st["base_commit"][:8], score, score, "baseline", round(secs, 1), "baseline", notes])
    H.log_event(root, "autoloop_start", run=run_id, baseline=score, locked_files=len(locked))
    print(f"autoloop started: {run_id} on branch {st['branch']}\n  goal: {st['goal']}\n  baseline {st['direction']} "
          f"score = {score} ({secs:.0f}s)  · {len(locked)} locked files · budget {st['max_runs']} runs / {st['max_hours']} h\n"
          f"  editable: {', '.join(st['editable'])}\nNext: change ONLY editable files, then `run --desc \"<idea>\"`.")


def cmd_run(root: Path, desc: str) -> None:
    st = load(root)
    if st.get("finished"):
        die("this autoloop is finished — start a new one")
    if st.get("halted"):
        die(f"HALTED ({st['halted']}) — the owner must look before anything else runs", 3)
    if sh(root, "git", "rev-parse", "--abbrev-ref", "HEAD") != st["branch"]:
        die(f"not on the loop branch {st['branch']}")
    elapsed_h = (time.time() - st["started"]) / 3600
    if st["runs"] >= st["max_runs"] or elapsed_h >= st["max_hours"]:
        die(f"budget reached ({st['runs']} runs, {elapsed_h:.1f} h) — run `report` and stop", 10)
    n = st["runs"] + 1
    # 1. auto-commit the agent's working changes
    changed_now = dirty(root)
    out_now = [f for f in changed_now if not H.matches_any(f, st["editable"])]
    if changed_now and not out_now:
        sh(root, "git", "add", "-A", "--", *changed_now)
        sh(root, "git", "commit", "-q", "-m", f"autoloop #{n}: {desc[:120]}")
    head = sh(root, "git", "rev-parse", "HEAD")
    changed = sorted(set(sh(root, "git", "diff", "--name-only", st["best_commit"], head).splitlines()) | set(out_now))
    if not changed:
        die("nothing changed since the best commit — edit an editable file first")
    # 2. honesty: locked files untouched (on disk, committed or not)
    now = hash_locked(root, st["locked"])
    if now != st["locked_hashes"]:
        diff = sorted(set(now.items()) ^ set(st["locked_hashes"].items()))
        reset_to(root, st["best_commit"])
        st["halted"] = "tampered"
        H.write_json(root / STATE_REL, st)
        append(root, st, [n, head[:8], "", st["best"], "tampered", 0, desc, f"locked files changed: {[d[0] for d in diff][:5]}"])
        H.log_event(root, "autoloop_tampered", run=st["run_id"], n=n, files=[d[0] for d in diff][:5])
        die("HALT: a locked evaluator/reference file changed. Reset done. Stop the loop and report this to the owner.", 3)
    # 3. scope
    out_scope = [f for f in changed if not H.matches_any(f, st["editable"])]
    st["runs"] = n
    if out_scope:
        reset_to(root, st["best_commit"])
        H.write_json(root / STATE_REL, st)
        append(root, st, [n, head[:8], "", st["best"], "out_of_scope", 0, desc, f"non-editable files: {out_scope[:5]}"])
        H.log_event(root, "autoloop_out_of_scope", run=st["run_id"], n=n, files=out_scope[:5])
        print(f"#{n} DISCARDED (out of scope: {out_scope[:5]}). Only {st['editable']} may change. Reset to best.")
        return
    # 4. evaluate
    status, score, ok, notes, secs = run_eval(root, st["eval_cmd"], st["time_budget_s"])
    if status == "crash" or score is None:
        verdict = "crash"
    elif not ok:
        verdict = "guard_fail"
    elif better(st, score):
        verdict = "keep"
    elif score == st["best"]:
        num = sh(root, "git", "diff", "--numstat", st["best_commit"], head)
        net = sum(int(a) - int(d) for a, d, *_ in (l.split("\t") for l in num.splitlines()) if a.isdigit() and d.isdigit())
        verdict = "keep_simpler" if net < 0 else "discard"
    else:
        verdict = "discard"
    if verdict.startswith("keep"):
        st.update(best=score, best_commit=head)
    else:
        reset_to(root, st["best_commit"])
    H.write_json(root / STATE_REL, st)
    append(root, st, [n, head[:8], score, st["best"], verdict, round(secs, 1), desc, notes])
    left = min(st["max_runs"] - n, int((st["max_hours"] - (time.time() - st["started"]) / 3600) * 3600 / max(secs, 1)))
    print(f"#{n} {verdict.upper()}: score {score} (best {st['best']}, baseline {st['baseline']}) in {secs:.0f}s"
          f"{' — ' + notes if notes else ''}\n~{max(left, 0)} runs left in budget. "
          + ("Build on this." if verdict.startswith("keep") else "Reset to best; try a different idea."))


def cmd_status(root: Path) -> None:
    st = load(root)
    p = tsv_path(root, st)
    rows = p.read_text().splitlines()[1:] if p.exists() else []
    counts = {}
    for r in rows[1:]:
        s = r.split("\t")[4]
        counts[s] = counts.get(s, 0) + 1
    print(json.dumps({k: st.get(k) for k in ("run_id", "branch", "goal", "direction", "baseline", "best", "best_commit",
                                             "runs", "max_runs", "halted", "finished")} | {"by_status": counts}, indent=2))


def cmd_report(root: Path) -> None:
    st = load(root)
    rows = [r.split("\t") for r in tsv_path(root, st).read_text().splitlines()[1:]]
    if st.get("halted"):
        reset_to(root, st["best_commit"])
    hold = None
    if st.get("holdout_cmd") and not st.get("halted"):
        status, score, ok, notes, secs = run_eval(root, st["holdout_cmd"], st["time_budget_s"])
        hold = f"{score} (ok={ok}, {status}{', ' + notes if notes else ''}) — run ONCE on the best commit"
    counts = {}
    for r in rows:
        counts[r[4]] = counts.get(r[4], 0) + 1
    kept = [r for r in rows if r[4].startswith("keep")]
    hrs = (time.time() - st["started"]) / 3600
    lines = [f"# Autoloop {st['run_id']}", "",
             f"**Goal:** {st['goal']}  ", f"**Result:** {st['direction']} score {st['baseline']} → **{st['best']}** "
             f"after {st['runs']} runs in {hrs:.1f} h ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}).  ",
             f"**Holdout (unseen during the loop):** {hold or 'not configured'}  ",
             f"**Status:** {'HALTED — ' + st['halted'] if st.get('halted') else 'complete'} · branch `{st['branch']}` · "
             f"best commit `{st['best_commit'][:8]}` · base `{st['base_commit'][:8]}`", "",
             "## Kept changes", "", "| # | commit | score | idea |", "| --- | --- | --- | --- |"]
    lines += [f"| {r[0]} | `{r[1]}` | {r[2]} | {r[6]} |" for r in kept] or ["| — | — | — | nothing beat the baseline |"]
    lines += ["", "## All runs", "", "| # | commit | score | best | status | s | idea | notes |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines += ["| " + " | ".join(r[:8] + [""] * (8 - len(r))) + " |" for r in rows]
    lines += ["", "Owner: review the kept diff (`git diff " + st["base_commit"][:8] + ".." + st["best_commit"][:8] +
              "`), then merge the PR or close it. Nothing here was merged automatically."]
    out = root / "docs" / "autoloop" / f"{st['run_id']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    st["finished"] = True
    H.write_json(root / STATE_REL, st)
    H.log_event(root, "autoloop_report", run=st["run_id"], baseline=st["baseline"], best=st["best"], runs=st["runs"],
                kept=len(kept), holdout=hold, halted=st.get("halted"))
    sh(root, "git", "add", "-f", str(out.relative_to(root)))
    if (root / H.EVENTS).exists():
        sh(root, "git", "add", H.EVENTS)
    sh(root, "git", "commit", "-q", "-m", f"autoloop {st['run_id']}: report ({st['baseline']} → {st['best']})")
    print(f"report: {out.relative_to(root)}\nbaseline {st['baseline']} → best {st['best']} · kept {len(kept)} · "
          f"holdout {hold or 'n/a'}\nNext: push {st['branch']} and open a PR — the owner merges.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="autoloop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start").add_argument("profile")
    sub.add_parser("run").add_argument("--desc", required=True)
    sub.add_parser("status")
    sub.add_parser("report")
    a = ap.parse_args()
    root = H.find_root(None)
    if not root:
        die("no .harness/config.json above the current directory")
    cfg = H.load_config(root)
    {"start": lambda: cmd_start(root, cfg, a.profile), "run": lambda: cmd_run(root, a.desc),
     "status": lambda: cmd_status(root), "report": lambda: cmd_report(root)}[a.cmd]()


if __name__ == "__main__":
    main()
