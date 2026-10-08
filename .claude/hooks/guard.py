#!/usr/bin/env python3
"""PreToolUse guard for Bash commands: enforces AGENTS.md §4 "Agents in the workspace".

On the production VM (hostname ``usher-prod``) it also enforces CLAUDE.md "On the
production VM": read-only, and Edit/Write on the checkout are refused as well.

Reads the hook JSON on stdin, prints a reason to stderr and exits 2 to block.
Exit 0 lets the normal permission flow decide. Standard library only.
"""

import json
import os
import re
import socket
import sys

VM_HOSTNAME = "usher-prod"
VM_RULES = 'CLAUDE.md "On the production VM": read-only; write the command out for the owner'
GIT_WRITES = {
    "pull", "merge", "rebase", "checkout", "switch", "reset", "restore", "stash", "commit",
    "push", "cherry-pick", "am", "apply", "clean", "revert",
}  # fmt: skip
GIT_BRANCH_LISTING = {"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes",
                      "--show-current", "--verbose", "--no-color"}  # fmt: skip
VM_TOOLS = {"evcrawl", "release.sh", "psql", "pg_dump", "pg_restore", "redis-cli", "gcloud",
            "shutdown", "reboot", "poweroff", "halt", "kill", "pkill", "killall"}  # fmt: skip
SYSTEMCTL_WRITES = {"start", "stop", "restart", "reload", "enable", "disable", "mask",
                    "unmask", "try-restart", "reload-or-restart", "kill", "isolate", "reboot",
                    "poweroff"}  # fmt: skip
WRAPPERS = {"env", "nohup", "time", "exec", "command", "nice", "stdbuf", "xargs"}
SECRETS = re.compile(
    r"""(?:^|[\s/'"=<>:])\.env(?:\.(?!example\b)[\w-]+)*(?=$|[\s'";|&)<>:])"""
    r"|credentials\.json|\.ssh(?:/|\b)|\bid_(?:rsa|ed25519|ecdsa|dsa)\b"
    r"|ssh_host_\w*key|/etc/(?:g?shadow|sudoers)"
)


def deny(reason: str) -> None:
    print(f"blocked by .claude/hooks/guard.py: {reason}", file=sys.stderr)
    sys.exit(2)


def segments(cmd: str) -> list[list[str]]:
    """Each simple command's words, wrappers and VAR=value prefixes dropped."""
    out = []
    for seg in re.split(r"\|\||&&|[;|&\n`()]|\$\(|\b(?:ba|z|da)?sh\s+-c\s+", cmd):
        words = seg.replace("'", " ").replace('"', " ").split()
        while words and (words[0] in WRAPPERS or re.match(r"^\w+=", words[0])):
            words.pop(0)
        if words[:2] == ["uv", "run"]:
            words = words[2:]
        if words and words[0] == "timeout":
            words = words[2:]
        if words:
            out.append(words)
    return out


def in_repo(path: str, cwd: str, root: str) -> bool:
    if path in {"/dev/null", "/dev/stderr", "/dev/stdout"}:
        return False
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path.replace("$HOME", "~"))))
    return full == root or full.startswith(root + "/")


def vm_rules(payload: dict[str, object]) -> None:
    """usher-prod runs production; the agent there observes and explains only."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    cwd = str(payload.get("cwd") or os.getcwd())
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if target and payload.get("tool_name") != "Bash":
        if in_repo(str(target), cwd, root):
            deny(f"no edits to the production checkout ({VM_RULES}).")
        return
    cmd = str(tool_input.get("command", ""))
    if SECRETS.search(cmd):
        deny(f".env, tunnel credentials and SSH keys are never read on the VM ({VM_RULES}).")
    for words in segments(cmd):
        name = os.path.basename(words[0])
        if name in VM_TOOLS:
            deny(f"{name} does not run from the agent on the VM ({VM_RULES}).")
        if name == "systemctl" and SYSTEMCTL_WRITES & set(words[1:]):
            deny(f"no service start/stop/enable on the VM ({VM_RULES}).")
        if name == "git":
            rest = words[1:]
            while rest and rest[0] in {"-C", "-c", "--no-pager"}:
                rest = rest[2:] if rest[0] != "--no-pager" else rest[1:]
            if rest and rest[0] in GIT_WRITES:
                deny(f"git {rest[0]} moves the production checkout ({VM_RULES}).")
            if rest and rest[0] == "branch" and not set(rest[1:]) <= GIT_BRANCH_LISTING:
                deny(f"git branch on the VM is for listing only ({VM_RULES}).")
        args = [w for w in words[1:] if not w.startswith("-")]
        if name == "sed" and any(w == "--in-place" or re.match(r"^-\w*i", w) for w in words):
            files = args if {"-e", "-f", "--expression"} & set(words) else args[1:]
        elif name in {"cp", "ln", "install"}:
            files = args[-1:]
        elif name in {"mv", "rm", "rmdir", "touch", "mkdir", "truncate", "unlink", "tee"}:
            files = args
        else:
            files = []
        for f in files:
            if in_repo(f, cwd, root):
                deny(f"{name} would write into the production checkout ({VM_RULES}).")
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "Q", cmd)
    for m in re.finditer(r"(?:^|[^>&<])(?:\d|&)?>>?\s*(?!&)([^\s;&|<>()]+)", unquoted):
        if in_repo(m.group(1), cwd, root):
            deny(f"redirection into the production checkout ({VM_RULES}).")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    if socket.gethostname() == VM_HOSTNAME:
        vm_rules(payload)
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
