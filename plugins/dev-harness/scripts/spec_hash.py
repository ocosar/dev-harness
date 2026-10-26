#!/usr/bin/env python3
"""Print the fingerprint of the spec named in .harness/features.json (spec.md + plan.md). The spec skill writes it
as `approved_spec_sha` in the same edit that sets `"approved": true`, so later spec edits are detectable."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

roots = H.candidate_roots({"cwd": os.getcwd()})
root = roots[0] if len(roots) == 1 else H.find_root(None)
if not root:
    sys.exit("no .harness/config.json here")
feats = json.loads((root / H.FEATURES).read_text())
print(H.spec_sha(root, feats) or "")
