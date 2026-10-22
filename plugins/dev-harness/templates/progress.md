# Progress log (dev-harness)

Append-only. One entry per session, newest at the bottom. Every new session reads the tail of this file first.

Format:
## YYYY-MM-DD HH:MM — <feature id or task> — <branch>
- Done: …
- Evidence: <review paths / test output summary>
- Spent: $x.xx (paid requests: <ids>)
- Next: …
- Needs owner: … (or "nothing")
