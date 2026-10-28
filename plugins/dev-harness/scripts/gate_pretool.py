#!/usr/bin/env python3
"""PreToolUse gate for dev-harness.

Gate 1 (spec):  approving .harness/features.json, or changing the scope of an approved one, needs the owner's click.
Gate 2 (paid):  a Bash command / MCP tool matching the repo's paid patterns (or a shell script that contains one)
                needs (a) a prepared request naming EXACTLY this command, with a priced estimate, (b) room under the
                daily cap, (c) the owner's click. One request = one call; loops / several paid calls per command are
                refused. Unattended sessions never pay.
Gate 3 (merge): agents never merge PRs, push to / commit on protected branches (git, gh CLI/API, MCP tools).
Also: harness control files (config, check script, cases, paid guard) and locked evaluator/reference files need the
owner's click — via Edit/Write or via a shell command. A PostToolUse hook (post_tool.py) restores any such file a
shell command changed without approval.

It checks EVERY harness repo the call can reach (paths it names, cwd, CLAUDE_PROJECT_DIR, repos below the cwd), and
fails CLOSED when a repo's .harness/config.json is unreadable. It is a guard rail against accidents and drift, not a
sandbox; the hard backstops are keys absent from unattended runs, provider spend limits, and the owner's merge.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
HAND_EDIT_ASK = [".harness/state/*", ".harness/ledger/*"]  # written by the harness's own scripts only


def ask_or_deny(cfg: dict, payload: dict, reason: str) -> tuple[str, str]:
    if H.unattended(cfg, payload):
        return ("deny", reason + "\n\nUnattended session: this needs the owner. Report it and stop; do not retry "
                                 "another way.")
    return ("ask", reason)


# ---------- Gate 3: merge guard ----------
GIT_PREFIX = r"\bgit(?:\s+(?:-[Cc]\s+\S+|--[\w-]+(?:=\S+)?|-[a-zA-Z]))*\s+"
PUSH_RE = re.compile(GIT_PREFIX + r"push\b([^;&|\n]*)")
LOCAL_WRITE_RE = re.compile(GIT_PREFIX + r"(commit|merge|cherry-pick|revert|rebase|am|pull)\b")
COMMIT_RE = re.compile(GIT_PREFIX + r"commit\b([^;&|\n]*)")
SWITCH_RE = re.compile(GIT_PREFIX + r"(?:checkout|switch)\s+(?:-\S+\s+)*['\"]?([\w./-]+)")
GH_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\b|\bgh\s+api\b[^\n]*(?:/merges?\b|mergePullRequest|enablePullRequestAutoMerge)"
                         r"|api\.github\.com[^\n]*(?:/merges?\b)")
MCP_MERGE_RE = re.compile(r"(?i)merge")
MCP_PUSH_RE = re.compile(r"(?i)(push|create_or_update_file|update_ref|create_commit|commit|delete_branch|update_branch)")


def _tokens(s: str) -> list[str]:
    try:
        return shlex.split(s)
    except ValueError:
        return [t.strip("'\"") for t in s.split()]


def _branch_of(ref: str, current: str) -> str:
    ref = ref.lstrip("+")
    if ref in ("HEAD", "@", ""):
        return current
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


# merge: "ask" lets exactly these through to the owner's click — one PR, one plain command, no admin/auto-merge
PR_MERGE_CMD_RE = re.compile(r"^\s*(?:gh\s+pr\s+merge\s+#?(\d+)(?:\s+--(?:squash|merge|rebase|delete-branch|"
                             r"subject\s+'[^']*'|subject\s+\"[^\"]*\"|body\s+'[^']*'|body\s+\"[^\"]*\"|"
                             r"match-head-commit\s+\w+))*"
                             r"|gh\s+api\s+(?:-X\s+PUT\s+|--method\s+PUT\s+)?/?repos/[\w.-]+/[\w.-]+/pulls/(\d+)/merge"
                             r"(?:\s+(?:-X\s+PUT|--method\s+PUT|-f\s+merge_method=(?:squash|merge|rebase)))*)\s*$")


def merge_mode(cfg: dict) -> str:
    m = cfg.get("merge")
    if m == "auto":
        return "auto" if H.autonomy(cfg) == "autopilot" and not cfg.get("deploys_on_merge") else "ask"
    return "ask" if m == "ask" else "owner"


# ---------- assisted / autopilot: which free calls may skip Claude Code's own permission prompt ----------
# ALLOW-LIST, not a block-list: a command is auto-approved only when EVERY segment's verb is known-safe and the
# segment has no risky shape (secret read, write outside the repo, command substitution, interpreter inline code,
# a paid script). Anything else returns None → Claude Code's normal permission prompt still applies.
SAFE_VERBS = {
    "ls", "pwd", "echo", "printf", "cat", "head", "tail", "wc", "nl", "sort", "uniq", "cut", "tr", "comm", "column",
    "diff", "stat", "file", "basename", "dirname", "realpath", "readlink", "tree", "date", "true", "false", "test",
    "which", "cd", "mkdir", "touch", "grep", "egrep", "fgrep", "rg", "ag", "find", "fd", "jq", "yq",
    "pytest", "tox", "ruff", "flake8", "pylint", "black", "isort", "mypy", "pyright", "coverage",
}  # sed/awk/make left off: they can write, exec or run arbitrary repo targets — they fall through to the prompt
# NOTE (honest limit): test runners and package installs still execute repo code (tests, setup scripts). They stay
# auto-approved because they ARE the routine work; the backstops are the paid guard inside paid CLIs, paid keys kept
# out of unattended runs, and provider spend limits.
SAFE_GIT = {"status", "diff", "log", "show", "add", "commit", "branch", "checkout", "switch", "restore", "stash",
            "fetch", "rebase", "merge", "pull", "push", "cherry-pick", "revert", "tag", "describe", "rev-parse",
            "ls-files", "blame", "shortlog", "remote", "worktree"}  # push/merge/main already decided by merge_guard
SAFE_PKG_SUB = {"install", "ci", "test", "add", "sync", "list", "show", "why"}  # not run/exec/build: arbitrary scripts
SECRET_TOK = re.compile(r"(?i)\.env\b|/\.(?:ssh|aws|gnupg|netrc)|/\.config/(?:gcloud|gh)|(?:^|/)credentials\b"
                        r"|\$\{?[A-Za-z_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)|/proc/\d+/environ|id_(?:rsa|ed25519)")
SECRET_VERB = re.compile(r"(?i)^(?:printenv|env|set|export)$")
SUBST = re.compile(r"\$\(|`|<\(")


def _seg_safe(root: Path, cwd: str | None, seg: str) -> bool:
    seg = seg.strip()
    if not seg:
        return True
    if SUBST.search(seg) or SECRET_TOK.search(seg):
        return False
    if re.search(r"(?<![0-9&])>\s*(?!/dev/null\b)", seg):  # any redirect except >/dev/null → let the prompt handle it
        return False
    try:
        toks = shlex.split(seg)
    except ValueError:
        return False
    while toks and "=" in toks[0] and not toks[0].startswith("-"):  # leading VAR=val assignments
        toks = toks[1:]
    while toks and toks[0] in ("command", "nice", "time", "xargs"):  # harmless wrappers
        toks = toks[1:]
    if not toks or toks[0] in ("sudo", "su", "env", "doas"):  # privilege / env-injection front → not auto-allowed
        return False
    verb = toks[0].rsplit("/", 1)[-1]
    if SECRET_VERB.match(verb):
        return False
    if verb in ("find", "fd") and any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint") for t in toks):
        return False  # find can delete or run commands
    if any(t == "~" or t.startswith("~/") or t.startswith("../") or "/../" in t for t in toks[1:]):
        return False  # writes/reads reaching outside the repo → normal prompt
    rroot = str(root.resolve())
    for t_ in toks[1:]:
        if t_.startswith("/") and not (t_ == rroot or t_.startswith(rroot + "/") or t_.startswith("/tmp/") or t_ == "/dev/null"):
            return False  # absolute path outside the repo (and not /tmp) → normal prompt
    if verb in ("bash", "sh") and toks[1:] in ([".harness/check.sh"], [".harness/init.sh"]):
        return True  # owner-approved control files (changing them asks the owner)
    if verb in ("python", "python3", "node", "nodejs", "ruby", "perl", "bash", "sh", "zsh", "deno", "bun"):
        rest = toks[1:]
        if verb.startswith("python") and rest[:1] == ["-m"] and rest[1:2] in (["pytest"], ["coverage"], ["ruff"], ["mypy"], ["black"]):
            return True
        return False  # bare scripts and -c/-e inline → normal prompt (could do anything, incl. a paid call)
    if verb == "git":
        sub = next((t for t in toks[1:] if not t.startswith("-")), "")
        if sub == "push" and any(f in toks for f in ("--force", "-f", "--force-with-lease", "--delete", "--mirror", "--all", "--prune")):
            return False  # force/delete/bulk push → normal prompt even on a feature branch
        return sub in SAFE_GIT  # excludes config, update-index, hash-object, apply, fast-import, filter-branch
    if verb in ("pip", "pip3", "npm", "pnpm", "yarn", "uv", "poetry", "cargo", "go"):
        sub = next((t for t in toks[1:] if not t.startswith("-")), "")
        return sub in SAFE_PKG_SUB
    if verb in ("rm", "mv", "cp", "ln", "dd", "tee", "truncate", "chmod", "chown", "rmdir", "shred"):
        return False  # destructive / out-of-repo moves → normal prompt
    return verb in SAFE_VERBS


def auto_allow_reason(root: Path, cfg: dict, payload: dict, tool: str, ti: dict) -> str | None:
    """assisted+ : auto-approve ONLY a free, low-risk call on the allow-list above. Everything else returns None,
    so Claude Code's own permission prompt still applies — the harness never silently allows an unknown command."""
    if H.autonomy(cfg) == "manual":
        return None
    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        if not cmd.strip() or len(cmd) > 2000 or H.is_builtin_paid(cmd) or paid_matches(cfg, cmd):
            return None
        if not all(_seg_safe(root, payload.get("cwd"), s) for s in SEG_SPLIT.split(cmd)):
            return None
        return f"dev-harness {H.autonomy(cfg)}: allow-listed low-risk command auto-approved"
    if tool in FILE_TOOLS:
        rel = to_rel(root, payload.get("cwd"), ti.get("file_path") or ti.get("notebook_path") or "")
        if not rel or rel.startswith("..") or rel.startswith(".git/") \
                or H.matches_any(rel, [".env", ".env.*", "*/.env", "*.pem", "*.key"]):
            return None
        return f"dev-harness {H.autonomy(cfg)}: edit inside the repo auto-approved"
    return None


def merge_request(cmd: str) -> str | None:
    """PR number when cmd is exactly one allowed merge command, else None."""
    m = PR_MERGE_CMD_RE.match(cmd)
    return (m.group(1) or m.group(2)) if m else None


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def strip_data_heredocs(cmd: str) -> str:
    """Drop heredoc bodies that are only DATA written to a file (`cat > f <<'EOF'`, `tee f <<EOF`), so text that
    merely mentions a command (a progress note quoting a merge command) is not read as that command. A heredoc
    piped into anything (`| bash`) or fed to any other program keeps its body — that text may run."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m:
            head = line[:m.start()].strip().split()
            verb = head[0].rsplit("/", 1)[-1] if head else ""
            data_only = verb in ("cat", "tee") and "|" not in line
            j = i + 1
            while j < len(lines) and lines[j].strip() != m.group(2):
                if not data_only:
                    out.append(lines[j])
                j += 1
            if j < len(lines):
                out.append(lines[j])
            i = j + 1
            continue
        i += 1
    return "\n".join(out)


def merge_guard(root: Path, cfg: dict, cmd: str, cwd: str | None) -> str | None:
    cmd = strip_data_heredocs(cmd)
    protected = [b for b in cfg.get("protected_branches", []) if b]
    where = Path(cwd) if cwd and Path(cwd).is_dir() and H.find_root(cwd) == root else root
    if GH_MERGE_RE.search(cmd):
        if merge_mode(cfg) in ("ask", "auto") and merge_request(cmd):
            return None  # handled in evaluate(): owner prompt ("ask") or allowed ("auto")
        if merge_mode(cfg) in ("ask", "auto"):
            return ("GATE 3 — merging needs the owner's click, one PR per command. Run exactly `gh pr merge <number> "
                    "--squash` (or `gh api -X PUT repos/<owner>/<repo>/pulls/<number>/merge`) on its own — "
                    "no --admin, no --auto, nothing chained.")
        return "GATE 3 — merging is the owner's job. Open/update the PR and report; never merge it yourself."
    if protected and re.search(r"(?:gh\s+api|api\.github\.com)[^\n]*git/refs/heads/(" + "|".join(map(re.escape, protected))
                               + r")\b", cmd):
        return "GATE 3 — updating a protected branch through the GitHub API is the owner's job."
    current = H.git(where, "rev-parse", "--abbrev-ref", "HEAD")
    m = SWITCH_RE.search(cmd)
    if m and m.group(1).strip("'\"") in protected and (LOCAL_WRITE_RE.search(cmd[m.end():]) or PUSH_RE.search(cmd[m.end():])):
        return (f"GATE 3 — this command switches to protected branch '{m.group(1)}' and then changes it. "
                "Work on a feature branch and open a PR.")
    for pm in PUSH_RE.finditer(cmd):
        toks = _tokens(pm.group(1))
        if any(t in ("--all", "--mirror") for t in toks):
            return "GATE 3 — `git push --all/--mirror` is not allowed. Push only your feature branch."
        pos = [t for t in toks if not t.startswith("-")]
        refspecs = pos[1:]
        if not refspecs:
            if current in protected:
                return f"GATE 3 — you are on protected branch '{current}'. Create a feature branch first."
            upstream = H.git(where, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if upstream and upstream.split("/", 1)[-1] in protected:
                return (f"GATE 3 — this branch tracks '{upstream}', so a bare `git push` would update a protected "
                        "branch. Push explicitly: `git push -u origin HEAD:<your-feature-branch>`.")
        for spec in refspecs:
            src, colon, dst = spec.partition(":")
            target = _branch_of(dst if colon else src, current)
            if target in protected:
                return f"GATE 3 — pushing to protected branch '{target}' is the owner's job (via PR merge)."
    lw = LOCAL_WRITE_RE.search(cmd)
    if lw and current in protected:
        return (f"GATE 3 — `git {lw.group(1)}` on protected branch '{current}' is blocked. "
                "Work on a feature branch (e.g. claude/<slug>) and open a PR.")
    return None


def is_mcp_pr_merge(tool: str) -> bool:
    name = tool.split("__")[-1]
    return bool(re.search(r"(?i)^merge_?pull_?request$", name))


def mcp_merge_guard(cfg: dict, tool: str, ti: dict) -> str | None:
    protected = [b for b in cfg.get("protected_branches", []) if b]
    name = tool.split("__")[-1]
    if merge_mode(cfg) in ("ask", "auto") and is_mcp_pr_merge(tool):
        return None  # owner prompt / auto in evaluate()
    if MCP_MERGE_RE.search(name) and re.search(r"(?i)(pull|pr\b|_pr|branch)", name):
        return "GATE 3 — merging is the owner's job, also through MCP tools. Open/update the PR and report."
    if MCP_PUSH_RE.search(name):
        refs = [str(ti.get(k, "")) for k in ("branch", "ref", "base", "head", "target_branch", "branch_name")]
        if any(_branch_of(r, "") in protected for r in refs if r):
            return f"GATE 3 — writing to a protected branch ({', '.join(protected)}) through {tool} is the owner's job."
    return None


# ---------- protected files written through the shell (pre-check; post_tool.py is the backstop) ----------
SEG_SPLIT = re.compile(r"\|\||&&|;|\||\n")
REDIRECT = re.compile(r"(?<![0-9&<>])>>?\s*([^\s;&|<>]+)")
WRITE_VERBS_ALL_ARGS = {"rm", "mv", "tee", "truncate", "touch", "chmod", "ln", "shred", "unlink"}
WRITE_VERBS_LAST_ARG = {"cp", "install", "rsync"}
WRITE_HINTS = re.compile(r"""['"][wa]b?\+?['"]|write|dump\(|remove|unlink|rename|shutil|truncate|rmtree|\.replace\(|-i\b""")
INTERPRETERS = re.compile(r"^(python[0-9.]*|node|ruby|perl)\s+(-[a-zA-Z]*[ce]|-[a-zA-Z]*i)\b")


def to_rel(root: Path, cwd: str | None, token: str) -> str | None:
    token = os.path.expanduser(token.strip("'\""))
    if not token or token.startswith("-") or token.startswith("/dev/"):
        return None
    p = Path(token)
    p = p if p.is_absolute() else Path(cwd or root) / p
    try:
        return Path(os.path.normpath(p)).relative_to(root).as_posix()
    except ValueError:
        return None


def shell_write_targets(root: Path, cfg: dict, cmd: str, cwd: str | None) -> list[str]:
    """Repo-relative protected paths that `cmd` may write (best effort, errs toward asking)."""
    # Spend ledger and paid-request files: the harness's own scripts append to them, but a shell command that
    # removes, truncates or rewrites one would wipe the daily-cap total or rewind a request → ask up front.
    pats = H.protected_patterns(cfg) + [".harness/ledger/*", ".harness/requests/*"]
    hit = set()
    for seg in SEG_SPLIT.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        cands = REDIRECT.findall(seg)
        words = seg.split()
        while words and (("=" in words[0] and not words[0].startswith("-")) or words[0] in ("sudo", "env", "command")):
            words = words[1:]
        if not words:
            continue
        verb = words[0].split("/")[-1]
        args = [w for w in words[1:] if not w.startswith("-")]
        if verb in WRITE_VERBS_ALL_ARGS or (verb == "sed" and re.search(r"\s-[a-zA-Z]*i|--in-place", seg)) \
                or (verb == "git" and args and args[0] in ("checkout", "restore", "rm", "mv", "apply")) \
                or (verb == "dd" and any(a.startswith("of=") for a in words)):
            cands += [a[3:] if a.startswith("of=") else a for a in args]
        elif verb in WRITE_VERBS_LAST_ARG and args:
            cands.append(args[-1])
        elif INTERPRETERS.search(" ".join(words)) and WRITE_HINTS.search(seg):
            cands += re.findall(r"[\w./-]*\.[\w]+|[\w.-]+/[\w./-]*", seg)  # inline code that writes: any path it names
        for c in cands:
            rel = to_rel(root, cwd, c)
            if not rel:
                continue
            if H.matches_any(rel, pats):
                hit.add(rel)
            elif (root / rel).is_dir() and H.matches_any(f"{rel}/x", pats):
                hit.add(rel + "/")  # a whole protected directory
    return sorted(hit)


# ---------- harness self-update: one owner click for the whole install ----------
INSTALL_RE = re.compile(r"^\s*python[0-9.]*\s+(?:-B\s+)?(\S*install_project\.py)\s+(\S+)\s*$")
INSTALL_TARGETS = [".claude/settings.json", ".claude/dev-harness/*", ".claude/skills/dev-harness-*",
                   ".claude/skills/dh-*", ".claude/agents/dev-harness-*", ".claude/agents/dh-*",
                   ".harness/paid_guard.py", ".harness/check.sh"]  # the installer only renames commands in these two


def harness_update(root: Path, cmd: str, cwd: str | None) -> str | None:
    """The installer path if `cmd` is exactly one run of install_project.py into this repo, else None."""
    m = INSTALL_RE.match(cmd)
    if not m:
        return None
    installer = Path(os.path.expanduser(m.group(1).strip("'\"")))
    if not installer.is_absolute():
        installer = Path(cwd or root) / installer
    target = Path(os.path.expanduser(m.group(2).strip("'\"")))
    if not target.is_absolute():
        target = Path(cwd or root) / target
    try:
        inside = str(installer.resolve()).startswith(str(root.resolve()) + os.sep)
        # The installer must be a fresh checkout OUTSIDE the repo. A repo-local install_project.py is not trusted
        # for the one-click whole-install approval (it would otherwise self-grant a rewrite of .claude/settings.json);
        # it falls through to the ordinary per-file control-file prompts instead.
        return m.group(1) if (target.resolve() == root.resolve() and not inside) else None
    except OSError:
        return None


def commit_touches_protected(root: Path, cfg: dict, cmd: str, cwd: str | None) -> list[str]:
    m = COMMIT_RE.search(cmd)
    if not m:
        return []
    names = H.git(root, "diff", "--cached", "--name-only").splitlines()
    if re.search(r"(^|\s)(--all|-[a-zA-Z]*a[a-zA-Z]*)(\s|$)", m.group(1)):
        names += H.git(root, "diff", "--name-only").splitlines()
    pats = H.CONTROL_FILES + H.locked_patterns(cfg)
    hits = {n for n in names if H.matches_any(n, pats)}
    return sorted(hits - _approved_install_content(root, hits, staged=set(H.git(root, "diff", "--cached", "--name-only").splitlines())))


def _approved_install_content(root: Path, names: set, staged: set) -> set:
    """Names whose to-be-committed content is exactly what an owner-approved HARNESS UPDATE wrote (≤ 12 h ago)."""
    import hashlib
    import json
    import subprocess
    import time
    try:
        rec = json.loads((root / H.HARNESS_DIR / "state" / "approved_install.json").read_text())
        if time.time() - float(rec.get("at", 0)) > 12 * 3600:
            return set()
        files = rec.get("files") or {}
    except (OSError, ValueError):
        return set()
    ok = set()
    for n in names:
        if n not in files:
            continue
        if n in staged:
            r = subprocess.run(["git", "show", f":{n}"], cwd=root, capture_output=True)
            data = r.stdout if r.returncode == 0 else None   # None = staged deletion
        else:
            f = root / n
            data = f.read_bytes() if f.is_file() else None
        sha = hashlib.sha256(data).hexdigest() if data is not None else None
        if sha == files[n]:
            ok.add(n)
    return ok


# ---------- Gate 2: paid calls ----------
LOOP_RE = re.compile(r"\b(for|while|until)\b[^\n]*\bdo\b|\bxargs\b|\bparallel\b|\bwatch\b")


def paid_matches(cfg: dict, text: str) -> int:
    return sum(len(re.findall(p, text)) for p in H.paid_patterns(cfg))


def is_paid_bash(cfg: dict, cmd: str) -> bool:
    return paid_matches(cfg, cmd) > 0


def is_paid_mcp(cfg: dict, tool: str) -> bool:
    return any(re.fullmatch(p, tool) for p in cfg["paid"].get("mcp_tools", []) if p)


def paid_script(root: Path, cfg: dict, cmd: str, cwd: str | None) -> str | None:
    """A shell script run by `cmd` that itself contains a paid command (write-a-script-then-run-it)."""
    for tok in re.findall(r"[^\s;&|'\"]+\.(?:sh|bash|zsh)\b", cmd):
        rel = to_rel(root, cwd, tok)
        f = root / rel if rel else None
        try:
            if f and f.is_file() and f.stat().st_size < 400_000 and is_paid_bash(cfg, f.read_text(errors="ignore")):
                return rel
        except OSError:
            continue
    return None


def paid_gate(root: Path, cfg: dict, payload: dict, what: str, match_key: str) -> tuple[str, str]:
    cap = float(cfg["paid"].get("daily_cap_usd", 0) or 0)
    if cap <= 0:
        return ("deny", "GATE 2 — the daily cap is 0, so paid calls are switched off in this repo. "
                        "Only the owner changes that (.harness/config.json → paid.daily_cap_usd).")
    spent = H.spent_today(root, cfg)
    max_age = float(cfg["paid"].get("request_max_age_h", 12))
    prepared = [(p, r) for p, r in H.find_requests(root, ("prepared",)) if H.request_age_h(r) <= max_age]
    found = [(p, r) for p, r in prepared if H.norm_cmd(str(r.get("command", ""))) == H.norm_cmd(match_key)]
    if not found:
        others = "; ".join(f"{p.stem}: `{r.get('command', '(no command)')}`" for p, r in prepared[-3:])
        return ("deny", "GATE 2 — paid call blocked: no prepared request for EXACTLY this command.\n"
                        f"This command: `{match_key[:300]}`\n" + (f"Prepared requests: {others}\n" if others else "") +
                        "Run the /dev-harness:paid-call procedure: write .harness/requests/<id>.json with status "
                        "'prepared', the exact `command`, summary, payload summary, est_usd from the provider's pricing "
                        f"API, price_source and created_at (requests older than {max_age:g} h are ignored). "
                        "One request covers ONE call.")
    path, req = found[-1]
    try:
        est = float(req.get("est_usd"))
    except (TypeError, ValueError):
        est = 0.0
    if est <= 0:
        return ("deny", f"GATE 2 — request {path.stem} has no positive est_usd. Verify the price first.")
    now = H.now_iso(cfg)
    if spent + est > cap + 1e-9:
        req.update(status="blocked_cap", blocked_at=now)
        H.write_json(path, req)
        return ("deny", f"GATE 2 — would exceed the daily cap: spent ${spent:.2f} + estimate ${est:.2f} > cap ${cap:.2f}. "
                        "Raising the cap is the owner's decision (.harness/config.json). Report and stop.")
    feats = H.load_features(root)
    sha = feats.get("approved_spec_sha")
    if H.autonomy(cfg) == "autopilot" and feats.get("approved") and sha and sha == H.spec_sha(root, feats):
        budget = float(feats.get("paid_budget_usd") or 0)
        max_call = float(feats.get("max_call_usd") or budget)
        used = H.envelope_spent(root, sha)
        if budget > 0 and est <= max_call + 1e-9 and used + est <= budget + 1e-9:
            req.update(status="asked", asked_at=now, auto=True, spec_sha=sha)
            H.write_json(path, req)
            return ("allow", f"dev-harness autopilot: paid call inside the budget you approved with the spec "
                             f"(${used:.2f} + ${est:.2f} of ${budget:.2f}; max ${max_call:.2f}/call; today ${spent:.2f} "
                             f"/ cap ${cap:.2f}). Request {path.stem}.")
    if H.unattended(cfg, payload):
        req.update(status="awaiting_approval", parked_at=now)
        H.write_json(path, req)
        return ("deny", f"GATE 2 — unattended session: paid calls never run without the owner. Request {path.stem} "
                        "is parked as 'awaiting_approval'. Put it in your report (summary, estimate, review paths) and stop.")
    req.update(status="asked", asked_at=now, spec_sha=sha if feats.get("approved") else None)
    H.write_json(path, req)
    return ("ask", "GATE 2 — PAID CALL\n"
                   f"{req.get('summary', '(no summary)')}\n"
                   f"Estimate: ${est:.2f}  ({req.get('price_source', 'price source missing!')})\n"
                   f"Today so far: ${spent:.2f} / cap ${cap:.2f}\n"
                   f"Command: {what[:300]}\n"
                   f"Request: {path.stem} — approves this ONE call only. Say No if the command doesn't match the summary.")


# ---------- file tools ----------
def _after_text(p: Path, tool: str, ti: dict) -> str | None:
    if tool == "Write":
        return ti.get("content", "")
    try:
        text = p.read_text()
    except OSError:
        text = ""
    edits = ti.get("edits") if tool == "MultiEdit" else [ti]
    for e in edits or []:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        if old not in text:
            return None  # the edit will fail on its own
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def file_guard(root: Path, cfg: dict, payload: dict, tool: str, ti: dict) -> tuple[str, str] | None:
    fp = ti.get("file_path") or ti.get("notebook_path") or ""
    if not fp:
        return None
    rel = to_rel(root, payload.get("cwd"), fp)
    if not rel or rel.startswith(".."):
        return None
    p = root / rel
    if H.matches_any(rel, [H.FEATURES]):
        try:
            before = p.read_text()
        except OSError:
            before = None
        after = _after_text(p, tool, ti)
        if after is None:
            return None
        ok, why = H.features_change_ok(before, after)
        if not ok and why == "sets the spec approval" and H.autonomy(cfg) == "autopilot":
            try:
                new = json.loads(after)
            except ValueError:
                new = {}
            if H.is_free_spec(new) and new.get("approved_spec_sha") == H.spec_sha(root, new):
                H.log_event(root, "spec_auto_approved", spec=new.get("spec"))
                return ("allow", "dev-harness autopilot: a FREE spec (no paid features, no budget) approves itself; "
                                 "the owner sees it in the report and the PR.")
        if not ok:
            return ask_or_deny(cfg, payload, f"GATE 1 — SPEC: this edit {why}. Approve only if you read and accept the "
                                             "spec and feature list as they will be after this edit.")
        return None
    if H.matches_any(rel, H.CONTROL_FILES):
        return ask_or_deny(cfg, payload, f"HARNESS CONTROL FILE — {rel} (caps, paid patterns, checks, locked "
                                         "evaluators, autoloop profiles). Only the owner changes these — approve only "
                                         "if you asked for exactly this.")
    if H.matches_any(rel, H.locked_patterns(cfg)):
        return ask_or_deny(cfg, payload, f"LOCKED FILE — {rel} is a frozen evaluator / reference. Agents may not change "
                                         "how results are measured. Approve only if YOU want the measurement changed.")
    if H.matches_any(rel, HAND_EDIT_ASK):
        return ask_or_deny(cfg, payload, f"{rel} is written by the harness's own scripts (book.py, autoloop.py). "
                                         "Hand edits change spend records or loop state — approve only if you asked.")
    if H.matches_any(rel, [".harness/requests/*.json"]) and p.is_file():
        # Creating a new request is free; rewinding a spent request to 'prepared' to re-approve a paid call is not.
        SPENT = {"asked", "executed", "done", "consumed", "blocked_cap"}
        after = _after_text(p, tool, ti)
        try:
            old_status = json.loads(p.read_text()).get("status")
            new_status = json.loads(after).get("status") if after is not None else old_status
        except (OSError, ValueError):
            old_status, new_status = None, None
        if old_status in SPENT and new_status not in SPENT:
            return ask_or_deny(cfg, payload, f"PAID REQUEST REUSE — {rel} is '{old_status}' (already approved/charged) "
                                             f"and this edit sets it back to '{new_status}', which would let the same "
                                             "paid call run again. One request = one call; write a NEW request instead. "
                                             "Approve only if you truly mean to re-charge this.")
    return None


# ---------- autoloop: sealed holdout ----------
def holdout_guard(root: Path, cmd: str) -> str | None:
    import json
    try:
        st = json.loads((root / H.HARNESS_DIR / "state" / "autoloop.json").read_text())
    except (OSError, ValueError):
        return None
    hc = st.get("holdout_cmd") or ""
    if st.get("finished") or not hc or "autoloop.py" in cmd:
        return None
    key = [t for t in _tokens(hc) if "/" in t or t.endswith(".py")]
    if H.norm_cmd(hc) in H.norm_cmd(cmd) or (key and key[0] in cmd):
        return ("AUTOLOOP — the holdout is sealed while the loop runs (it is scored once, by `autoloop.py report`). "
                "Use the tuning evaluator's notes instead.")
    return None


# ---------- per-repo evaluation ----------
def evaluate(root: Path, payload: dict) -> list[tuple[str, str, dict]]:
    """[(decision, reason, extra)] for one repo; extra carries owner-approved protected targets for post_tool."""
    cfg = H.load_config(root)
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd")
    if cfg.get("_broken"):
        d, r = ask_or_deny(cfg, payload, f"HARNESS CONFIG BROKEN — {root.name}/.harness/config.json can't be read "
                                         f"({cfg['_broken']}), so no gate can be checked. Fix the file first (the owner "
                                         "approves the fix); until then every tool call needs the owner.")
        return [(d, r, {})]
    out = []
    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        cds = H.CD_RE.findall(cmd)  # `cd proj && …`: resolve relative paths where the command actually runs
        if cds:
            first = os.path.expanduser(cds[0].strip("'\""))
            cwd = first if os.path.isabs(first) else str(Path(cwd or root) / first)
        reason = merge_guard(root, cfg, cmd, cwd) or holdout_guard(root, cmd)
        if reason:
            return [("deny", reason, {})]
        pr = merge_request(cmd) if merge_mode(cfg) in ("ask", "auto") else None
        if pr and merge_mode(cfg) == "auto":
            return [("allow", f"dev-harness autopilot: auto-merge of PR #{pr} (merge: auto)", {})]
        if pr:
            d, r = ask_or_deny(cfg, payload, f"GATE 3 — MERGE PR #{pr}. Command: {cmd.strip()}\n"
                                             "Approve only if you have read the PR and want it on main now "
                                             "(if main deploys automatically, this click is also the deploy).")
            return [(d, r, {})]
        inst = harness_update(root, cmd, cwd)
        if inst:
            d, r = ask_or_deny(cfg, payload, f"HARNESS UPDATE — `{cmd.strip()}` rewrites the harness itself in "
                                             f"{root.name}/.claude/ (hooks, skills, agents, settings). One click approves "
                                             "this install only; other protected files stay protected. Approve only if "
                                             "you asked for this update and trust the installer's source.")
            return [(d, r, {"owner_ok": list(INSTALL_TARGETS)})]
        script = paid_script(root, cfg, cmd, cwd)
        scan = strip_data_heredocs(cmd)  # text only written to a file (cat/tee heredoc) is not a call; scripts are
        n_paid = paid_matches(cfg, scan)  # still caught when RUN (paid_script reads the .sh it runs)
        if n_paid > 1 or ((n_paid or script) and LOOP_RE.search(scan)):
            return [("deny", "GATE 2 — one approval covers ONE paid call. This command runs several (or a loop). "
                             "Prepare one request per call and run them one by one.", {})]
        if n_paid or script:
            what = cmd + (f"   [runs {script}, which contains a paid command]" if script else "")
            d, r = paid_gate(root, cfg, payload, what, cmd)
            out.append((d, r, {}))
        targets = shell_write_targets(root, cfg, cmd, cwd)
        committed = commit_touches_protected(root, cfg, cmd, cwd)
        if targets or committed:
            names = sorted(set(targets) | set(committed))
            d, r = ask_or_deny(cfg, payload, f"PROTECTED FILES — this command may change or commit {', '.join(names[:4])} "
                                             "(harness control, spec contract, or a locked evaluator/reference). "
                                             "Approve only if YOU asked for exactly this.")
            out.append((d, r, {"owner_ok": names}))
        if not out and H.autonomy(cfg) != "manual" and (SECRET_TOK.search(cmd) or any(
                SECRET_VERB.match(w.rsplit("/", 1)[-1]) for w in cmd.split()[:1])):
            d, r = ask_or_deny(cfg, payload, "SECRETS — this command may read or print secret values (.env, environment, "
                                             "key variables). With fewer prompts on, the harness asks you for these "
                                             "explicitly. Approve only if you want this.")
            out.append((d, r, {}))
        if not out:
            why = auto_allow_reason(root, cfg, payload, tool, ti)
            if why:
                out.append(("allow", why, {}))
    elif tool.startswith("mcp__"):
        reason = mcp_merge_guard(cfg, tool, ti)
        if reason:
            return [("deny", reason, {})]
        if merge_mode(cfg) == "auto" and is_mcp_pr_merge(tool):
            return [("allow", "dev-harness autopilot: auto-merge (merge: auto)", {})]
        if merge_mode(cfg) == "ask" and is_mcp_pr_merge(tool):
            d, r = ask_or_deny(cfg, payload, f"GATE 3 — MERGE PR via {tool}: {str(ti)[:300]}\n"
                                             "Approve only if you have read the PR and want it on main now.")
            return [(d, r, {})]
        if is_paid_mcp(cfg, tool):
            d, r = paid_gate(root, cfg, payload, f"{tool} {str(ti)[:300]}", tool)
            out.append((d, r, {}))
    elif tool in FILE_TOOLS:
        res = file_guard(root, cfg, payload, tool, ti)
        if res:
            out.append((res[0], res[1], {}))
        else:
            why = auto_allow_reason(root, cfg, payload, tool, ti)
            if why:
                out.append(("allow", why, {}))
    return out


def main() -> None:
    if H.yield_to_repo_copy():
        return
    payload = H.read_stdin_json()
    roots = H.candidate_roots(payload)
    if not roots:
        return
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    summary = ti.get("command") or ti.get("file_path") or str(ti)[:200]
    results = []
    for root in roots:
        for d, r, extra in evaluate(root, payload):
            results.append((root, d, r, extra))
            if d != "allow" or "auto-approved" not in r:   # routine auto-approvals are not logged (noise)
                H.log_event(root, f"gate_{d}", tool=tool, input=summary, reason=r.splitlines()[0] if r else "")
    denies = [x for x in results if x[1] == "deny"]
    asks = [x for x in results if x[1] == "ask"]
    allows = [x for x in results if x[1] == "allow"]
    # allow only when every harness root that saw the call agrees (no root denied/asked, each root allowed)
    if not denies and not asks and allows and len({str(x[0]) for x in allows}) == len(roots):
        final = allows[:1]
    else:
        final = denies[:1] or asks
    if tool == "Bash" and not denies:  # snapshot protected files so post_tool.py can undo unapproved shell changes
        tid = payload.get("tool_use_id") or "last"
        for root in roots:
            cfg = H.load_config(root)
            if cfg.get("_broken"):
                continue
            owner_ok = sorted({t for rr, _, _, ex in asks if rr == root for t in ex.get("owner_ok", [])})
            H.write_json(root / H.HARNESS_DIR / "state" / "pre" / f"{tid}.json",
                         {"head": H.git(root, "rev-parse", "HEAD"), "owner_ok": owner_ok,
                          "patterns": H.protected_patterns(cfg), "snap": H.snapshot(root, cfg)})
    if not final:
        return
    decision = final[0][1]
    reason = "\n\n".join(x[2] for x in final) if decision == "ask" else final[0][2]
    H.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail visible, never silently open: a human sees the prompt
        H.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                       "permissionDecisionReason": f"dev-harness gate error ({type(e).__name__}: {e}). "
                                                                   "Review this action manually."}})
