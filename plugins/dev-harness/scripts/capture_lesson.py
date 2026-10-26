#!/usr/bin/env python3
"""UserPromptSubmit: the owner's cheapest way to teach the harness.

Any line in a prompt that starts with `LESSON:` (also `Lesson:`, `DERS:`) is appended to
.harness/lessons-inbox.md with a timestamp. The retro skill later dedupes it into .harness/lessons.md
(which every session reads) or turns it into a harness change. The prompt itself is not altered.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

LESSON_RE = re.compile(r"^\s*(?:lesson|ders)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def main() -> None:
    payload = H.read_stdin_json()
    root = H.find_root(payload.get("cwd"))
    if not root:
        return
    found = [m.strip() for m in LESSON_RE.findall(payload.get("prompt") or "") if m.strip()]
    if not found:
        return
    inbox = root / H.HARNESS_DIR / "lessons-inbox.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    stamp = H._dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    new = not inbox.exists()
    with inbox.open("a") as f:
        if new:
            f.write("# Lessons inbox (raw, from the owner — the retro skill curates these into lessons.md)\n\n")
        for text in found:
            f.write(f"- {stamp} — {text}\n")
            H.log_event(root, "lesson_captured", text=text)
    H.emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": f"dev-harness: captured {len(found)} owner lesson(s) to "
                                                        ".harness/lessons-inbox.md. Apply them from now on in this session; "
                                                        "acknowledge in one short line."}})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
