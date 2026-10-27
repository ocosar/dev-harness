---
name: eval
description: Evals beyond Claude grading Claude — (1) an eval packet the owner can paste into any other model for a second opinion (default, free), (2) the same second opinion sent automatically through another provider's API (optional, paid, gated), (3) product evals — a fixed eval set scored against an owner-approved baseline with a pluggable judge model (optional). Use from /dev-harness:ship, or when the owner asks for a second opinion or an eval run.
argument-hint: "[packet | record <model> | api | product | baseline]"
---

Scripts: `${CLAUDE_PLUGIN_ROOT}/scripts/` (`eval_packet.py`, `second_opinion.py`, `product_eval.py`, `llm_client.py`). Settings live in `.harness/config.json → evals` (owner-approved; changing them prompts the owner).

## 1. Second opinion — manual (default, `evals.second_opinion.mode = "manual"`)
1. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/eval_packet.py" build` → `.harness/evals/<branch>/packet.md` (spec, acceptance criteria, evidence, diff, last check, fixed reviewer questions; secret-looking values redacted).
2. Tell the owner the path and, in one line, what to do: "Open it, copy everything, paste it into ChatGPT / Gemini / any model, and paste the answer back here." If the packet is very long, say which model handles long input (Gemini, or a long-context GPT).
3. When the owner pastes an answer back (any time, also in a later session): save it to a temp file and run `eval_packet.py record --model "<model name the owner used>" --file <tmp>`. It stores the verdict (PASS / CONCERNS / FAIL / UNPARSED).
4. Treat its findings like the reviewer's: verify each BLOCKER/SHOULD against the code before acting — another model can be wrong too. Fix real ones (builder), say in the PR which you rejected and why.

## 2. Second opinion — API (optional, `mode = "api"`)
Needs `provider` (openai | gemini | openrouter | anthropic), `model`, the key in an env var (`key_env`, default per provider; never read or print it), and `usd_per_mtok_in/out` from the provider's price page.
1. Dry run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/second_opinion.py"` → packet + cost estimate.
2. It is a PAID call: follow /dev-harness:paid-call for exactly `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/second_opinion.py" --send` (request with that command, `est_usd` from the dry run, `price_source` from it). One request, one click.
3. After it ran: close the request with `book.py <id> <actual_usd_from_usage> "<provider> usage tokens"`.
4. Handle findings as in 1.4. In a cloud sandbox the provider host may not be reachable (network allowlist) — then fall back to the manual packet and say so.

## 3. Product evals (optional, `evals.product.enabled`)
The repo's own command scores a FIXED eval set and prints a last line `{"score": …, "ok": …, "metrics": {per-case…}, "notes": …}`. A judge model is pluggable: the command receives `HARNESS_JUDGE_PROVIDER` / `HARNESS_JUDGE_MODEL` and can use `llm_client.call(...)` (or `llm_client.py … --send`), so the owner can swap Claude / GPT / Gemini as judge without code changes.
- Free eval: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/product_eval.py" run` → PASS/FAIL vs. `.harness/evals/baseline.json`, worst cases listed.
- Paid eval (`evals.product.paid = true`, e.g. real generations or a model judge): /dev-harness:paid-call for exactly `<evals.product.cmd> > .harness/evals/<branch>/product-out.txt`, then `product_eval.py compare --from .harness/evals/<branch>/product-out.txt`. Never run a paid eval set without the owner asking for it in this ship; give its estimate first.
- Baseline: first run has none. `product_eval.py propose-baseline` prints it; write it to `.harness/evals/baseline.json` with the Write tool — the owner's click approves it. Only move the baseline when the owner agrees the new level is the new normal (in the PR that changes it).
- A regression (score worse than baseline beyond `tolerance`, or `ok: false`) stops the ship: report the worst cases and stop.

## Rules
- Evals never replace the checks: `check.sh`, evaluator and reviewer still run.
- Never tune code to the eval set's specific cases (special-casing ids/names/expected values = metric gaming; the reviewer rejects it).
- Disagreement between Claude's reviewer and the second opinion is information for the owner: list both in the PR, don't hide one.
