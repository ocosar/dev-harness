#!/usr/bin/env python3
"""Record the ACTUAL cost of an executed paid request (deterministic bookkeeping for the paid-call skill).

usage: book.py <request-id> <actual_usd> "<billing source, e.g. x-fal-billable-units 60.13 × $0.05>"

If the harness auto-booked the estimate, this writes a correction row (actual − estimate); if the repo keeps its own
ledger (paid.extra_ledgers), nothing is added to the harness ledger (the repo's ledger already counts it) and only the
request file is closed. Prints today's total afterwards.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    rid, actual, source = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    roots = H.candidate_roots({"cwd": os.getcwd()})
    root = roots[0] if len(roots) == 1 else H.find_root(None)
    if not root:
        print("no .harness/config.json")
        return 2
    cfg = H.load_config(root)
    path = H.requests_dir(root) / f"{rid}.json"
    try:
        req = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        print(f"request {rid} not found in .harness/requests/")
        return 2
    if req.get("status") == "done":
        print(f"request {rid} is already booked (${req.get('actual_usd')})")
        return 2
    now = H._dt.datetime.now().astimezone().isoformat(timespec="seconds")
    est = float(req.get("est_usd") or 0)
    if not cfg["paid"].get("extra_ledgers"):
        if req.get("booked_estimate"):
            delta = round(actual - est, 6)
            if delta:
                H.append_jsonl(H.ledger_path(root), {"ts": now, "request": rid, "kind": "actual_adjust", "usd": delta,
                                                     "billing_source": source})
        else:
            H.append_jsonl(H.ledger_path(root), {"ts": now, "request": rid, "kind": "actual", "usd": actual,
                                                 "billing_source": source})
    req.update(status="done", actual_usd=actual, billing_source=source, booked_at=now)
    H.write_json(path, req)
    H.log_event(root, "paid_booked", request=rid, est=est, actual=actual)
    print(f"booked {rid}: ${actual:.4f} (estimate ${est:.4f}). Today: ${H.spent_today(root, cfg):.2f} / "
          f"cap ${float(cfg['paid'].get('daily_cap_usd', 0)):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
