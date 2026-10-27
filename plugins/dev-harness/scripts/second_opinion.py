#!/usr/bin/env python3
"""Second opinion, step 2 (optional, evals.second_opinion.mode = "api"): send the eval packet to another model
family through its API and record the verdict next to Claude's own review.

usage:
  second_opinion.py            → dry run: builds the packet, prints provider/model/size and the cost ESTIMATE to put
                                 in the paid request (no key read, nothing sent)
  second_opinion.py --send     → the real call. PAID: the harness gate requires a prepared request for exactly this
                                 command (/dev-harness:paid-call), the daily cap, and the owner's click.

Prices come from evals.second_opinion.usd_per_mtok_in/out (the provider's price page); without them there is no
estimate and the gate refuses the call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
import eval_packet as P  # noqa: E402
import llm_client as L  # noqa: E402


def main() -> int:
    send = "--send" in sys.argv[1:]
    base = next((x.split("=", 1)[1] for x in sys.argv[1:] if x.startswith("--base=")), "origin/main")
    root = P.root_or_die()
    cfg = H.load_config(root)
    so = cfg["evals"]["second_opinion"]
    provider, model = so.get("provider") or "", so.get("model") or ""
    if not provider or not model:
        sys.exit("set evals.second_opinion.provider and .model in .harness/config.json first (owner approves)")
    if not H.git(root, "rev-parse", "--verify", "--quiet", base):
        base = "main" if H.git(root, "rev-parse", "--verify", "--quiet", "main") else "HEAD~1"
    packet = P.build(root, base)
    prompt = packet.read_text()
    est = L.estimate_usd(len(prompt), 0, int(so.get("max_output_tokens") or 2000),
                         float(so.get("usd_per_mtok_in") or 0), float(so.get("usd_per_mtok_out") or 0))
    if not send:
        print(json.dumps({"dry_run": True, "provider": provider, "model": model, "packet": str(packet),
                          "packet_chars": len(prompt), "est_usd": est,
                          "price_source": f"config evals.second_opinion: ${so.get('usd_per_mtok_in')}/M in, "
                                          f"${so.get('usd_per_mtok_out')}/M out (owner-entered from the price page)",
                          "next": "prepare a paid request for exactly `python3 <scripts>/second_opinion.py --send`"
                                  if est > 0 else "set usd_per_mtok_in/out first — no estimate, no call"}, indent=2))
        return 0
    try:
        text, usage = L.call(provider, model, prompt, max_tokens=int(so.get("max_output_tokens") or 2000),
                             key_env=so.get("key_env") or "")
    except RuntimeError as e:
        H.log_event(root, "second_opinion_error", model=model, error=str(e)[:300])
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    verdict = P.record(root, f"{provider}:{model}", text, "api")
    actual = None
    if usage.get("in") is not None and usage.get("out") is not None:
        actual = round(usage["in"] / 1e6 * float(so.get("usd_per_mtok_in") or 0)
                       + usage["out"] / 1e6 * float(so.get("usd_per_mtok_out") or 0), 6)
    print(json.dumps({"verdict": verdict, "usage": usage, "actual_usd_from_usage": actual,
                      "verdicts": str(H.evals_dir(root)),
                      "next": "close the request: book.py <request-id> <actual_usd_from_usage> \"<provider> usage tokens\""},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
