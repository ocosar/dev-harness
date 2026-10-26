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
  max_hours=8, min_delta=0, patience=20 (stop after this many runs without a keep),
  forbid_in_diff [strings] (e.g. test-file ids: a diff that adds one is discarded as metric gaming),
  holdout_cmd (optional: scored on the base AND the best commit at report time, never during the loop).
Evaluations run with the env vars in config paid.secret_env removed, so a free evaluator can't spend money.

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


def hash_locked(root: Path, patterns: list[str]) -> dict:
    """sha256 of every file matching the locked patterns, gitignored data included."""
    return {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest()
            for rel in H.expand(root, patterns) if not rel.startswith(".harness/state/")}


def noise(path: str) -> bool:
    return path.startswith(".harness/") or "__pycache__/" in path or path.endswith((".pyc", ".pyo"))


def purge_bytecode(root: Path, editable: list[str]) -> None:
    """Stale .pyc can make an edit score as the previous version (same size, same second) — remove it."""
    import re as _re
    import shutil
    for pat in editable:
        base = root / _re.split(r"[*?\[]", pat, maxsplit=1)[0]
        base = base if base.is_dir() else base.parent
        if base.is_dir():
            for d in list(base.rglob("__pycache__")):
                shutil.rmtree(d, ignore_errors=True)


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
        if not noise(path):
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


def eval_env(cfg: dict) -> dict:
    env = dict(os.environ)
    for k in (cfg.get("paid") or {}).get("secret_env") or []:
        env.pop(k, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_eval(root: Path, cmd: str, timeout: int, env: dict | None = None) -> tuple[str, float | None, bool, str, float]:
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return "crash", None, False, f"timeout > {timeout}s", time.time() - t0
    secs = time.time() - t0
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    scored = []
    for ln in lines:
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict) and "score" in obj:
                scored.append(ln)
        except ValueError:
            pass
    if len(scored) > 1:
        return "crash", None, False, f"{len(scored)} score lines — only the evaluator may print one (code under test printed a score?)", time.time() - t0
    try:
        res = json.loads(lines[-1])
        score = round(float(res["score"]), 6) + 0.0  # stable decimals, no -0.0
    except Exception:
        tail = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["no output"])[-1]
        return "crash", None, False, f"exit {r.returncode}; no JSON score line; {tail[:160]}", secs
    if r.returncode != 0:
        return "crash", score, False, f"exit {r.returncode}", secs
    return "ok", score, bool(res.get("ok", True)), str(res.get("notes", ""))[:300], secs


PROFILE_KEYS = ("eval_cmd", "holdout_cmd", "editable", "locked", "direction", "min_delta", "time_budget_s", "max_runs",
                "max_hours", "patience", "forbid_in_diff")
MIRROR_KEYS = ("run_id", "branch", "base_commit", "baseline", "best", "best_commit", "runs", "since_keep",
               "locked_hashes", "started", "halted", "finished")


def profile_fields(prof: dict) -> dict:
    return {"eval_cmd": prof.get("eval_cmd"), "holdout_cmd": prof.get("holdout_cmd"),
            "editable": list(prof.get("editable") or []), "locked": list(prof.get("locked") or []),
            "direction": prof.get("direction", "max"), "min_delta": float(prof.get("min_delta", 0)),
            "time_budget_s": int(prof.get("time_budget_s", 900)), "max_runs": int(prof.get("max_runs", 50)),
            "max_hours": float(prof.get("max_hours", 8)), "patience": int(prof.get("patience", 20)),
            "forbid_in_diff": list(prof.get("forbid_in_diff") or [])}


def mirror_path(root: Path, run_id: str) -> Path:
    gd = Path(sh(root, "git", "rev-parse", "--git-dir"))
    return (gd if gd.is_absolute() else root / gd) / "dev-harness" / f"autoloop-{run_id}.json"


def save(root: Path, st: dict) -> None:
    """State lives in .harness/state (readable) and a mirror under .git/ — the runner trusts neither alone."""
    H.write_json(root / STATE_REL, st)
    H.write_json(mirror_path(root, st["run_id"]), {k: st.get(k) for k in MIRROR_KEYS})


def load(root: Path, cfg: dict | None = None) -> dict:
    try:
        st = json.loads((root / STATE_REL).read_text())
    except (OSError, json.JSONDecodeError):
        die("no active autoloop — run `start <profile>` first")
    problems = []
    try:
        mirror = json.loads(mirror_path(root, st["run_id"]).read_text())
        problems += [k for k in MIRROR_KEYS if mirror.get(k) != st.get(k)]
    except (OSError, json.JSONDecodeError, KeyError):
        problems.append("mirror missing")
    if cfg is not None and not st.get("finished"):
        prof = (cfg.get("autoloop") or {}).get(st.get("profile"), {})
        want = profile_fields(prof) if prof else {}
        problems += [f"profile.{k}" for k in PROFILE_KEYS if want.get(k) != st.get(k)]
    if problems and not st.get("halted"):
        st["halted"] = "state tampered: " + ", ".join(problems[:6])
        H.write_json(root / STATE_REL, st)
        H.log_event(root, "autoloop_tampered", run=st.get("run_id"), what=st["halted"])
    return st


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


def budget_reason(st: dict) -> str:
    """Why the loop must end now ('' = keep going). Shared with the Stop hook."""
    if st["runs"] >= st["max_runs"]:
        return f"{st['runs']} runs = max_runs"
    hrs = (time.time() - st["started"]) / 3600
    if hrs >= st["max_hours"]:
        return f"{hrs:.1f} h ≥ max_hours"
    if st.get("since_keep", 0) >= st.get("patience", 20):
        return f"plateau: {st['since_keep']} runs without improvement"
    return ""


def gaming_tokens(root: Path, st: dict, head: str) -> list[str]:
    toks = st.get("forbid_in_diff") or []
    if not toks:
        return []
    added = "\n".join(l[1:] for l in sh(root, "git", "diff", "-U0", st["best_commit"], head).splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    return [t for t in toks if t and t in added]


def cmd_start(root: Path, cfg: dict, name: str) -> None:
    prof = (cfg.get("autoloop") or {}).get(name)
    if not prof:
        die(f"no autoloop profile '{name}' in .harness/config.json (the owner adds it)")
    for k in ("eval_cmd", "editable", "locked"):
        if not prof.get(k):
            die(f"profile '{name}' needs '{k}'")
    if prof.get("direction", "max") not in ("max", "min"):
        die(f"profile '{name}': direction must be \"max\" or \"min\", not {prof.get('direction')!r}")
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
          **profile_fields(prof), "locked_hashes": locked, "started": time.time(), "runs": 0, "since_keep": 0,
          "halted": None, "finished": None, "base_commit": sh(root, "git", "rev-parse", "HEAD")}
    purge_bytecode(root, st["editable"])
    status, score, ok, notes, secs = run_eval(root, st["eval_cmd"], st["time_budget_s"], eval_env(cfg))
    leftovers = dirty(root)
    if status != "ok" or not ok or leftovers:
        reset_to(root, st["base_commit"])
        sh(root, "git", "checkout", "-q", "-")
        sh(root, "git", "branch", "-q", "-D", st["branch"])
        if leftovers:
            die(f"the evaluator left files in the repo ({leftovers[:5]}) — make it write only to gitignored paths")
        die(f"baseline evaluation failed ({status}, ok={ok}): {notes} — fix the evaluator before looping")
    st.update(best=score, baseline=score, best_commit=st["base_commit"])
    save(root, st)
    append(root, st, [0, st["base_commit"][:8], score, score, "baseline", round(secs, 1), "baseline", notes])
    H.log_event(root, "autoloop_start", run=run_id, baseline=score, locked_files=len(locked))
    print(f"autoloop started: {run_id} on branch {st['branch']}\n  goal: {st['goal']}\n  baseline {st['direction']} "
          f"score = {score} ({secs:.0f}s)  · {len(locked)} locked files · budget {st['max_runs']} runs / {st['max_hours']} h\n"
          f"  editable: {', '.join(st['editable'])}\nNext: change ONLY editable files, then `run --desc \"<idea>\"`.")


def cmd_run(root: Path, cfg: dict, desc: str) -> None:
    st = load(root, cfg)
    if st.get("finished"):
        die("this autoloop is finished — start a new one")
    if st.get("halted"):
        die(f"HALTED ({st['halted']}) — the owner must look before anything else runs", 3)
    if sh(root, "git", "rev-parse", "--abbrev-ref", "HEAD") != st["branch"]:
        die(f"not on the loop branch {st['branch']}")
    why = budget_reason(st)
    if why:
        die(f"loop over ({why}) — run `report`, push the branch, open the PR, and stop", 10)
    n = st["runs"] + 1
    # 1. auto-commit the agent's working changes
    changed_now = dirty(root)
    out_now = [f for f in changed_now if not H.matches_any(f, st["editable"])]
    if changed_now and not out_now:
        sh(root, "git", "add", "-A", "--", *changed_now)
        sh(root, "git", "commit", "-q", "-m", f"autoloop #{n}: {desc[:120]}")
    head = sh(root, "git", "rev-parse", "HEAD")
    changed = sorted(f for f in set(sh(root, "git", "diff", "--name-only", st["best_commit"], head).splitlines()) | set(out_now)
                     if not noise(f))
    if not changed:
        die("nothing changed since the best commit — edit an editable file first")
    # 2. honesty: locked files untouched (on disk, committed or not)
    now = hash_locked(root, st["locked"])
    if now != st["locked_hashes"]:
        diff = sorted(set(now.items()) ^ set(st["locked_hashes"].items()))
        reset_to(root, st["best_commit"])
        st["halted"] = f"locked files changed: {[d[0] for d in diff][:5]}"
        save(root, st)
        append(root, st, [n, head[:8], "", st["best"], "tampered", 0, desc, f"locked files changed: {[d[0] for d in diff][:5]}"])
        H.log_event(root, "autoloop_tampered", run=st["run_id"], n=n, files=[d[0] for d in diff][:5])
        die("HALT: a locked evaluator/reference file changed. Reset done. Stop the loop and report this to the owner.", 3)
    # 3. scope
    out_scope = [f for f in changed if not H.matches_any(f, st["editable"])]
    st["runs"] = n
    st["since_keep"] = st.get("since_keep", 0) + 1
    gaming = gaming_tokens(root, st, head)
    if gaming and not out_scope:
        reset_to(root, st["best_commit"])
        save(root, st)
        append(root, st, [n, head[:8], "", st["best"], "gaming", 0, desc, f"diff adds forbidden token(s): {gaming}"])
        H.log_event(root, "autoloop_gaming", run=st["run_id"], n=n, tokens=gaming)
        print(f"#{n} DISCARDED (metric gaming: the change mentions {gaming}). Solutions must be general — "
              "never refer to specific evaluation files. Reset to best.")
        return
    if out_scope:
        reset_to(root, st["best_commit"])
        save(root, st)
        append(root, st, [n, head[:8], "", st["best"], "out_of_scope", 0, desc, f"non-editable files: {out_scope[:5]}"])
        H.log_event(root, "autoloop_out_of_scope", run=st["run_id"], n=n, files=out_scope[:5])
        print(f"#{n} DISCARDED (out of scope: {out_scope[:5]}). Only {st['editable']} may change. Reset to best.")
        return
    # 4. evaluate
    purge_bytecode(root, st["editable"])
    status, score, ok, notes, secs = run_eval(root, st["eval_cmd"], st["time_budget_s"], eval_env(cfg))
    leftovers = dirty(root)
    if leftovers:
        status, notes = "crash", f"the run left files in the repo: {leftovers[:5]}"
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
        st.update(best=score, best_commit=head, since_keep=0)
    else:
        reset_to(root, st["best_commit"])
    save(root, st)
    append(root, st, [n, head[:8], score, st["best"], verdict, round(secs, 1), desc, notes])
    left = min(st["max_runs"] - n, int((st["max_hours"] - (time.time() - st["started"]) / 3600) * 3600 / max(secs, 1)))
    print(f"#{n} {verdict.upper()}: score {score} (best {st['best']}, baseline {st['baseline']}) in {secs:.0f}s"
          f"{' — ' + notes if notes else ''}\n~{max(left, 0)} runs left; {st['since_keep']}/{st['patience']} "
          "without improvement. " + ("Build on this." if verdict.startswith("keep") else "Reset to best; try a different idea."))


def cmd_status(root: Path, cfg: dict) -> None:
    st = load(root, cfg)
    p = tsv_path(root, st)
    rows = p.read_text().splitlines()[1:] if p.exists() else []
    counts = {}
    for r in rows[1:]:
        s = r.split("\t")[4]
        counts[s] = counts.get(s, 0) + 1
    print(json.dumps({k: st.get(k) for k in ("run_id", "branch", "goal", "direction", "baseline", "best", "best_commit",
                                             "runs", "max_runs", "halted", "finished")} | {"by_status": counts}, indent=2))


def holdout_at(root: Path, cfg: dict, st: dict, commit: str) -> str:
    sh(root, "git", "checkout", "-q", "--detach", commit)
    try:
        status, score, ok, notes, _ = run_eval(root, st["holdout_cmd"], st["time_budget_s"], eval_env(cfg))
    finally:
        sh(root, "git", "checkout", "-q", st["branch"])
    return f"{score} (ok={ok}{', ' + status if status != 'ok' else ''}{'; ' + notes[:120] if notes else ''})"


def cmd_report(root: Path, cfg: dict) -> None:
    st = load(root, cfg)
    if st.get("finished"):
        die("already reported")
    if sh(root, "git", "rev-parse", "--abbrev-ref", "HEAD") != st["branch"]:
        die(f"check out the loop branch first: git checkout {st['branch']}  (report never touches other branches)")
    rows = [r.split("\t") for r in tsv_path(root, st).read_text().splitlines()[1:]]
    reset_to(root, st["best_commit"])  # never report from a half-applied attempt
    hold = None
    if st.get("holdout_cmd") and not st.get("halted"):
        base_h, best_h = holdout_at(root, cfg, st, st["base_commit"]), holdout_at(root, cfg, st, st["best_commit"])
        hold = f"base {base_h} → best {best_h}"
        if st["best_commit"] == st["base_commit"]:
            hold = f"{best_h} (no change kept)"
    counts = {}
    for r in rows:
        counts[r[4]] = counts.get(r[4], 0) + 1
    kept = [r for r in rows if r[4].startswith("keep")]
    hrs = (time.time() - st["started"]) / 3600
    lines = [f"# Autoloop {st['run_id']}", "",
             f"**Goal:** {st['goal']}  ", f"**Result:** {st['direction']} score {st['baseline']} → **{st['best']}** "
             f"after {st['runs']} runs in {hrs:.1f} h ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}).  ",
             f"**Holdout (never seen during the loop, scored once on base and best):** {hold or 'not configured'}  ",
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
    save(root, st)
    H.log_event(root, "autoloop_report", run=st["run_id"], baseline=st["baseline"], best=st["best"], runs=st["runs"],
                kept=len(kept), holdout=hold, halted=st.get("halted"))
    sh(root, "git", "add", "-f", str(out.relative_to(root)))
    for extra in (H.EVENTS_DIR, f"{H.HARNESS_DIR}/progress.md", f"{H.HARNESS_DIR}/lessons-inbox"):
        if (root / extra).exists():
            sh(root, "git", "add", extra)
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
    roots = H.candidate_roots({"cwd": os.getcwd()})
    root = roots[0] if len(roots) == 1 else H.find_root(None)
    if not root:
        die("no .harness/config.json above the current directory")
    cfg = H.load_config(root)
    {"start": lambda: cmd_start(root, cfg, a.profile), "run": lambda: cmd_run(root, cfg, a.desc),
     "status": lambda: cmd_status(root, cfg), "report": lambda: cmd_report(root, cfg)}[a.cmd]()


if __name__ == "__main__":
    main()
