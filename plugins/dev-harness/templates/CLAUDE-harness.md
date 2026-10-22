<!-- dev-harness:start (managed by /dev-harness:init — edit the repo rules elsewhere in this file) -->
## dev-harness

This repo runs the dev-harness plugin. The owner is a non-coding product owner: they approve, agents build.

**Three human gates (enforced by hooks — never work around them, never try a different command to get past a deny):**
1. **Spec** — nothing is built until `.harness/features.json` has `"approved": true`, which only the owner's click can set.
2. **Paid call** — every call that spends money on an external API needs a prepared request in `.harness/requests/`, a verified estimate, room under the daily cap, and the owner's click. One approval = one call. Unattended sessions never pay.
3. **Merge** — agents never merge, never push to or commit on `main`/`master`. Work on `claude/<slug>` branches and open a PR.

**Session routine:** read the SessionStart briefing → `git log --oneline -10` → `.harness/progress.md` tail → pick ONE feature → build test-first → `.harness/check.sh` green → evaluator + reviewer subagents → commit → append to progress.md.

**State lives in the repo, not in chat:** spec in `docs/specs/<slug>/`, contract in `.harness/features.json` (agents may only change `status`, `passes`, `evidence`), experiments in the experiment log, money in `.harness/ledger.jsonl`.

**Truthfulness:** label claims FACT (checked, with source/command) / INFERENCE / UNKNOWN. Prices come from the provider's pricing API, never from memory or docs pages. A feature passes only with evidence a human can open.

**Report format (every stop):** what was done · commit hashes · review paths · USD spent today · what you need from the owner.
<!-- dev-harness:end -->
