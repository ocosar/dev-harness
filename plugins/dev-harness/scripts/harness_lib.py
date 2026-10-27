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
    # Gate 3 mode: "owner" = the owner merges on GitHub; "ask" = Claude may merge ONE PR per click in the session;
    # "auto" = (autopilot only, never for repos that deploy on merge) Claude merges a green PR itself
    "merge": "owner",
    # How much Claude may do without asking:
    #   "manual"    — every Claude Code permission prompt + all three gates (default)
    #   "assisted"  — free, low-risk tool calls are auto-approved (no permission prompts); gates unchanged
    #   "autopilot" — assisted + free specs approve themselves, paid calls run inside the budget the owner approved
    #                 with the spec (paid_budget_usd / max_call_usd in features.json), merge "auto" allowed
    "autonomy": "manual",
    # true if merging to the default branch deploys (Railway/Pages from main…): then merge "auto" acts like "ask"
    "deploys_on_merge": False,
    "check": {"command": "bash .harness/check.sh", "timeout_s": 600, "max_consecutive_blocks": 3},
    "unattended_env": "HARNESS_UNATTENDED",
    "locked_paths": [],
    "autoloop": {},
    # Evals beyond Claude's own evaluator/reviewer. 1) manual second opinion: an eval packet the owner pastes into
    # any other model (default, free). 2) api second opinion: the harness sends the packet itself (paid, gated).
    # 3) product evals: a fixed eval set + score vs. a baseline, with a pluggable judge model.
    "evals": {
        "second_opinion": {
            "mode": "manual",          # "manual" | "api" | "off"
            "required": False,         # true → a FAIL verdict stops /dev-harness:ship
            "provider": "openai",      # api mode: openai | gemini | openrouter | anthropic
            "model": "",
            "key_env": "",             # env var holding the key (default per provider)
            "usd_per_mtok_in": 0,      # from the provider's price page (needed for the estimate)
            "usd_per_mtok_out": 0,
            "max_output_tokens": 2000,
            "max_diff_chars": 60000,
        },
        "product": {
            "enabled": False,
            "cmd": "",                 # prints a last line {"score": .., "ok": .., "metrics": {..}}
            "direction": "max",        # or "min"
            "tolerance": 0,            # allowed worsening vs. baseline before it counts as a regression
            "paid": False,             # true if cmd spends money (e.g. a model judge) → run it through Gate 2
            "est_usd": 0,
            "judge": {"provider": "", "model": ""},   # passed to cmd as HARNESS_JUDGE_PROVIDER / _MODEL
            "timeout_s": 3600,
        },
    },
}

# Harness tools that spend money themselves — always gated like the repo's own paid commands.
BUILTIN_PAID_PATTERNS = [r"\b(?:llm_client|second_opinion)\.py\b[^;&|\n]*\s--send\b"]


def paid_patterns(cfg: dict) -> list[str]:
    pats = [p for p in (cfg.get("paid", {}).get("bash_patterns") or []) if p] + BUILTIN_PAID_PATTERNS
    pe = (cfg.get("evals") or {}).get("product") or {}
    if pe.get("enabled") and pe.get("paid") and (pe.get("cmd") or "").strip():
        pats.append(re.escape(pe["cmd"].strip()))   # a paid product eval is gated like any paid command
    return pats


def is_builtin_paid(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in BUILTIN_PAID_PATTERNS)


EVALS_DIR = f"{HARNESS_DIR}/evals"
EVAL_BASELINE = f"{EVALS_DIR}/baseline.json"


def autonomy(cfg: dict) -> str:
    a = cfg.get("autonomy")
    return a if a in ("manual", "assisted", "autopilot") else "manual"


def load_features(root: Path) -> dict:
    try:
        d = json.loads((root / FEATURES).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def envelope_spent(root: Path, sha: str) -> float:
    """What autopilot has run inside this approved spec's budget so far (actual if booked, else estimate).
    Calls the owner clicked through individually are their own approval and don't use the budget."""
    total = 0.0
    for _, r in find_requests(root, ("asked", "executed", "done", "consumed")):
        if r.get("spec_sha") == sha and r.get("auto"):
            v = r.get("actual_usd") if r.get("status") == "done" else r.get("est_usd")
            try:
                total += float(v or 0)
            except (TypeError, ValueError):
                pass
    return round(total, 6)


def evals_dir(root: Path) -> Path:
    return root / EVALS_DIR / branch_slug(root)

# Files that steer the harness itself. Changing them needs the owner (Edit/Write → ask; shell → restored).
CONTROL_FILES = [".harness/config.json", ".harness/check.sh", ".harness/cases.json", ".harness/paid_guard.py",
                 # the harness itself when installed into the repo (install_project.py), and the settings that load it
                 ".claude/settings.json", ".claude/dev-harness/*", ".claude/skills/dev-harness-*",
                 ".claude/agents/dev-harness-*",
                 # the product-eval baseline: moving it is the owner's call
                 ".harness/evals/baseline.json"]
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


def yield_to_repo_copy() -> bool:
    """True when this is the PLUGIN copy and the project runs its own installed copy (install_project.py) —
    then the plugin stays silent so every hook doesn't run twice. The repo copy is the one the repo pins."""
    here = Path(__file__).resolve().parent
    proj = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    for root in [proj] + list(proj.parents)[:3]:
        own = root / ".claude" / "dev-harness" / "scripts"
        if own.is_dir():
            if here == own.resolve():
                return False
            try:
                return ".claude/dev-harness/scripts/gate_pretool.py" in (root / ".claude" / "settings.json").read_text()
            except OSError:
                return False
    return False


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


def is_free_spec(feats: dict) -> bool:
    items = feats.get("features") if isinstance(feats.get("features"), list) else []
    return bool(items) and not any(isinstance(f, dict) and f.get("paid") for f in items) \
        and not float(feats.get("paid_budget_usd") or 0)


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
