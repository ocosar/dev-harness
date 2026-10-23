#!/usr/bin/env python3
"""SessionStart: brief every new / resumed / compacted session from the repo's own state files,
so no human has to re-explain the project (the 'Ozan as message bus' problem)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402


def main() -> None:
    payload = H.read_stdin_json()
    root = H.find_root(payload.get("cwd"))
    if not root:
        return
    cfg = H.load_config(root)
    hd = root / H.HARNESS_DIR
    lines = ["# dev-harness is active in this repo",
             "Gates: (1) spec approval, (2) every paid API call (request file + owner click + daily cap), "
             "(3) merge stays with the owner. Hooks enforce these; do not look for workarounds.",
             f"Write everything new (chat replies, specs, reports, PR text, commit messages, progress.md) in "
             f"language '{cfg.get('language', 'en')}'; leave existing docs in their own language. "
             "Report format: what was done · commit hashes · "
             "review paths · USD spent · what you need from the owner."]
    try:
        feats = json.loads((hd / "features.json").read_text())
        items = feats.get("features", [])
        done = sum(1 for f in items if f.get("passes"))
        lines.append(f"\n## Spec: {feats.get('spec', '?')} — approved: {bool(feats.get('approved'))}; "
                     f"features passing {done}/{len(items)}")
        for f in [f for f in items if not f.get("passes")][:5]:
            lines.append(f"- {f.get('id')} [{f.get('status', 'todo')}] {f.get('title')}"
                         + ("  (PAID)" if f.get("paid") else ""))
    except (OSError, json.JSONDecodeError):
        lines.append("\n## No .harness/features.json yet — start with /dev-harness:spec.")
    spent = H.spent_today(root, cfg)
    lines.append(f"\n## Spend today: ${spent:.2f} / cap ${float(cfg['paid'].get('daily_cap_usd', 0)):.2f}")
    parked = H.latest_request(root, "awaiting_approval")
    if parked:
        lines.append(f"Parked paid request awaiting the owner: {parked[0].name} — {parked[1].get('summary', '')}")
    try:
        prog = (hd / "progress.md").read_text().strip().splitlines()
        lines.append("\n## Latest progress (.harness/progress.md, tail)")
        lines += prog[-15:]
    except OSError:
        pass
    H.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
