#!/usr/bin/env python3
"""UserPromptSubmit: the owner's cheapest way to teach the harness.

Any line in a prompt that starts with `LESSON:` (any case) is appended to .harness/lessons-inbox/<branch>.md
(one file per branch, so branches never conflict) with a timestamp. The retro skill later dedupes it into .harness/lessons.md
(which every session reads) or turns it into a harness change. The prompt itself is not altered.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

LESSON_RE = re.compile(r"^\s*lesson\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def main() -> None:
    if H.yield_to_repo_copy():
        return
    payload = H.read_stdin_json()
    found = [m.strip() for m in LESSON_RE.findall(payload.get("prompt") or "") if m.strip()]
    roots = H.candidate_roots(payload) if found else []
    if not roots:
        return
    files = [capture(root, found) for root in roots]
    H.emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": f"dev-harness: captured {len(found)} owner lesson(s) to "
                                                        f"{', '.join(files)}. Commit with your next commit. Apply them "
                                                        "from now on in this session; acknowledge in one short line."}})


def capture(root, found) -> str:
    inbox = root / H.HARNESS_DIR / "lessons-inbox" / f"{H.branch_slug(root)}.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    stamp = H.now_iso(H.load_config(root))[:16].replace("T", " ")
    new = not inbox.exists()
    with inbox.open("a") as f:
        if new:
            f.write("# Lessons inbox (raw, from the owner — the retro skill curates these into lessons.md)\n\n")
        for text in found:
            f.write(f"- {stamp} — {text}\n")
            H.log_event(root, "lesson_captured", text=text)
    return f"{root.name}/{inbox.relative_to(root)}"


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
