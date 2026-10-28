#!/usr/bin/env python3
"""Install dev-harness INTO a repo (no plugin system needed — for Claude Code cloud sessions, where
"Plugins aren't available in this environment").

    python3 install_project.py /path/to/repo        # install or update (idempotent)

What it writes in the target repo (commit it via a PR; it takes effect in the NEXT session):
  .claude/dev-harness/scripts/    hook + helper scripts (stdlib Python)
  .claude/dev-harness/templates/  templates used by /dev-harness-init
  .claude/dev-harness/VERSION     source commit
  .claude/skills/dev-harness-<name>/       the skills, as /dev-harness-init, /dev-harness-spec, /dev-harness-build, /dev-harness-paid-call, /dev-harness-experiment,
                                  /dev-harness-autoloop, /dev-harness-retro, /dev-harness-ship, /dev-harness-status
  .claude/agents/dev-harness-<name>.md     planner, builder, evaluator, reviewer (as dev-harness-planner, …)
  .claude/settings.json           hooks merged in (other settings kept; old dev-harness hooks replaced)

All of these are protected by the harness itself once .harness/config.json exists (an agent can't quietly
switch the gates off by editing them).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "plugins" / "dev-harness"
ROOT_VAR = "${CLAUDE_PROJECT_DIR}/.claude/dev-harness"


def rewrite(text: str) -> str:
    text = text.replace("${CLAUDE_PLUGIN_ROOT}", ROOT_VAR)
    text = text.replace("/dev-harness:", "/dev-harness-").replace("`dev-harness:", "`dev-harness-")
    return text


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1]).resolve()
    if not (repo / ".git").exists():
        print(f"{repo} is not a git repo")
        return 2
    claude = repo / ".claude"
    dest = claude / "dev-harness"
    try:  # the copy being replaced decides which files its own gate lets this one install touch
        prev = tuple(int(x) for x in re.search(r"(\d+)\.(\d+)\.(\d+)", (dest / "VERSION").read_text()).groups())
    except (OSError, AttributeError):
        prev = None
    if dest.exists():
        shutil.rmtree(dest)
    for sub in ("scripts", "templates"):
        shutil.copytree(SRC / sub, dest / sub, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in list((dest / "scripts").glob("*.py")) + [x for x in (dest / "templates").rglob("*") if x.is_file()]:
        f.write_text(rewrite(f.read_text()))
    (dest / ".gitignore").write_text("__pycache__/\n")
    notices = SRC.parents[1] / "THIRD_PARTY_NOTICES.md"
    if notices.is_file():  # license notice for instructions adapted from other projects travels with the copy
        shutil.copy2(notices, dest / "THIRD_PARTY_NOTICES.md")
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=SRC, capture_output=True, text=True).stdout.strip()
    version = json.loads((SRC / ".claude-plugin" / "plugin.json").read_text())["version"]
    (dest / "VERSION").write_text(f"dev-harness {version} ({sha or 'unknown'}) — installed by install_project.py\n")

    old_skills = [x for g in ("dh-*", "dev-harness-*") for x in (claude / "skills").glob(g)] if (claude / "skills").exists() else []
    for old in old_skills:  # dh-* = names used by 0.3.1
        shutil.rmtree(old)
    for skill in sorted((SRC / "skills").iterdir()):
        text = rewrite((skill / "SKILL.md").read_text())
        text = text.replace(f"name: {skill.name}\n", f"name: dev-harness-{skill.name}\n", 1)
        if skill.name == "retro":
            text += ("\n## Installed into the repo (no plugin)\nThis copy was installed by `install_project.py` from "
                     "github.com/ocosar/dev-harness (version in `.claude/dev-harness/VERSION`). Harness fixes go to that "
                     "repo as a PR; this repo then re-runs the installer on a branch and opens a PR — never hand-edit "
                     "the copy under `.claude/`.\n")
        out = claude / "skills" / f"dev-harness-{skill.name}" / "SKILL.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)

    old_agents = [x for g in ("dh-*.md", "dev-harness-*.md") for x in (claude / "agents").glob(g)] if (claude / "agents").exists() else []
    for old in old_agents:
        old.unlink()
    for agent in sorted((SRC / "agents").glob("*.md")):
        text = rewrite(agent.read_text()).replace(f"name: {agent.stem}\n", f"name: dev-harness-{agent.stem}\n", 1)
        out = claude / "agents" / f"dev-harness-{agent.name}"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)

    hooks = json.loads((SRC / "hooks" / "hooks.json").read_text())["hooks"]
    for groups in hooks.values():
        for g in groups:
            for h in g["hooks"]:
                h["command"] = h["command"].replace('python3 "${CLAUDE_PLUGIN_ROOT}/',
                                                    'python3 -B "$CLAUDE_PROJECT_DIR/.claude/dev-harness/')
    settings_path = claude / "settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        settings = {}
    current = settings.get("hooks") or {}
    for event, groups in list(current.items()):  # drop previous dev-harness hooks, keep everything else
        kept = [g for g in groups if not any(".claude/dev-harness/" in h.get("command", "") for h in g.get("hooks", []))]
        current[event] = kept
    for event, groups in hooks.items():
        current[event] = (current.get(event) or []) + groups
    settings["hooks"] = {k: v for k, v in current.items() if v}
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    # Files /dev-harness-init wrote earlier that mention command names (older installs used /dh-… or the plugin's
    # /dev-harness:…). Only names are renamed; nothing else in them changes.
    def rename(text: str) -> str:
        return re.sub(r"(?<![\w-])/dh-(?=[a-z])", "/dev-harness-", rewrite(text))
    for rel in ((".harness/paid_guard.py", ".harness/check.sh") if prev is None or prev >= (0, 6, 5) else ()):
        f = repo / rel
        if f.is_file():
            old = f.read_text()
            if rename(old) != old:
                f.write_text(rename(old))
    md = repo / "CLAUDE.md"
    if md.is_file():
        old = md.read_text()
        new = re.sub(r"(<!-- dev-harness:start.*?<!-- dev-harness:end -->)", lambda m: rename(m.group(1)), old, flags=re.S)
        if new != old:
            md.write_text(new)
    print(f"installed dev-harness {version} into {repo}/.claude — commit it; it is active from the next session.\n"
          "Skills: /dev-harness-init /dev-harness-spec /dev-harness-build /dev-harness-paid-call /dev-harness-experiment /dev-harness-autoloop /dev-harness-retro /dev-harness-ship /dev-harness-status /dev-harness-eval")
    return 0


if __name__ == "__main__":
    sys.exit(main())
