"""Shared helpers for dev-harness hooks. Standard library only; Python 3.9+ (macOS system python3 works)."""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HARNESS_DIR = ".harness"
UNATTENDED_MODES = {"dontAsk", "bypassPermissions"}
CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")

DEFAULT_CONFIG = {
    "version": 1,
    "language": "en",
    "timezone": "UTC",
    "paid": {
        "bash_patterns": [],
        "mcp_tools": [],
        "daily_cap_usd": 10.0,
        "extra_ledgers": [],
        "secret_env": [],
        "request_max_age_h": 12,
    },
    "protected_branches": ["main", "master"],
    "check": {"command": "bash .harness/check.sh", "timeout_s": 600, "max_consecutive_blocks": 3},
    "unattended_env": "HARNESS_UNATTENDED",
    "locked_paths": [],
    "autoloop": {},
}

# Files that steer the harness itself. Changing them needs the owner (Edit/Write → ask; shell → restored).
CONTROL_FILES = [".harness/config.json", ".harness/check.sh", ".harness/cases.json", ".harness/paid_guard.py",
                 # the harness itself when installed into the repo (install_project.py), and the settings that load it
                 ".claude/settings.json", ".claude/dev-harness/*", ".claude/skills/dh-*", ".claude/agents/dh-*"]
FEATURES = ".harness/features.json"


def read_stdin_json() -> dict:
    raw = sys.stdin.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


# ---------- finding the repo(s) a tool call touches ----------
def find_root(start: str | os.PathLike | None) -> Path | None:
    """Nearest ancestor of `start` (a dir or file) that has .harness/config.json."""
    p = Path(start or os.getcwd())
    try:
        p = p.resolve()
    except OSError:
        return None
    for d in [p, *p.parents]:
        if (d / HARNESS_DIR / "config.json").is_file():
            return d
    return None


def _children_with_harness(base: Path, depth: int = 2) -> list[Path]:
    out = []
    try:
        entries = [e for e in base.iterdir() if e.is_dir() and not e.name.startswith(".")
                   and e.name not in ("node_modules", "__pycache__", "venv", ".venv")]
    except OSError:
        return out
    for e in sorted(entries):
        if (e / HARNESS_DIR / "config.json").is_file():
            out.append(e.resolve())
        elif depth > 1:
            out += _children_with_harness(e, depth - 1)
    return out


CD_RE = re.compile(r"(?:^|[;&|(]\s*)(?:cd|pushd)\s+([^\s;&|)]+)")
GITC_RE = re.compile(r"\bgit\s+(?:-[^C\s]\S*\s+)*-C\s+([^\s;&|]+)")


def path_hints(payload: dict) -> list[str]:
    """Paths a tool call points at: file_path / notebook_path, `cd X`, `git -C X`."""
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    hints = []
    for k in ("file_path", "notebook_path", "path"):
        if ti.get(k):
            p = Path(str(ti[k]))
            hints.append(str(p if p.is_absolute() else Path(cwd) / p))
    cmd = ti.get("command") or ""
    for m in CD_RE.findall(cmd) + GITC_RE.findall(cmd):
        m = os.path.expanduser(m.strip("'\""))
        hints.append(m if os.path.isabs(m) else str(Path(cwd) / m))
    return hints


def candidate_roots(payload: dict) -> list[Path]:
    """Every harness repo this call may touch: from the paths it names, the cwd, CLAUDE_PROJECT_DIR, and — when
    the session sits ABOVE the repos (cloud sessions start in the parent folder) — harness repos below the cwd."""
    cwd = payload.get("cwd") or os.getcwd()
    roots: list[Path] = []
    for h in path_hints(payload) + [cwd, os.environ.get("CLAUDE_PROJECT_DIR") or ""]:
        if h:
            r = find_root(h if os.path.isdir(h) else os.path.dirname(h) or h)
            if r and r not in roots:
                roots.append(r)
    if not roots:
        for base in {Path(cwd), Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)}:
            for r in _children_with_harness(base):
                if r not in roots:
                    roots.append(r)
    return roots


# ---------- config ----------
def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(root: Path) -> dict:
    """A config that exists but doesn't parse is reported as `_broken` (gates then fail CLOSED), never as defaults."""
    p = root / HARNESS_DIR / "config.json"
    try:
        user = json.loads(p.read_text())
        if not isinstance(user, dict):
            raise ValueError("top level is not an object")
    except FileNotFoundError:
        return _merge(DEFAULT_CONFIG, {})
    except (OSError, ValueError) as e:
        cfg = _merge(DEFAULT_CONFIG, {})
        cfg["_broken"] = f"{type(e).__name__}: {e}"
        return cfg
    return _merge(DEFAULT_CONFIG, user)


def unattended(cfg: dict, payload: dict) -> bool:
    env_name = cfg.get("unattended_env") or "HARNESS_UNATTENDED"
    if os.environ.get(env_name, "").strip() not in ("", "0", "false"):
        return True
    return payload.get("permission_mode") in UNATTENDED_MODES


def tz(cfg: dict):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone") or "UTC")
    except Exception:
        return _dt.timezone.utc


def now_iso(cfg: dict | None = None) -> str:
    return _dt.datetime.now(tz(cfg or {})).isoformat(timespec="seconds")


def today(cfg: dict) -> str:
    return _dt.datetime.now(tz(cfg)).date().isoformat()


# ---------- money ----------
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


LEDGER_DIR = f"{HARNESS_DIR}/ledger"  # one file per branch (no merge conflicts); the cap sums them all


def ledger_path(root: Path) -> Path:
    return root / LEDGER_DIR / f"{branch_slug(root)}.jsonl"


def append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def spent_today(root: Path, cfg: dict) -> float:
    """Harness ledgers (every branch file present in this checkout) + repo ledgers ({path, ts_field, usd_field}),
    summed for today in the configured timezone. Naive timestamps are read as UTC.
    Limit: spend recorded only on other machines / unmerged branches is invisible here — provider-side limits
    are the real backstop."""
    day, zone = today(cfg), tz(cfg)
    sources = [{"path": p.relative_to(root).as_posix(), "ts_field": "ts", "usd_field": "usd"}
               for p in sorted((root / LEDGER_DIR).glob("*.jsonl"))]
    sources += cfg["paid"].get("extra_ledgers") or []
    total = 0.0
    for src in sources:
        for r in _rows(root / src["path"]):
            ts = str(r.get(src.get("ts_field", "ts"), ""))
            try:
                d = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                d = (d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).astimezone(zone)
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


def norm_cmd(cmd: str) -> str:
    try:
        return " ".join(shlex.split(cmd or ""))
    except ValueError:
        return " ".join((cmd or "").split())


def find_requests(root: Path, statuses: tuple) -> list[tuple[Path, dict]]:
    out = []
    for f in sorted(requests_dir(root).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("status") in statuses:
            out.append((f, data))
    return out


def request_age_h(req: dict) -> float:
    try:
        t = _dt.datetime.fromisoformat(str(req.get("created_at", "")).replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
        return (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 3600
    except ValueError:
        return float("inf")


def latest_request(root: Path, status: str = "prepared") -> tuple[Path, dict] | None:
    found = find_requests(root, (status,))
    return found[-1] if found else None


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


# ---------- self-improvement: event log ----------
# One file per branch: hooks append on whatever branch is checked out, so two branches never edit the same file
# (no merge conflicts for the owner). The retro reads .harness/events/*.jsonl.
EVENTS_DIR = f"{HARNESS_DIR}/events"


def branch_slug(root: Path) -> str:
    b = git(root, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", "detached" if b == "HEAD" else b).strip("-") or "unknown"


def events_path(root: Path) -> Path:
    return root / EVENTS_DIR / f"{branch_slug(root)}.jsonl"


def log_event(root: Path, kind: str, **data) -> None:
    """Append-only log the retro skill learns from. Never raises."""
    try:
        rec = {"ts": now_iso(load_config(root)), "kind": kind}
        rec.update({k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:400]) for k, v in data.items()})
        append_jsonl(events_path(root), rec)
    except Exception:
        pass


# ---------- protected paths ----------
def locked_patterns(cfg: dict) -> list[str]:
    pats = list(cfg.get("locked_paths") or [])
    for prof in (cfg.get("autoloop") or {}).values():
        if isinstance(prof, dict):
            pats += list(prof.get("locked") or [])
    return [p for p in pats if p]


def protected_patterns(cfg: dict) -> list[str]:
    return CONTROL_FILES + [FEATURES] + locked_patterns(cfg)


def matches_any(rel: str, patterns: list[str]) -> bool:
    """fnmatch-style; '*' also matches '/' (so 'dir/*' covers the whole subtree). Case-insensitive on macOS."""
    if CASE_INSENSITIVE_FS:
        rel, patterns = rel.lower(), [p.lower() for p in patterns]
    return any(fnmatch.fnmatchcase(rel, p) or rel == p.rstrip("/") for p in patterns)


def expand(root: Path, patterns: list[str]) -> list[str]:
    """Files under root matching patterns (incl. gitignored ones). Walks only below each pattern's literal prefix."""
    found = set()
    for pat in patterns:
        prefix = re.split(r"[*?\[]", pat, maxsplit=1)[0]
        base = root / prefix if (root / prefix).is_dir() else (root / prefix).parent
        if (root / pat).is_file():
            found.add(pat)
            continue
        if not base.is_dir():
            continue
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "node_modules")]
            for fn in files:
                rel = (Path(d) / fn).relative_to(root).as_posix()
                if matches_any(rel, [pat]):
                    found.add(rel)
    return sorted(found)


SMALL = 1_000_000


def snapshot(root: Path, cfg: dict | None = None, patterns: list[str] | None = None) -> dict:
    """{rel: {size, mtime, sha?, text?|b64?}} for protected files — cheap enough to take around every shell command.
    Small files keep their content so an unapproved change can be restored exactly."""
    import base64
    snap = {}
    for rel in expand(root, patterns if patterns is not None else protected_patterns(cfg or {})):
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            continue
        rec = {"size": st.st_size, "mtime": st.st_mtime_ns}
        if st.st_size <= SMALL:
            data = p.read_bytes()
            rec["sha"] = hashlib.sha256(data).hexdigest()
            if rel in CONTROL_FILES or rel == FEATURES:
                rec["text"] = data.decode("utf-8", "replace")
            else:
                rec["b64"] = base64.b64encode(data).decode()
        snap[rel] = rec
    return snap


def snapshot_diff(root: Path, before: dict, after: dict) -> list[str]:
    changed = []
    for rel in sorted(set(before) | set(after)):
        b, a = before.get(rel), after.get(rel)
        if b is None or a is None:
            changed.append(rel)
        elif "sha" in b and "sha" in a:
            if b["sha"] != a["sha"]:
                changed.append(rel)
        elif (b["size"], b["mtime"]) != (a["size"], a["mtime"]):
            changed.append(rel)
    return changed


# ---------- spec approval semantics ----------
def _strip_progress(d: dict) -> dict:
    feats = d.get("features") if isinstance(d.get("features"), list) else []
    return {**{k: v for k, v in d.items() if k != "features"},
            "features": [{k: v for k, v in f.items() if k not in ("status", "passes", "evidence")}
                         if isinstance(f, dict) else f for f in feats]}


def features_change_ok(before_text: str | None, after_text: str | None) -> tuple[bool, str]:
    """Agents may only move progress (status / passes / evidence) of an approved contract. Anything that
    approves, or changes an approved contract's scope, needs the owner."""
    try:
        after = json.loads(after_text or "")
    except ValueError:
        try:
            before_ok = bool(json.loads(before_text or "").get("approved"))
        except (ValueError, AttributeError):
            before_ok = False
        return (not before_ok, "makes an approved features.json unreadable")
    if not isinstance(after, dict) or after.get("approved") is not True:
        return True, ""
    try:
        before = json.loads(before_text or "")
    except ValueError:
        before = {}
    if not isinstance(before, dict) or before.get("approved") is not True:
        return False, "sets the spec approval"
    if _strip_progress(before) != _strip_progress(after):
        return False, "changes the scope of an approved spec (only status / passes / evidence may change)"
    return True, ""


def spec_sha(root: Path, feats: dict) -> str | None:
    """sha256 of the approved spec.md (+ plan.md next to it) — recorded at approval, re-checked before building."""
    spec = feats.get("spec")
    if not spec:
        return None
    p = root / spec
    h = hashlib.sha256()
    found = False
    for f in (p, p.parent / "plan.md"):
        if f.is_file():
            h.update(f.read_bytes())
            found = True
    return h.hexdigest()[:16] if found else None
