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
    if H.yield_to_repo_copy():
        return
    payload = H.read_stdin_json()
    roots = H.candidate_roots(payload)
    if not roots:
        return
    text = "\n\n".join(brief(r) for r in roots)
    H.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def brief(root) -> str:
    cfg = H.load_config(root)
    hd = root / H.HARNESS_DIR
    if cfg.get("_broken"):
        return (f"# dev-harness: {root} — CONFIG UNREADABLE ({cfg['_broken']}). Every gated tool call needs the owner "
                "until .harness/config.json is fixed. Tell the owner first.")
    lines = [f"# dev-harness is active in {root}",
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
        if feats.get("approved") and feats.get("approved_spec_sha") != H.spec_sha(root, feats):
            lines.append("**SPEC CHANGED SINCE APPROVAL** (spec.md/plan.md differ from what the owner approved) — "
                         "do not build; show the owner the diff and ask for re-approval.")
        for f in [f for f in items if not f.get("passes")][:5]:
            lines.append(f"- {f.get('id')} [{f.get('status', 'todo')}] {f.get('title')}"
                         + ("  (PAID)" if f.get("paid") else ""))
    except (OSError, json.JSONDecodeError):
        lines.append("\n## No .harness/features.json yet — start with /dev-harness:spec.")
    spent = H.spent_today(root, cfg)
    lines.append(f"\n## Spend today: ${spent:.2f} / cap ${float(cfg['paid'].get('daily_cap_usd', 0)):.2f}")
    for path, req in H.find_requests(root, ("awaiting_approval",))[-5:]:
        lines.append(f"Parked paid request waiting for the owner: {path.stem} — {req.get('summary', '')} "
                     f"(${req.get('est_usd')}). If the owner is here, show it; with their yes, set status back to "
                     "'prepared' with a fresh created_at and run the exact command (the prompt asks them).")
    try:
        les = [ln for ln in (hd / "lessons.md").read_text().splitlines() if ln.strip().startswith("- ")]
        if les:
            lines.append("\n## Lessons for this repo (.harness/lessons.md) — follow them")
            lines += les[:30]
    except OSError:
        pass
    inbox = []
    for f in sorted((hd / "lessons-inbox").glob("*.md")) + [hd / "lessons-inbox.md"]:
        try:
            inbox += [ln for ln in f.read_text().splitlines() if ln.startswith("- ")]
        except OSError:
            pass
    try:
        if inbox:
            lines.append(f"\n## {len(inbox)} uncurated owner lesson(s) in .harness/lessons-inbox/ — apply them too:")
            lines += inbox[-10:]
    except OSError:
        pass
    try:
        al = json.loads((hd / "state" / "autoloop.json").read_text())
        if not al.get("finished"):
            lines.append(f"\n## Autoloop ACTIVE: {al.get('run_id')} on {al.get('branch')} — best {al.get('best')} "
                         f"(baseline {al.get('baseline')}), {al.get('runs')}/{al.get('max_runs')} runs"
                         + (f" — HALTED: {al['halted']}" if al.get("halted") else "")
                         + ". Continue with /dev-harness:autoloop continue.")
    except (OSError, json.JSONDecodeError):
        pass
    try:
        prog = (hd / "progress.md").read_text().strip().splitlines()
        lines.append("\n## Latest progress (.harness/progress.md, tail)")
        lines += prog[-15:]
    except OSError:
        pass
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
