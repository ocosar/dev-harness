"""dev-harness paid guard — the second lock on a paid entry point. /dev-harness:init copies this file to
.harness/paid_guard.py (a protected file) and wires ONE call into the repo's paid CLI path, right before the first
paid request, e.g. in pipeline.py:

    if args.pay:
        import runpy, pathlib
        runpy.run_path(str(pathlib.Path(__file__).resolve().parent / ".harness" / "paid_guard.py"))["require_approval"]()

The PreToolUse hook matches command text, which a variable (`P=--pay; python pipeline.py $P`), an alias or a wrapper
script can dodge. This check runs inside the program: it proceeds only if the owner approved THIS exact command
through the dev-harness prompt in the last 30 minutes (a request in status 'asked' whose command has the same script
and arguments), then marks that request 'consumed' so it can't be reused. Production paths (e.g. a web worker with its
own budget caps) don't call it. Standard library only.
"""
from __future__ import annotations

import datetime as _dt
import json
import shlex
import sys
from pathlib import Path


def _root(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if (d / ".harness" / "config.json").is_file():
            return d
    return None


def _age_min(ts: str) -> float:
    try:
        t = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
        return (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 60
    except ValueError:
        return float("inf")


def require_approval(argv: list[str] | None = None, max_age_min: float = 30) -> str:
    argv = list(sys.argv if argv is None else argv)
    root = _root(Path(__file__).resolve().parent)
    if root is None:
        sys.exit("dev-harness paid guard: .harness/config.json not found — refusing the paid call.")
    script = Path(argv[0]).name
    for f in sorted((root / ".harness" / "requests").glob("*.json"), reverse=True):
        try:
            req = json.loads(f.read_text())
            toks = shlex.split(str(req.get("command", "")))
        except (OSError, ValueError):
            continue
        if req.get("status") != "asked" or _age_min(req.get("asked_at", "")) > max_age_min:
            continue
        idx = next((i for i, t in enumerate(toks) if Path(t).name == script), None)
        if idx is not None and toks[idx + 1:] == argv[1:]:
            req.update(status="consumed", consumed_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
            f.write_text(json.dumps(req, indent=2, ensure_ascii=False) + "\n")
            return f.stem
    sys.exit("dev-harness paid guard: refused — the owner has not approved this exact command in the last "
             f"{max_age_min:g} minutes (no matching 'asked' request in .harness/requests/). Prepare a request "
             "(/dev-harness:paid-call) and run the command as written there, so the approval prompt appears.")


if __name__ == "__main__":
    print(require_approval(sys.argv[1:]))
