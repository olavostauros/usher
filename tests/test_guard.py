"""`.claude/hooks/guard.py`: the workspace rules everywhere, the read-only VM rules on usher-prod.

The hook is loaded with importlib and fed the PreToolUse payload on stdin; the
hostname is monkeypatched. No database, no network.
"""

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_PY = ROOT / ".claude" / "hooks" / "guard.py"
VM = "usher-prod"
ELSEWHERE = "stauros-desktop"


def load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard", GUARD_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_guard()


def run(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    command: str = "",
    tool: str = "Bash",
    file_path: str | None = None,
) -> tuple[int, str]:
    monkeypatch.setattr(guard.socket, "gethostname", lambda: host)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(ROOT))
    tool_input: dict[str, str] = {"command": command} if tool == "Bash" else {}
    if file_path:
        tool_input["file_path"] = file_path
    payload = {"tool_name": tool, "cwd": str(ROOT), "tool_input": tool_input}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    try:
        guard.main()
    except SystemExit as exc:
        return int(exc.code or 0), err.getvalue()
    return 0, err.getvalue()


VM_DENIED = [
    "git pull",
    "git -C ~/usher pull --ff-only",
    "git merge origin/main",
    "git rebase origin/main",
    "git checkout main",
    "git switch -c x",
    "git reset --hard HEAD~1",
    "git restore README.md",
    "git stash",
    "git commit -m x",
    "git push origin x",
    "git cherry-pick abc123",
    "git am patch.mbox",
    "git apply fix.diff",
    "git clean -fd",
    "git branch new-branch",
    "evcrawl version",
    "uv run evcrawl crawl sympla --dry-run",
    "ops/host/release.sh",
    "psql -c 'select 1'",
    "pg_dump -Fc evcrawl",
    "pg_restore -d evcrawl x.dump",
    "redis-cli ping",
    "gcloud compute instances list",
    "systemctl restart usher.service",
    "systemctl stop usher.service",
    "systemctl disable usher.service",
    "shutdown -h now",
    "reboot",
    "kill 1234",
    "pkill -f uvicorn",
    "cat .env",
    "grep PASSWORD ops/host/.env",
    "cp .env.local /tmp/x",
    "cat ops/host/cloudflared/credentials.json",
    "ls ~/.ssh/",
    "cat ~/.ssh/id_ed25519",
    "echo x > README.md",
    "echo x >> docs/notes.md",
    "python3 ops/host/status.py --json | tee status.json",
    "sed -i s/a/b/ AGENTS.md",
    "mv README.md /tmp/",
    "cp /tmp/x ops/host/README.md",
    "rm CLAUDE.md",
    "ls && git pull",
]


@pytest.mark.parametrize("command", VM_DENIED)
def test_vm_denies(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    code, reason = run(monkeypatch, VM, command)
    assert code == 2, command
    assert "On the production VM" in reason


# Allowed by the hook off the VM today (secrets reads and repo writes included);
# git push to a task branch, release.sh and psql are workspace/host business.
ALLOWED_ELSEWHERE = [
    "git pull",
    "git commit -m x",
    "git push origin x",
    "git switch -c x",
    "evcrawl version",
    "uv run evcrawl crawl sympla --dry-run",
    "ops/host/release.sh",
    "psql -c 'select 1'",
    "redis-cli ping",
    "gcloud compute instances list",
    "systemctl restart usher.service",
    "cat .env",
    "echo x > README.md",
    "sed -i s/a/b/ AGENTS.md",
    "rm CLAUDE.md",
]


@pytest.mark.parametrize("command", ALLOWED_ELSEWHERE)
def test_same_commands_pass_elsewhere(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    assert run(monkeypatch, ELSEWHERE, command)[0] == 0, command


VM_ALLOWED = [
    "git status",
    "git log -1 --oneline",
    "git fetch",
    "git log --oneline HEAD..origin/main",
    "git diff HEAD origin/main -- docker-compose.yml",
    "git show origin/main:ops/host/README.md",
    "git branch -a",
    "python3 ops/host/status.py",
    "python3 ops/host/status.py --json",
    "python3 ops/host/status.py --city vitoria",
    "curl -s https://api.eventolivre.com/healthz",
    "df -h /",
    "free -h",
    "uptime",
    "systemctl status usher.service",
    "systemctl is-enabled usher.service",
    "cat .env.example",
    "cat src/evcrawl/cli.py",
    "ls 2>&1 | head",
    "python3 ops/host/status.py --json > /tmp/status.json",
]


@pytest.mark.parametrize("command", VM_ALLOWED)
def test_vm_allows_reads(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    code, reason = run(monkeypatch, VM, command)
    assert code == 0, (command, reason)


def test_vm_denies_edit_and_write_on_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    for tool in ("Edit", "Write"):
        assert run(monkeypatch, VM, tool=tool, file_path=str(ROOT / "README.md"))[0] == 2
        assert run(monkeypatch, VM, tool=tool, file_path="/tmp/scratch/notes.md")[0] == 0
        assert run(monkeypatch, ELSEWHERE, tool=tool, file_path=str(ROOT / "README.md"))[0] == 0


@pytest.mark.parametrize("host", [VM, ELSEWHERE])
def test_docker_denied_everywhere(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    assert run(monkeypatch, host, "docker" + " ps")[0] == 2
    assert run(monkeypatch, host, "sudo ls")[0] == 2


def test_workspace_crawl_needs_dry_run_or_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(monkeypatch, ELSEWHERE, "evcrawl crawl all")[0] == 2
    assert run(monkeypatch, ELSEWHERE, "evcrawl crawl all --limit 5")[0] == 0
    assert run(monkeypatch, ELSEWHERE, "git push --force origin x")[0] == 2


# ADR 0020: from any machine, an agent only lists, reads and prompts the VM's agent.
PROD_REACH_DENIED = [
    "ssh usher-prod hostname",
    "ssh -t usher-prod 'cd usher && git pull'",
    "ssh olavostauros_gmail_com@usher-prod uptime",
    "ssh compute.3612190695804880857 ls",
    "ssh olavostauros_gmail_com@100.122.99.101 uptime",
    "ssh usher-prod.tail1234.ts.net uptime",
    "scp usher-prod:usher/README.md /tmp/x",
    "rsync -a usher-prod:usher/ /tmp/u",
    "bash -c 'ssh usher-prod uptime'",
    "gcloud compute ssh usher-prod --zone southamerica-east1-b --tunnel-through-iap",
    "gcloud --project usher-prod compute scp x usher-prod:~",
    "gcloud compute start-iap-tunnel usher-prod 22 --listen-on-stdin",
    "ssh -o 'ProxyCommand=gcloud compute start-iap-tunnel usher-prod %p' x uptime",
    "herdr --machine usher-prod pane run w1:p2 'uptime'",
    "herdr --machine usher-prod pane send-text w1:p1 x",
    "herdr --machine usher-prod pane send-keys w1:p1 Enter",
    "herdr --machine usher-prod pane split w1:p1",
    "herdr --machine=usher-prod pane close w1:p1",
    "herdr --machine usher-prod agent send-keys w1:p1 shift+tab",
    "herdr --machine usher-prod agent start w1:p2",
    "herdr --machine usher-prod agent attach w1:p1",
    "herdr --machine usher-prod api call x",
    "herdr --machine usher-prod server stop",
    "herdr --remote usher-prod",
    "herdr machine add usher-prod",
    "herdr machine remove usher-prod",
    "herdr --machine usher-prod agent prompt w1:p1 '!uptime'",
    "herdr --machine usher-prod agent prompt w1:p1 '  /permissions'",
    "herdr --machine usher-prod agent prompt --timeout 5 w1:p1 '#remember x'",
    "herdr --machine usher-prod agent prompt w1:p1 'status\x1b[Z'",
    "herdr --machine usher-prod agent prompt w1:p1 'status\n!uptime'",
    "herdr --machine usher-prod agent prompt w1:p1 'status\n!uptime'",
    "ls && herdr --machine usher-prod pane run w1:p2 uptime",
    'herdr --machine usher-prod agent prompt w1:p1 "$(printf %s !uptime)"',
    "herdr --machine usher-prod agent prompt w1:p1 $(cat /tmp/p.txt)",
    'herdr --machine usher-prod agent prompt w1:p1 "`cat /tmp/p.txt`"',
    'herdr --machine usher-prod agent prompt w1:p1 "$PROMPT"',
    'herdr --machine usher-prod agent prompt w1:p1 "$(cat /nonexistent/p.txt)"',
]


def test_prod_prompt_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ok, bad = tmp_path / "ok.txt", tmp_path / "bad.txt"
    ok.write_text("Read-only status report, please; R$ 15/day budget.\n")
    bad.write_text("!uptime\n")
    cmd = 'herdr --machine usher-prod agent prompt w1:p1 "$(cat {})" --wait'
    assert run(monkeypatch, ELSEWHERE, cmd.format(ok))[0] == 0
    assert run(monkeypatch, ELSEWHERE, cmd.format(bad))[0] == 2


@pytest.mark.parametrize("host", [VM, ELSEWHERE])
@pytest.mark.parametrize("command", PROD_REACH_DENIED)
def test_prod_reach_denied(monkeypatch: pytest.MonkeyPatch, host: str, command: str) -> None:
    code, reason = run(monkeypatch, host, command)
    assert code == 2, command
    # on the VM a few of these hit the read-only VM rules first (git pull, gcloud)
    assert "ADR 0020" in reason or (host == VM and "On the production VM" in reason)


PROD_REACH_ALLOWED = [
    "herdr machine list",
    "herdr --machine usher-prod agent list",
    "herdr --machine usher-prod agent get w1:p1",
    "herdr --machine usher-prod agent read w1:p1 --source recent-unwrapped --lines 200",
    "herdr --machine usher-prod agent wait w1:p1 --until idle --timeout 60000",
    "herdr --machine usher-prod agent explain w1:p1",
    "herdr --machine usher-prod pane list",
    "herdr --machine usher-prod agent prompt w1:p1 'Read-only status report, please.' --wait",
    "herdr --machine usher-prod agent prompt --timeout 5 w1:p1 'status, please'",
    "herdr pane run w1:p2 'uv run pytest -q'",
    "herdr agent prompt w1:p1 '/clear'",
    "ssh usher uptime",
    "ssh -p 2222 olavostauros@stauros-desktop",
    "gcloud compute instances list --project usher-prod",
    "git log --grep 'ssh usher-prod'",
    "git commit -m 'refuse gcloud compute ssh from agents'",
]


@pytest.mark.parametrize("command", PROD_REACH_ALLOWED)
def test_prod_reach_allowed(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    code, reason = run(monkeypatch, ELSEWHERE, command)
    assert code == 0, (command, reason)
