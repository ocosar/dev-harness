---
name: paid-call
description: Gate 2. Prepare, get approval for, run and book exactly ONE call that spends money on an external API (video/image generation, paid LLM/API, etc.). Use whenever any step needs a paid call.
argument-hint: "<what the call is for>"
---

Purpose of the call: $ARGUMENTS

The PreToolUse hook blocks any command/tool matching the repo's paid patterns unless the steps below were done. Do not try alternative commands, scripts or tools to get around a deny — report instead.

1. **Do everything free first.** Run the repo's dry-run / mock path for this exact call. Build the review artefacts the repo's CLAUDE.md requires (input strips, guide frames, prompt, payload). Paths go in the request.
2. **Verify the price now** from the provider's pricing API or official pricing page fetched today (e.g. fal pricing API). Never from memory. If the provider bills by actual units, estimate the maximum.
3. **Write the request file** `.harness/requests/<YYYYMMDD-HHMMSS>-<slug>.json`:
   ```json
   {
     "id": "<same as file stem>",
     "status": "prepared",
     "summary": "One line the owner understands: what this call produces and why it is worth the money",
     "variable": "the one thing that differs from the previous run (for experiments)",
     "endpoint": "provider/model/endpoint",
     "payload_summary": "key parameters, durations, resolutions, number of outputs",
     "est_usd": 0.00,
     "price_source": "e.g. fal pricing API 2026-10-22 14:05, $0.30/s × 10 s",
     "review_paths": ["review/... strips, prompt, payload"],
     "created_at": "<ISO timestamp>"
   }
   ```
4. **Show the owner** (configured language, default English): the summary, the review paths (they should look at the frames/prompt), estimate + source, today's spend vs cap. Then run the paid command. The hook shows them a Yes/No prompt — that click is the approval. One request = one call; a retry or variant needs a new request.
5. **If denied or unattended:** leave the request as is, report it under "needs owner", stop.
6. **After the call:** append one line to `.harness/ledger.jsonl`:
   `{"ts": "<ISO with timezone>", "request": "<id>", "endpoint": "...", "usd": <actual>, "usd_estimate": <est>, "billing_source": "e.g. x-fal-billable-units × unit price", "note": "..."}`
   and set the request's `status` to `done` with `actual_usd`. If the repo keeps its own ledger, it keeps writing it too — the harness counts both.
7. Save outputs as review artefacts, evaluate them (evaluator subagent), and report: result, actual cost, evidence paths, recommendation — the decision on what to keep is the owner's.
