#!/usr/bin/env python3
"""Second opinion, step 1 — the eval packet: one self-contained Markdown file that ANY other model (ChatGPT, Gemini,
a different Claude…) can review without access to the repo. Free; nothing leaves the machine unless the owner pastes
it somewhere (or second_opinion.py --send sends it, which is a gated paid call).

usage:
  eval_packet.py build [--base main]          → .harness/evals/<branch>/packet.md (prints the path)
  eval_packet.py record --model "<name>" [--file verdict.txt] [--source manual|api]
                                              → saves the verdict (stdin if no --file), prints PASS/CONCERNS/FAIL
  eval_packet.py status                       → verdicts recorded on this branch

The packet ends with fixed questions and asks for a last line `VERDICT: PASS | CONCERNS | FAIL`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

VERDICT_RE = re.compile(r"(?im)^\W*VERDICT\W*[:\-]?\s*\**\s*(PASS|CONCERNS|FAIL)\b")
SECRETISH = re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization)\s*[=:]\s*)(['\"]?)[^\s'\"]{8,}")

QUESTIONS = """## Your task (reviewer)

You are an independent reviewer from a different model family. You did not write this change. Be concrete and brief.

1. **Requirement fit** — does the diff do what the spec and acceptance criteria say? Anything missing or extra?
2. **Correctness** — bugs, edge cases, error handling, anything that would break in production.
3. **Tests & evidence** — do the tests actually prove the acceptance criteria, or could they pass while the feature is broken?
4. **Cost & safety** — could this spend money unexpectedly, leak secrets, or weaken a check/guard?
5. **Unverified claims** — statements in the spec/PR that the diff and evidence do not support.

List findings as `BLOCKER:`, `SHOULD:` or `NIT:` with file:line where possible. If you cannot judge something from
the packet, say so instead of guessing.

End with exactly one line: `VERDICT: PASS` (ship it), `VERDICT: CONCERNS` (shippable, but the owner should read the
findings) or `VERDICT: FAIL` (has a BLOCKER).
"""


def root_or_die() -> Path:
    roots = H.candidate_roots({"cwd": os.getcwd()})
    root = roots[0] if len(roots) == 1 else H.find_root(None)
    if not root:
        sys.exit("no .harness/config.json here")
    return root


def redact(text: str) -> str:
    return SECRETISH.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)


def _read(p: Path, limit: int) -> str:
    try:
        t = p.read_text(errors="replace")
    except OSError:
        return ""
    return t if len(t) <= limit else t[:limit] + f"\n… [truncated, {len(t) - limit} more chars]\n"


def build(root: Path, base: str) -> Path:
    cfg = H.load_config(root)
    so = cfg["evals"]["second_opinion"]
    max_diff = int(so.get("max_diff_chars") or 60000)
    parts = [f"# Eval packet — {root.name} @ {H.branch_slug(root)}", "",
             f"Generated {H.now_iso(cfg)} by dev-harness. Base: `{base}`. Head: `{H.git(root, 'rev-parse', '--short', 'HEAD')}`.", ""]
    try:
        feats = json.loads((root / H.FEATURES).read_text())
    except (OSError, ValueError):
        feats = {}
    spec_path = feats.get("spec")
    if spec_path:
        parts += ["## Spec", f"(`{spec_path}`)", "", _read(root / spec_path, 20000), ""]
    if feats.get("features"):
        parts += ["## Features and acceptance criteria", ""]
        for f in feats["features"]:
            parts.append(f"### {f.get('id')} — {f.get('title', '')}  (status: {f.get('status')}, passes: {f.get('passes')})")
            for c in f.get("acceptance", []) or []:
                parts.append(f"- {c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)}")
            if f.get("evidence"):
                parts.append(f"- evidence: {f.get('evidence')}")
            parts.append("")
    mb = H.git(root, "merge-base", base, "HEAD") or base
    stat = H.git(root, "diff", "--stat", mb, "HEAD")
    log = H.git(root, "log", "--oneline", f"{mb}..HEAD")
    import subprocess
    try:
        diff = subprocess.run(["git", "diff", mb, "HEAD", "--", ".", ":(exclude).harness/events/*",
                               ":(exclude).harness/ledger/*", ":(exclude).harness/evals/*"],
                              cwd=root, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        diff = ""
    if len(diff) > max_diff:
        diff = diff[:max_diff] + f"\n… [diff truncated at {max_diff} chars; full stat above]\n"
    parts += ["## Commits", "```", log or "(none)", "```", "", "## Diff stat", "```", stat or "(empty)", "```", "",
              "## Diff", "```diff", diff or "(empty)", "```", ""]
    check = root / H.HARNESS_DIR / "state" / "check.json"
    if check.is_file():
        parts += ["## Last harness check", "```json", _read(check, 3000), "```", ""]
    parts.append(QUESTIONS)
    out = H.evals_dir(root) / "packet.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(redact("\n".join(parts)))
    H.log_event(root, "eval_packet", chars=out.stat().st_size, base=base)
    return out


def parse_verdict(text: str) -> str:
    found = VERDICT_RE.findall(text)
    return found[-1].upper() if found else "UNPARSED"


def record(root: Path, model: str, text: str, source: str) -> str:
    cfg = H.load_config(root)
    verdict = parse_verdict(text)
    d = H.evals_dir(root)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "model"
    (d / f"verdict-{slug}.md").parent.mkdir(parents=True, exist_ok=True)
    (d / f"verdict-{slug}.md").write_text(text)
    H.append_jsonl(d / "verdicts.jsonl", {"ts": H.now_iso(cfg), "model": model, "source": source, "verdict": verdict,
                                         "head": H.git(root, "rev-parse", "--short", "HEAD"),
                                         "file": f"verdict-{slug}.md"})
    H.log_event(root, "second_opinion", model=model, source=source, verdict=verdict)
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--base", default="origin/main")
    r = sub.add_parser("record")
    r.add_argument("--model", required=True)
    r.add_argument("--file")
    r.add_argument("--source", default="manual", choices=["manual", "api"])
    sub.add_parser("status")
    a = ap.parse_args()
    root = root_or_die()
    if a.cmd == "build":
        base = a.base
        if not H.git(root, "rev-parse", "--verify", "--quiet", base):
            base = "main" if H.git(root, "rev-parse", "--verify", "--quiet", "main") else "HEAD~1"
        print(build(root, base))
    elif a.cmd == "record":
        text = Path(a.file).read_text() if a.file else sys.stdin.read()
        if not text.strip():
            sys.exit("empty verdict")
        print(record(root, a.model, text, a.source))
    else:
        rows = list(H._rows(H.evals_dir(root) / "verdicts.jsonl"))
        print("\n".join(f"{x['ts']}  {x['model']:<24} {x['source']:<6} {x['verdict']}  (head {x.get('head')})"
                        for x in rows) or "no second-opinion verdicts on this branch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
