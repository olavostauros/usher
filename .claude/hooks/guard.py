#!/usr/bin/env python3
"""PreToolUse guard for Bash commands: enforces AGENTS.md §4 "Agents in the workspace".

Reads the hook JSON on stdin, prints a reason to stderr and exits 2 to block.
Exit 0 lets the normal permission flow decide. Standard library only.
"""

import json
import os
import re
import sys


def deny(reason: str) -> None:
    print(f"blocked by .claude/hooks/guard.py: {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    cmd = str(payload.get("tool_input", {}).get("command", ""))
    if not cmd.strip():
        return
    home = os.path.expanduser("~")
    user = os.path.basename(home)

    # --- privileged or host-level tools (AGENTS.md §4: enforced, do not work around)
    if re.search(r"(^|[\s;&|(])(sudo|su|doas|docker|docker-compose|apt|apt-get|pip)\s", cmd + " "):
        deny(
            "no sudo/su, apt, pip into system paths or docker inside the workspace (AGENTS.md §4). "
            "Missing tooling is a PR against the dev stage of the Dockerfile."
        )

    # --- crawls and the scheduler (AGENTS.md §4: never without --dry-run or --limit)
    if (
        re.search(r"\bevcrawl\s+scheduler\b", cmd)
        and os.environ.get("EVCRAWL_ALLOW_SCHEDULER") != "1"
    ):
        deny(
            "evcrawl scheduler is not run from the workspace unless the task says so "
            "(AGENTS.md §4). Set EVCRAWL_ALLOW_SCHEDULER=1 only when the task explicitly allows it."
        )
    if re.search(r"\bevcrawl\s+crawl\b", cmd) and not re.search(r"--(dry-run|limit)\b", cmd):
        deny(
            "crawls from the workspace need --dry-run or --limit; the db is shared "
            "(AGENTS.md §4, §11)."
        )

    # --- destructive SQL and stack wipes (AGENTS.md §4, §7)
    if re.search(r"\b(DROP)\s+(TABLE|SCHEMA|DATABASE|EXTENSION)\b", cmd, re.I):
        deny(
            "DROP on the shared database is not allowed; use createdb <login>_scratch "
            "for experiments (AGENTS.md §7)."
        )
    if re.search(r"\bTRUNCATE\b", cmd, re.I):
        deny("TRUNCATE on the shared database is not allowed (AGENTS.md §4).")
    if re.search(r"\bDELETE\s+FROM\s+[\w.\"]+\s*(;|$|\"|')", cmd, re.I) and not re.search(
        r"\bWHERE\b", cmd, re.I
    ):
        deny("DELETE without WHERE is not allowed on public.* (AGENTS.md §4).")
    if re.search(r"compose\s+down\b.*\s-v\b|compose\s+down\s+--volumes", cmd):
        deny(
            "`compose down -v` wipes everyone's db and homes; the host owner announces "
            "and runs it (AGENTS.md §4)."
        )

    # --- file-system reach (AGENTS.md §4: only ~/usher and your own dotfiles)
    other_home = re.search(r"/home/([a-z_][a-z0-9_-]*)", cmd)
    if other_home and other_home.group(1) != user:
        deny(f"paths under /home/{other_home.group(1)} belong to another member (AGENTS.md §4).")
    for m in re.finditer(
        r"\b(rm\s+-[a-zA-Z]*[rf][a-zA-Z]*|chmod|chown)\s+(?:[-\w+=,.:]+\s+)*(/[^\s;&|]*)", cmd
    ):
        path = m.group(2)
        if not (path.startswith(home + "/") or path == home or path.startswith("/tmp/")):
            deny(f"{m.group(1)} on {path}: only inside $HOME or /tmp (AGENTS.md §4).")
    if re.search(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:-[\w-]+\s+)*(/|~|\$HOME)(\s|$)", cmd):
        deny("refusing to remove / or the whole home (AGENTS.md §4).")

    # --- git history (AGENTS.md §4, §11: branch per task, PR into main, never force)
    if re.search(r"\bgit\s+push\b", cmd):
        if re.search(r"\s(--force|-f|--force-with-lease)\b", cmd):
            deny("force pushes are not allowed (AGENTS.md §4).")
        if re.search(
            r"\bgit\s+push\s+(?:-u\s+|--set-upstream\s+)?\S+\s+(main|master)(:|\s|$)", cmd
        ):
            deny(
                "main is updated by merged PRs only; push your task branch instead (AGENTS.md §11)."
            )
    if re.search(r"\bgit\s+(branch\s+-D|reset\s+--hard\s+origin/(main|master))", cmd):
        deny("do not rewrite or hard-reset shared branches (AGENTS.md §4).")


if __name__ == "__main__":
    main()
