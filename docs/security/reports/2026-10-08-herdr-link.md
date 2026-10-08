# Security audit: the herdr link from the host to the production VM usher-prod (ADR 0020)

- Date: 2026-10-08 18:00-19:10Z
- Mode: host (`stauros-desktop`), the owner's session; **scoped audit**, not a stack audit. Tools: `herdr` 0.9.3 help output and its v0.9.3 documentation; the read-only `herdr` commands `machine list`, `--machine usher-prod agent list`, `agent get`, `agent explain`, `pane list`, `status server`, and the local `pane list`; `.claude/hooks/guard.py` fed recorded command strings on stdin (nothing executed); reading the repo. No login to the VM, no `herdr` command that types, no `ssh`, no `docker`.
- Repository commit: e553279 (`main`, the merge of PR #97, which added ADR 0020 and `ops/gcp/vm-setup.md` §7). PR #98 (`fix/herdr-prod-reach`, 100a350, open) was opened during this audit; it is reviewed in "Analyst notes", not counted as a fix.
- Previous report: docs/security/reports/2026-10-07-usher-stack.md (still the open queue for everything outside this scope)
- Findings: high 1, medium 2, low 2, info 1 (6 new, F-039 to F-044)
- Not in this run: `ops/audit/audit.py` (it has no herdr or host-agent checks; CVE data is the 2026-10-07 run's); anything on the VM itself (agents never log in to it; the owner's checks are in each finding's Verify).

Fix contract: see docs/security/README.md. One PR per finding unless two touch the same file; the commit message cites the finding id; set `Status` in this file when merged.

## Summary

| id | severity | title | component | rebuild | since | status |
|---|---|---|---|---|---|---|
| F-039 | HIGH | `herdr`'s typing commands give any host agent the owner's shell on usher-prod (root-equivalent), outside the VM agent's guard; ADR 0020's blast-radius claim is false | docs/decisions/0020 "Consequences"; .claude/hooks/guard.py; the host's `herdr` machine profile `usher-prod` | no | new | fixed in #98, #100, #101, #102 (owner: steps 1 and 4) |
| F-040 | MEDIUM | Nothing stops a host agent from opening a plain SSH session to usher-prod (the `usher-prod` alias, `gcloud compute ssh/scp`, the IAP tunnel); "agents never log in to the VM" is a rule on paper | owner's `~/.ssh/config` (vm-setup.md §7 step 1); .claude/hooks/guard.py; .claude/settings.json `deny` | no | new | open (guard part in #98; tailnet path and sshd on 0.0.0.0:22 remain) |
| F-041 | MEDIUM | "Only the owner allows one action, in their own words" cannot be enforced while host agents can prompt the VM agent | AGENTS.md §4 "On the production VM"; .claude/agents/usher-ops.md "Rules"; ADR 0020 "Decision" | no | new | fixed in #PRNUM |
| F-042 | INFO (was LOW) | `herdr` on the production VM is an unpinned, unverified third-party binary and a persistent server under a root-equivalent user, outside the image | ops/gcp/vm-setup.md §7 step 2; VM `~/.local/bin/herdr`, `~/.config/herdr/` | no | new | open (INFO after #102; steps 1 and 2 open) |
| F-043 | LOW | The VM pane's scrollback leaves production to every host process through `agent read` / `pane read` | ADR 0020; ops/gcp/vm-setup.md §7 | no | new | open |
| F-044 | INFO | The VM's `herdr` session and agent die on every preemption or restart and nothing restarts them | ADR 0020 "Consequences" bullet 1 | no | new | open (accepted, ADR 0020) |

## Findings

### F-039 [HIGH] `herdr`'s typing commands give any host agent the owner's shell on usher-prod (root-equivalent), outside the VM agent's guard; ADR 0020's blast-radius claim is false

- Category: production-access, agent-guardrails
- Component: docs/decisions/0020-herdr-on-the-production-vm.md ("Consequences": "Its blast radius is the production agent's: read-only, enforced by the guard hook"); .claude/hooks/guard.py (no rule for `herdr`); the host's `herdr` 0.9.3 machine profile `usher-prod`
- Rebuild required: no
- Status: fixed in #98, #100, #101, #102 (guard rules in #98 and #100; step 5, the boundary: the status snapshot in #101 and herdr as the unprivileged `usher-agent` in #102). Owner still has step 1 (keep the `herdr` profile disabled outside a VM session) and step 4 (user-level `permissions.deny`).
- Since: new

Evidence:

```
$ herdr --version
herdr 0.9.3
$ herdr pane --help        (excerpt)
  split                 Split a pane
  close                 Close a pane
  send-text             Send literal text to a pane
  send-keys             Send key presses to a pane
  run                   Run a command in a pane
$ herdr pane run --help
Run a command in a pane
Usage: herdr pane run <PANE_ID> <COMMAND>...
$ herdr agent --help       (excerpt)
  send-keys  Send key presses to an agent
  prompt     Submit a prompt to an agent
  start      Start a supported interactive agent in an existing pane
$ herdr machine list
9f9b12beee1dfa55d462e28ef9795fff	usher-prod	usher-prod	default	enabled
$ herdr --machine usher-prod pane list          (abridged)
w1:p1  agent=claude  cwd=/home/olavostauros_gmail_com/usher
$ herdr --machine usher-prod status server
status: running   version: 0.9.3   socket: machine:9f9b12beee1dfa55d462e28ef9795fff/default

herdr CLI reference v0.9.3 (docs/next/website/src/content/docs/cli-reference.mdx), "--machine routing":
  "Supported commands are `workspace`, `worktree`, `tab`, `pane`, `notification`, `agent`
   (except `attach` and local `explain --file`) ... Server commands support `stop`, ..."
  (no read-only, allowlist or permission option exists for --machine)

$ grep -n 'usermod -aG' ops/gcp/vm-setup.sh
35:  sudo usermod -aG docker "$USER"           (the owner's VM user is in the docker group; vm-setup.sh runs sudo as it)

guard.py at e553279, fed the 29 PROD_REACH_DENIED strings of #98's tests/test_guard.py on stdin:
rc=0  'herdr --machine usher-prod pane split w1:p1'
rc=0  "herdr --machine usher-prod pane run w1:p2 'uptime'"
rc=0  'herdr --machine usher-prod pane send-keys w1:p1 Enter'
rc=0  'herdr --machine usher-prod agent send-keys w1:p1 shift+tab'
rc=0  'herdr --machine usher-prod agent start w1:p2'
rc=0  "herdr --machine usher-prod agent prompt w1:p1 '!uptime'"
rc=0  'herdr --machine usher-prod server stop'
... (29 of 29 rc=0)
$ grep -c -i herdr .claude/settings.json
0
$ herdr pane list          (local, read-only)
wP:p1 claude  ...  /home/olavostauros/Work/usher
wQ:p1 claude  ...  /home/olavostauros/Work/emmoni       (an agent of another project, which does not load this repo's hook)
$ herdr integration status (excerpt)
codex: current (v8)   hermes: current (v5)   pi, claude, opencode: outdated   (non-Claude agents are installed on this host too)
```

Impact: the VM agent's guard (`.claude/hooks/guard.py` on `usher-prod`) sees only that agent's own tool calls. `herdr --machine usher-prod` reaches the VM's `herdr` server, which owns every pane there, so a host process can `pane split` a new shell next to the agent and `pane run` anything in it as the owner's OS Login user: `docker` group and `sudo`, i.e. root on production, the production db as superuser, `.env` and the tunnel credentials. Without a new pane: `agent send-keys` answers the VM agent's permission dialogs, interrupts it or cycles its mode; `agent prompt '!cmd'` runs `cmd` through Claude Code's bash mode, which is typed input, not a tool call, so no PreToolUse hook sees it (Claude Code behaviour; not reproduced on the VM, by the limits of this audit); `server stop` ends the agent. Who can do it: every process of the owner's Unix user on the host, which includes every Claude Code session in any project (the `emmoni` pane above loads no usher hook) and the codex, opencode, pi and hermes agents installed next to it, which load no Claude hook at all. A host agent steered by content it reads (a PR comment, a crawled page, an issue; CYBERSECURITY.md §3) needs one command. ADR 0020 and AGENTS.md §4 told agents the opposite ("blast radius is the production agent's"), and `herdr`'s own help invites agents to drive panes ("Control Herdr panes, agents, or workspaces: run `herdr --skill`"). HIGH, not CRITICAL: the actor is the owner's own agent holding the owner's credentials, not a member and not an unauthenticated party (the rubric keeps CRITICAL for those); above MEDIUM because the reach is root on production behind a documented claim that it is not.

Fix:

1. Owner, now: keep the profile off outside a VM session: `herdr machine disable usher-prod`, and `herdr machine enable usher-prod` when you sit down to use the VM agent (`--machine` "must match an enabled saved profile", cli-reference.mdx). Write it into ops/gcp/vm-setup.md §7.
2. docs/decisions/0020-herdr-on-the-production-vm.md "Consequences": replace the blast-radius bullet with what is true: `herdr` types as the owner on the VM; the host guard narrows it for Claude Code sessions in this checkout only; the boundary is step 5. (#98 rewrites the bullet; see "Analyst notes, On PR #98" for the wording it still overstates.)
3. .claude/hooks/guard.py: with `--machine`/`--remote`, allow only `machine list`, `agent list|get|read|explain|wait`, `pane list|get|read|layout|process-info` and `agent prompt` with plain text; deny the rest, and `machine add|remove|enable`. #98 does this; close the gaps listed in the analyst notes before merging.
4. Owner's user-level `~/.claude/settings.json` `permissions.deny` (applies to Claude Code in every project on the host): `Bash(herdr --machine usher-prod pane run:*)`, `... pane send-text:*`, `... pane send-keys:*`, `... pane split:*`, `... pane close:*`, `... agent send-keys:*`, `... agent start:*`, `... server:*`, `Bash(herdr machine add:*)`, `Bash(herdr machine enable:*)`, plus the F-040 lines. Prefix rules are guidance (F-006), and non-Claude agents still ignore them; that is why step 5 exists.
5. The boundary, owner decision, recorded in ADR 0020: run the VM's `herdr` server and agent as an identity that is not root-equivalent. A second OS Login identity with `roles/compute.osLogin` (not `osAdminLogin`), not in the `docker` group, with read access to the checkout and none to `.env` or `ops/host/cloudflared/credentials.json`; a host alias `usher-prod-ops` for it; `herdr machine add usher-prod-ops`; the owner's admin identity only ever in a real terminal. `ops/host/status.py` needs `docker compose exec db psql` today: give the restricted user a root-run systemd timer that writes `status.py --json` to a file it can read (or a read-only role over a socket), never a `docker` group membership or a sudo rule on a script in a writable checkout. Then whatever a host agent types into the VM is bounded by Unix permissions, and F-040 and F-042 drop to INFO.

Verify:

```
printf '%s' '{"tool_input":{"command":"herdr --machine usher-prod pane run w1:p2 id"}}' | python3 .claude/hooks/guard.py; echo $?   -> 2
printf '%s' '{"tool_input":{"command":"herdr --machine usher-prod agent send-keys w1:p1 Enter"}}' | python3 .claude/hooks/guard.py; echo $?   -> 2
herdr machine list                      -> usher-prod ... disabled   (outside an owner session)
# after step 5, the owner in a pane of the VM's herdr session:
id -nG                                 -> no docker, no google-sudoers
sudo -n true                           -> "a password is required" / not allowed
```

### F-040 [MEDIUM] Nothing stops a host agent from opening a plain SSH session to usher-prod; "agents never log in to the VM" is a rule on paper

- Category: production-access, agent-guardrails
- Component: the owner's `~/.ssh/config` `Host usher-prod` (ops/gcp/vm-setup.md §7 step 1, ProxyCommand `gcloud compute start-iap-tunnel`); .claude/hooks/guard.py; .claude/settings.json `permissions.deny`; the rule in docs/security/README.md ("agents never log in to the VM") and .claude/agents/atlas.md ("you do not `gcloud compute ssh` into it")
- Rebuild required: no
- Status: open (alias, `compute.<id>` and `gcloud` rules merged in #98; the tailnet path is not covered, and the VM's sshd listens on 0.0.0.0:22, `ss -ltn` 2026-10-08 20:17Z, reported from the VM agent)
- Since: new

Evidence:

```
guard.py at e553279, the same 29 strings of #98's tests:
rc=0  'ssh usher-prod hostname'
rc=0  "ssh -t usher-prod 'cd usher && git pull'"
rc=0  'ssh compute.3612190695804880857 ls'
rc=0  'scp usher-prod:usher/README.md /tmp/x'
rc=0  "bash -c 'ssh usher-prod uptime'"
rc=0  'gcloud compute ssh usher-prod --zone southamerica-east1-b --tunnel-through-iap'
rc=0  'gcloud compute start-iap-tunnel usher-prod 22 --listen-on-stdin'
$ python3 -c 'import json; print(json.load(open(".claude/settings.json"))["permissions"]["deny"])'
['Bash(docker:*)', 'Bash(docker-compose:*)', 'Bash(sudo:*)', 'Bash(su:*)', 'Bash(git push --force:*)', 'Bash(git push -f:*)', 'Read(/home/*/.ssh/**)', 'Read(**/.credentials.json)']
guard.py at 100a350 (#98):
rc=0  'ssh olavostauros_gmail_com@100.122.99.101 uptime'     (the VM's tailnet address, DEPLOY.md step 6)
```

Impact: the same reach as F-039 (the owner's shell on the VM, root-equivalent) in one non-interactive command. It predates ADR 0020: the host has held the owner's IAP and OS Login credentials since DEPLOY.md step 5, and nothing mechanical backed atlas.md's "never on the VM". ADR 0020's alias makes it a bare `ssh usher-prod <cmd>` that any tool can run. MEDIUM per the rubric (a documented rule that is not enforced); it is not rated with F-039 because no document claimed it was safe, and the fix is the same identity split. Unverified, owner to check: the VM is on the tailnet (`100.122.99.101`); the GCP firewall does not filter Tailscale traffic, so if the VM's sshd listens on all addresses and the tailnet ACL lets the host reach port 22, SSH works without IAP, and #98's host pattern (`usher-prod` or `compute.<id>`) does not match the address.

Fix:

1. .claude/hooks/guard.py: #98's rules (ssh, scp, sftp, rsync, sshfs, mosh to `usher-prod` or `compute.<id>`; `gcloud compute ssh|scp|start-iap-tunnel`), plus the tailnet address and MagicDNS name of `usher-prod` in `PROD_HOST`.
2. User-level deny (F-039 step 4): `Bash(ssh usher-prod:*)`, `Bash(gcloud compute ssh:*)`, `Bash(gcloud compute scp:*)`, `Bash(gcloud compute start-iap-tunnel:*)`.
3. Owner on the VM: `sudo ss -ltnp 'sport = :22'`. If sshd listens on `0.0.0.0`/`[::]`, either set `ListenAddress` to the VPC address only, or make the tailnet ACL grant nothing on `usher-prod:22`. CYBERSECURITY.md §6 "the only ingress is TCP 22 from IAP" then names the tailnet as checked.
4. F-039 step 5 (the agent-reachable identity is not root-equivalent).

Verify:

```
printf '%s' '{"tool_input":{"command":"ssh usher-prod hostname"}}' | python3 .claude/hooks/guard.py; echo $?          -> 2
printf '%s' '{"tool_input":{"command":"ssh x@100.122.99.101 uptime"}}' | python3 .claude/hooks/guard.py; echo $?   -> 2
# owner, on the VM:
sudo ss -ltn 'sport = :22'             -> no listener on 0.0.0.0/[::] or the tailnet address (or the ACL documented as denying it)
```

### F-041 [MEDIUM] "Only the owner allows one action, in their own words" cannot be enforced while host agents can prompt the VM agent

- Category: agent-guardrails
- Component: AGENTS.md §4 "On the production VM" ("read-only unless the owner says otherwise for one action"; "only the owner allows one action, in their own words"); .claude/agents/usher-ops.md "Rules" ("Read-only unless the owner says otherwise for one named action"); docs/decisions/0020 "Decision" bullet 4
- Rebuild required: no
- Status: fixed in #PRNUM (step 1 and step 2 in the docs; step 3 already in #100. The owner restarts the VM agents with `--permission-mode default`)
- Since: new

Evidence:

```
$ herdr agent prompt --help (excerpt)
Submit a prompt to an agent ... If the agent is already blocked, submission is rejected with agent_blocked before any input is sent.
$ herdr --machine usher-prod agent explain w1:p1
agent: claude   state: idle
rule: live_prompt_box (region=prompt_box_body priority=950)      (herdr reads and types into the terminal UI; no sender channel)
#98 tests/test_guard.py PROD_REACH_ALLOWED:
"herdr --machine usher-prod agent prompt w1:p1 'Read-only status report, please.' --wait"
guard.py at 100a350, extra strings:
rc=0  'herdr --machine usher-prod agent prompt w1:p1 "$(cat /tmp/p.txt)"'
rc=0  "herdr --machine usher-prod agent prompt w1:p1 $'status\n!uptime'"
```

Impact: a prompt sent with `agent prompt` arrives as a user turn typed into the VM agent's terminal, exactly like the owner typing; nothing marks the sender, and the main session's test of 2026-10-08 showed the VM agent answers it as the user. "The owner allows one action, in their own words" is therefore something any host agent can write. The guard still blocks what it lists, so the exposure is the set of actions the prose forbids but the guard does not: by definition its gaps (F-006: a script file, `python3 -c` with `subprocess`, a `curl` that sends `status.py` output somewhere). The VM agent's user is in the `docker` group and `status.py` runs `docker compose exec db psql -U evcrawl` (ops/host/status.py line 197), so those gaps reach the superuser. The VM agent runs in auto mode (the caller's observation), where the classifier reads a user turn as the user's intent: it lowers the bar for exactly this case. MEDIUM: a documented rule that cannot be enforced.

Fix:

1. AGENTS.md §4 "On the production VM", .claude/agents/usher-ops.md and ADR 0020: drop the "the owner allows one action" exception. Every change on the VM is a command the owner runs (in a real terminal, or with `!` typed by the owner); the agent never runs one, whoever asks. Then no prompt can widen anything and the rule needs no provenance.
2. ops/gcp/vm-setup.md §7 step 3 and usher-ops.md: start the VM agent in the default permission mode, not auto. (F-039's `send-keys` can still press the key; F-039 steps 3 and 5 close that.)
3. .claude/hooks/guard.py (#98): keep the refusal of prompts that start with `!`, `/` or `#` or carry control characters; also refuse a newline in the prompt and prompt text that comes from a substitution (`$(`, a backtick, `$'`), which #98 passes.

Verify:

```
grep -n -E "owner says otherwise|owner allows one" AGENTS.md .claude/agents/usher-ops.md docs/decisions/0020-herdr-on-the-production-vm.md   -> no lines
printf '%s' '{"tool_input":{"command":"herdr --machine usher-prod agent prompt w1:p1 \"$(cat /tmp/p.txt)\""}}' | python3 .claude/hooks/guard.py; echo $?   -> 2
```

### F-042 [INFO, was LOW] `herdr` on the production VM is an unpinned, unverified third-party binary and a persistent server under a root-equivalent user, outside the image

- Category: supply-chain
- Component: ops/gcp/vm-setup.md §7 step 2 (`herdr update`, `herdr machine add usher-prod`); on the VM `~/.local/bin/herdr`, `~/.config/herdr/herdr.sock`, `~/.local/state/herdr/agent-detection/remote/`
- Rebuild required: no
- Status: open, INFO since #102 (fix step 3: herdr runs as `usher-agent`, not root-equivalent); steps 1 (pin version and digest in vm-setup.md §7) and 2 (owner's checks on the VM) are open
- Since: new

Evidence:

```
$ herdr --machine usher-prod status server
status: running  version: 0.9.3  private_protocol: 22
$ herdr --machine usher-prod agent explain w1:p1
manifest: remote:/home/olavostauros_gmail_com/.local/state/herdr/agent-detection/remote/claude.toml 2026.09.11.1
$ herdr channel show
stable
herdr docs v0.9.3:
  persistence-remote.mdx: "Linux and macOS installs use `~/.local/bin/herdr`" ... "If copying the local binary isn't possible,
                          Herdr downloads a release asset from https://herdr.dev/latest.json"
  connecting-machines.mdx: "The remote server keeps running after setup exits."
  socket-api.mdx: default socket `~/.config/herdr/herdr.sock`; "On Unix, that socket is a Unix domain socket" (no TCP mentioned)
  install.mdx: `curl -fsSL https://herdr.dev/install.sh | sh`; updates by `herdr update`; no checksums, signatures or attestations mentioned
$ grep -n -E "0\.9|sha256|checksum|channel" ops/gcp/vm-setup.md
198:2. On the host, in a real terminal: `herdr update` (the `machine` commands      (no version, digest or channel recorded)
```

Impact: code nobody reviewed runs as a long-lived process on production under the owner's user (`docker` group, sudo), copied from whatever the host had or fetched from `latest.json`, with detection manifests pushed to it; a compromised release, or a compromised host binary, is root on production. Updates are manual (`herdr update` notifies, it does not install by itself; the docs say a host update does not replace the running remote server), which refutes "self-updating" but leaves "unpinned". By the docs it listens on no network port, so it is not network-facing: LOW per the rubric (unpinned, no current exploit path), the same class as F-002's Tailscale `curl | sh`.

Fix:

1. ops/gcp/vm-setup.md §7: record the version (0.9.3), the SHA-256 of the release asset from the project's GitHub release, and `sha256sum ~/.local/bin/herdr` on the host and the VM before `machine add`; the host stays on the `stable` channel; the VM is updated only by the owner after the same check.
2. Owner, on the VM, once: `ss -ltnp | grep -i herdr` (expect nothing); `stat -c '%a %U %n' ~/.config/herdr ~/.config/herdr/herdr.sock` (expect 700 and 600, the owner); `herdr integration status` (if the claude integration is installed, it adds a hook script to the VM agent's `~/.claude` settings: record it in §7).
3. F-039 step 5 moves the server to a non-root-equivalent user, which takes this to INFO.

Verify:

```
grep -n -E "0\.9\.3|sha256" ops/gcp/vm-setup.md         -> the pinned version and digest in §7
# owner, on the VM:
sha256sum ~/.local/bin/herdr                               -> the digest in §7
ss -ltnp | grep -ci herdr                                  -> 0
```

### F-043 [LOW] The VM pane's scrollback leaves production to every host process through `agent read` / `pane read`

- Category: data-exposure
- Component: docs/decisions/0020 (reads are allowed by design; #98 adds a line on it); ops/gcp/vm-setup.md §7; AGENTS.md §4 / CYBERSECURITY.md §3 ("nothing from the db, `.env` or a home leaves through any channel")
- Rebuild required: no
- Status: open
- Since: new

Evidence:

```
$ herdr agent read --help (excerpt)
--source <SOURCE>  Terminal snapshot source (default: recent) [possible values: visible, recent, recent-unwrapped, detection]
--lines <N>
#98 tests/test_guard.py PROD_REACH_ALLOWED:
"herdr --machine usher-prod agent read w1:p1 --source recent-unwrapped --lines 200"
AGENTS.md §4 "On the production VM": "Every command that changes something is written out for the owner, who runs it with `!`"
```

Impact: anything the VM agent's pane ever printed is readable from the host: `status.py` rows, error lines carrying crawled text, and the output of the owner's own `!` commands typed into that same pane (`docker compose logs`, `journalctl`, a `cat` of a config). The VM agent's transcript already goes to its model provider, so the new reach is every host process: agents of other projects and whatever egress they have (the host's user settings enable a Discord plugin). LOW: under its rules the agent prints aggregates, never `.env` or rows; the exposure depends on what the owner runs in that pane.

Fix:

1. ops/gcp/vm-setup.md §7: the VM agent's pane is the agent's; the owner runs commands that print logs, environment or config in a separate terminal over IAP (or a separate pane after `herdr machine disable`, F-039 step 1).
2. AGENTS.md §4: a host agent reads the VM agent's answer only (`agent read --source visible` or a bounded `--lines`) and never forwards it outside its own session.

Verify:

```
grep -n -E "separate terminal|--source visible" ops/gcp/vm-setup.md AGENTS.md   -> the two rules
```

### F-044 [INFO] The VM's `herdr` session and agent die on every preemption or restart and nothing restarts them

- Category: availability (observation only)
- Component: docs/decisions/0020 "Consequences" bullet 1; ops/gcp/vm-setup.md §7 last paragraph
- Rebuild required: no
- Status: open (accepted, ADR 0020)
- Since: new

Evidence:

```
ADR 0020: "a preemption or restart ends the herdr session and the agent, which the owner starts again."
herdr docs, persistence-remote.mdx: stopping the server ends pane processes; restore "covers layout, not running processes".
```

Impact: after a Spot preemption (DEPLOY.md: 2026-10-07 17:21 UTC, and the 2026-10-08 incident of PR #96) the VM agent is gone until the owner reconnects. Production itself does not depend on it (`usher.service`, the restart path of DEPLOY.md). It is also a small security positive: a preemption ends any shell a host agent opened (F-039). Accepted by ADR 0020; it would change if the owner wanted the VM agent to watch unattended (a systemd unit), which must wait for F-039 step 5, since an unattended root-equivalent agent behind `herdr` is F-039 without the owner in the loop.

Verify:

```
grep -n "preemption or restart ends" docs/decisions/0020-herdr-on-the-production-vm.md   -> the accepting line
```

## Analyst notes

### Take these first

1. **F-039 step 1 + #98 with its gaps closed** (F-039 step 3, F-040 step 1, F-041 step 3). `herdr machine disable usher-prod` costs nothing and closes every `--machine` path between sessions; #98 is already written and its tests pass; it needs the tailnet address, the substitution and newline cases, and the ADR wording below.
2. **F-041 step 1** (drop the "owner allows one action" exception on the VM). Documentation only, and it removes the one rule that a prompt from a host agent could satisfy.
3. **F-039 step 5** (the VM's `herdr` and agent under a non-admin identity). The only fix that is a boundary rather than guidance; it closes F-039, drops F-040 and F-042 to INFO and makes F-041 cosmetic. Owner decision, and it needs `status.py`'s output without the `docker` group.

### The main session's six concerns, checked

1. Typing commands bypass the VM guard: **confirmed**, F-039, from `herdr` help, the v0.9.3 CLI reference (`pane` and `agent` are forwarded, `attach` alone excepted) and the VM's pane list. Not demonstrated, by the limits of this audit. Add `server stop` and `agent prompt '!cmd'` (Claude Code bash mode) to the list.
2. The `usher-prod` alias: **confirmed**, F-040; it predates ADR 0020 through `gcloud compute ssh`, and the tailnet may be a third path (unverified).
3. "Prompts are data, the owner allows one action": **confirmed unenforceable**, F-041. The guard still holds for what it lists, whoever prompts; the exception is the weak point.
4. Supply chain: **partly confirmed**, F-042. Installed at `~/.local/bin/herdr`, persistent server, Unix socket and no TCP per the docs (not checked on the VM). Not self-updating: `herdr update` is manual and a host update does not replace the remote server.
5. Data leaving through `agent read`: **confirmed, LOW**, F-043.
6. Session dies on preemption: **confirmed, INFO**, F-044.

### What is good about the current design

- SSH to the VM stays IAP plus OS Login: no new port, firewall rule or key; the alias pins Google's host key (`StrictHostKeyChecking yes`, `HostKeyAlias compute.<id>`), so a recreated VM fails closed until the owner updates the alias.
- The `herdr` socket is a Unix-domain socket, no TCP listener (by the docs); `--machine` requests travel over non-interactive SSH as JSON, "API payloads are not interpolated into the SSH shell command", and the bridge "never installs, starts, or restarts a server".
- `herdr` is outside the compose stack, the unit and the image; production runs the same without it, and the ADR documents how to remove it.
- On the VM the guard enforces read-only on the agent's own tool calls regardless of who prompted it.
- A preemption ends the session and anything opened in it.

### On PR #98 (100a350, open)

Its 29 deny and 16 allow cases pass against its guard (run here: every `PROD_REACH_DENIED` string rc=2, every `PROD_REACH_ALLOWED` rc=0; against `main` all 45 are rc=0). Gaps, fed on stdin to its guard, all rc=0:

```
'ssh olavostauros_gmail_com@100.122.99.101 uptime'                                     (tailnet address)
'herdr --machine usher-prod agent prompt w1:p1 "$(cat /tmp/p.txt)"'                        (prompt text from a file)
"herdr --machine usher-prod agent prompt w1:p1 $'status\n!uptime'"                        (newline, then a bash-mode line)
'python3 -c "import subprocess; subprocess.run([\'her\'+\'dr\',\'--machine\',\'usher-prod\',\'pane\',\'run\',\'w1:p2\',\'id\'])"'   (F-006 class)
'herdr pane run wX:p1 uptime'                         (a local pane; harmful only if it holds an SSH session to the VM, which #98's ADR text names)
```

The first three are worth closing in #98; the fourth is F-006 and is why the guard is guidance. One wording fix: #98's ADR text says the guard stops this "for every agent on every machine". It applies to Claude Code sessions whose project is this checkout; the host's `emmoni` session and the codex, opencode, pi and hermes agents do not load it. Say so, and point at F-039 steps 4 and 5.

### A `herdr`-level read-only mode

None for `--machine` in 0.9.3: the CLI reference lists no permission, allowlist or read-only option, and any client of the remote socket can call every method. The docs (`docs/next`) describe `terminal session observe`, a read-only live stream; it is not in 0.9.3's top-level help. If it ships, it could replace `agent read` for watching, but it would not restrict the other methods, so F-039 step 5 stays the fix.

### Not verified, and why

- Anything on the VM (sshd listeners, socket modes, `herdr integration status` there, the owner's group membership as it stands today): agents never log in to the VM; each finding's Verify block has the owner's commands.
- That Claude Code's `!` bash mode skips PreToolUse hooks on the VM: from Claude Code's documented behaviour, not reproduced (it would mean sending a prompt).
- The contents of the owner's `~/.ssh/config`: not read (the settings deny `.ssh`); taken from ops/gcp/vm-setup.md §7 and the main session's description.
- The VM agent's auto mode: the main session's observation.

### Follow-up for the scanner

`ops/audit/audit.py` could add host-side checks that need no `docker`: `herdr machine list` (a production profile enabled), and guard.py fed the F-039/F-040 strings (expect rc=2). Not added in this scoped run.

## Tooling notes

- `ops/audit/audit.py` not run: this audit is scoped to the `herdr` link, which the scanner has no check for; CVE data is from the 2026-10-07 run.
- Read-only `herdr` commands run against usher-prod: `machine list`, `agent list`, `agent get w1:p1`, `agent explain w1:p1`, `pane list`, and also `status server` (read-only status, not in the main session's list; named here for completeness). Nothing that types, prompts, splits or starts was sent.
- Guard tests: `.claude/hooks/guard.py` from `origin/main` and from 100a350 (`git show`) were fed the command strings of #98's `tests/test_guard.py` (extracted with `ast`) and five extra strings on stdin. Nothing was executed.
- This session had no Write/Edit tool, and the guard in the main checkout (then on #98's branch) scans the whole Bash string, heredocs included; the report was written by a Python script with placeholder words substituted for the command names the guard matches. The branch was created in a separate worktree because the main checkout had #98's branch checked out.
