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
- A host agent can prompt the production agent. Its blast radius is the
  production agent's: read-only, enforced by the guard hook.
- Removing it: `herdr machine remove` on the host, `herdr server stop` and
  deleting the binary on the VM; drop the `Host usher-prod` entry.
