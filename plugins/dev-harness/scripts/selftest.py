#!/usr/bin/env python3
"""Repo-level gate regression cases: .harness/cases.json

{
  "paid_must_match":     ["python pipeline.py --from a --to b --pay"],
  "paid_must_not_match": ["grep -n -- --pay pipeline.py", "python pipeline.py --dry-run"],
  "locked_must_match":   ["ilan-poc/tools/autoloop_eval.sh"]
}

Every false positive / false negative the owner reports becomes a case here (the retro skill adds them),
so a later config change can't silently bring the bug back. Exit 1 lists failing cases.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
from gate_pretool import is_paid_bash  # noqa: E402


def main() -> int:
    root = H.find_root(None)
    if not root:
        print("selftest: no .harness/config.json")
        return 0
    cfg = H.load_config(root)
    try:
        cases = json.loads((root / H.HARNESS_DIR / "cases.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"selftest: cannot read .harness/cases.json ({e})")
        return 1
    bad = []
    bad += [f"paid pattern MISSES (would run unapproved): {c}" for c in cases.get("paid_must_match", []) if not is_paid_bash(cfg, c)]
    bad += [f"paid pattern FALSE POSITIVE (blocks a free command): {c}" for c in cases.get("paid_must_not_match", []) if is_paid_bash(cfg, c)]
    locked = H.locked_patterns(cfg)
    bad += [f"path not locked: {p}" for p in cases.get("locked_must_match", []) if not H.matches_any(p, locked)]
    n = sum(len(cases.get(k, [])) for k in ("paid_must_match", "paid_must_not_match", "locked_must_match"))
    if bad:
        print(f"dev-harness selftest: {len(bad)}/{n} gate cases fail\n" + "\n".join(f"- {b}" for b in bad))
        return 1
    print(f"dev-harness selftest: {n} gate cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
