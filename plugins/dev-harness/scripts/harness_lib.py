"""Shared helpers for dev-harness hooks. Standard library only (runs on macOS python3 and cloud sessions)."""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import subprocess
from pathlib import Path

HARNESS_DIR = ".harness"
UNATTENDED_MODES = {"dontAsk", "bypassPermissions"}

DEFAULT_CONFIG = {
    "version": 1,
    "language": "en",
    "paid": {
        "bash_patterns": [],
        "mcp_tools": [],
        "daily_cap_usd": 10.0,
        "extra_ledgers": [],
    },
    "protected_branches": ["main", "master"],
    "check": {"command": "bash .harness/check.sh", "timeout_s": 600, "max_consecutive_blocks": 3},
    "unattended_env": "HARNESS_UNATTENDED",
}


def read_stdin_json() -> dict:
    import sys

    raw = sys.stdin.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def find_root(cwd: str | None) -> Path | None:
    """Nearest ancestor of cwd that has .harness/config.json."""
    p = Path(cwd or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / HARNESS_DIR / "config.json").is_file():
            return d
    return None


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(root: Path) -> dict:
    try:
        user = json.loads((root / HARNESS_DIR / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        user = {}
    return _merge(DEFAULT_CONFIG, user)


def unattended(cfg: dict, payload: dict) -> bool:
    env_name = cfg.get("unattended_env") or "HARNESS_UNATTENDED"
    if os.environ.get(env_name, "").strip() not in ("", "0", "false"):
        return True
    return payload.get("permission_mode") in UNATTENDED_MODES


def today_local() -> str:
    return _dt.datetime.now().astimezone().date().isoformat()


def _rows(path: Path):
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return


def spent_today(root: Path, cfg: dict) -> float:
    """Harness ledger + any repo ledgers ({path, ts_field, usd_field}) for today's local date.
    ts values are matched on their date prefix (UTC 'Z' timestamps are converted to local time)."""
    day = today_local()
    sources = [{"path": f"{HARNESS_DIR}/ledger.jsonl", "ts_field": "ts", "usd_field": "usd"}]
    sources += cfg["paid"].get("extra_ledgers") or []
    total = 0.0
    for src in sources:
        for r in _rows(root / src["path"]):
            ts = str(r.get(src.get("ts_field", "ts"), ""))
            try:
                d = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                d = d.astimezone() if d.tzinfo else d
                ok = d.date().isoformat() == day
            except ValueError:
                ok = ts.startswith(day)
            if ok:
                try:
                    total += float(r.get(src.get("usd_field", "usd"), 0) or 0)
                except (TypeError, ValueError):
                    pass
    return round(total, 4)


def requests_dir(root: Path) -> Path:
    return root / HARNESS_DIR / "requests"


def latest_request(root: Path, status: str = "prepared") -> tuple[Path, dict] | None:
    best = None
    for f in sorted(glob.glob(str(requests_dir(root) / "*.json"))):
        try:
            data = json.loads(Path(f).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") == status:
            best = (Path(f), data)
    return best


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def pretool_decision(decision: str, reason: str) -> None:
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                 "permissionDecisionReason": reason}})
