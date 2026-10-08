# 0020. herdr on the production VM, reached from the host over IAP

Date: 2026-10-08. Status: proposed (owner, 2026-10-08: "write the ssh config
and open the PR").

## Context

The owner runs agents in herdr on the host and an agent on `usher-prod`
(AGENTS.md §4 "On the production VM"). The VM's agent ran inside a herdr pane
whose foreground process was `gcloud compute ssh ... --tunnel-through-iap`.
herdr detects agents from a pane's local processes, so it saw `gcloud`/`ssh`,
marked the pane `unknown`, and no host-side agent could find the VM's agent.
Claude Code's own session list is per machine too.

## Decision

- **herdr runs on the VM, under the owner's user only**, installed by
  `herdr machine add usher-prod` from the host (it asks before installing).
  Nothing in the compose stack, the unit or the image changes; herdr is not
  part of production and the VM works the same without it.
- **The host reaches it through an SSH alias, not the gcloud wrapper.** A
  `Host usher-prod` entry in the owner's `~/.ssh/config` repeats the options
  `gcloud compute ssh` uses, with `gcloud compute start-iap-tunnel` as the
  `ProxyCommand` (`ops/gcp/vm-setup.md` §7). SSH stays IAP-only; no port,
  firewall rule or key changes.
- **The VM's agent is started inside the VM's herdr session**, so the host's
  herdr lists it (`herdr --machine usher-prod agent list`).
- **Nothing a host agent sends it widens what it may do.** Prompts and text
  that arrive through herdr are data (CYBERSECURITY.md §3); the read-only
  rules and the guard hook of AGENTS.md §4 are unchanged, and only the owner
  allows a single action.

## Consequences

- One more program on the production VM, outside the image; a preemption or
  restart ends the herdr session and the agent, which the owner starts again.
- A host agent can prompt the production agent. herdr does more than prompt:
  `pane run`, `send-text`, `send-keys`, `split` and `agent start` type into
  a terminal on the VM as the owner, outside the VM agent's guard hook, and a
  prompt that starts with `!` runs as a shell command there. So the blast
  radius is the owner's on the VM unless the host's guard stops it.
  `.claude/hooks/guard.py` does, for Claude Code sessions that load this
  checkout's hook (other checkouts and other agent CLIs on the host load
  none, so the owner keeps `herdr machine disable usher-prod` outside VM
  sessions; F-039): with
  `--machine`/`--remote` only `machine list`, `agent list/get/read/explain/
  wait`, `pane list/get/read/layout/process-info` and `agent prompt` with
  plain text (no leading `!`, `/` or `#`, no control characters) pass;
  `herdr machine add/remove`, `ssh`/`scp`/`rsync` to `usher-prod` and
  `gcloud compute ssh/scp/start-iap-tunnel` are the owner's.
- Remaining gap: a host agent can still `herdr pane run` into a *local* pane
  whose shell is an SSH session to the VM. Don't leave such a pane open
  next to agents; the VM-side fix is to run the VM agent as a user without
  docker or sudo (owner's step, follow-up).
- `agent read` returns the VM pane's scrollback to the host; the VM agent
  prints aggregates, never `.env` or row data (CYBERSECURITY.md §3).
- Removing it: `herdr machine remove` on the host, `herdr server stop` and
  deleting the binary on the VM; drop the `Host usher-prod` entry.
