# 0019. Production runs on one Google Cloud VM inside the trial credit

Date: 2026-10-07. Status: **accepted 2026-10-13** (`docs/DEPLOY.md` step 11;
proposed from 2026-10-07 until the VM had served the public API through the
three-day watch of step 10). The step 0 estimate was approved on
2026-10-07. Numbered 0016 until 2026-10-07, which collided with the
`events_upcoming` ADR of 2026-09-25; renumbered, no other change.

## Context

The whole stack runs on the owner's computer (ADR 0002), so the public API
(ADR 0015) and the scheduler go down when that machine does. The owner has
a Google Cloud Free Trial: R$ 1.779,29 of credit, 2026-10-07 to 2027-01-06,
not upgraded.

## Decision

- **Production moves to one Compute Engine Spot VM** (`usher-prod`,
  `e2-standard-2`, `southamerica-east1`, ~R$ 210/month with disk; on-demand
  would be ~R$ 550 and eat 93% of the credit), restarted by a Cloud
  Scheduler job after each preemption, in project `usher-prod`, running the same compose services minus `dev`, from the
  same release image and the same `ops/host/release.sh`. No managed
  database, cache or load balancer: their fixed minimums do not fit the
  credit.
- **The team workspace stays on the host**, with its own `db` as
  development data.
- **Money is a hard constraint**: ≤ R$ 450/month (planned ~R$ 210), two budgets and a
  Pub/Sub-driven function that stops the VM at 100%; nothing is created
  before the budgets. The trial is never upgraded without the owner.
- **Access is unchanged in shape**: public only through the Cloudflare
  tunnel (same tunnel, connector moves), everything else on the tailnet,
  SSH to the VM only through IAP.

## What shipped (2026-10-07 to 2026-10-13)

- **The VM**: `usher-prod`, `e2-standard-2` Spot (`STOP` on preemption),
  `southamerica-east1-b`, Debian 12, Shielded VM, no service account,
  label `usher=prod`, tailnet only plus the IAP SSH rule. Its disk is
  **20 GB** pd-balanced: created at 10 GB, grown online during the cut-over
  when the first release filled it (images, build cache and swap live next
  to `pgdata`); the first, 80 GB VM was deleted the same day (ADR 0018
  removed the geocoder it was sized for). Daily snapshots `usher-daily`
  (7 days, regional). Production on it since 2026-10-07 17:50 UTC; build
  `48ef7a9` on 2026-10-13.
- **Money**: budgets `usher-monthly` and `usher-credit` (before credits),
  topic `usher-budget` and the cap function `usher-budget-cap`, tested
  at 100% on the first VM. Real burn ~R$ 4,85/day (~R$ 145/month, under
  the ~R$ 210 planned, because the disk shrank), R$ 1.752,45 of credit
  left on 2026-10-13.
- **Spot restarts**: the Cloud Scheduler job `usher-restart` starts the VM
  every 5 minutes (a no-op while it runs); back within 2-4 minutes on
  every preemption so far. `ops/gcp/usher.service` starts compose at boot,
  and since PR #129 the tailnet bind sysctl plus `ops/gcp/usher-heal.sh`
  (its `ExecStartPost=`) recreate any service not healthy after boot.
- **PostgreSQL 18**: the VM's `db` moved from 16-3.4 to
  `postgis/postgis:18-3.6` on 2026-10-08 (F-015, PR #86), down 9 minutes.
- **Agents on the VM** (ADR 0020): the read-only `usher-ops` and
  `cerberus` run in the VM's herdr session as the Linux user `usher-agent`
  (no sudo, no docker), reading a status snapshot; the scanner runs as the
  owner through `usher-audit.service`.
- **Incidents**: on 2026-10-08 (~2 h 54 min) and 2026-10-11 the public API
  answered 502 after a preemption: `dockerd` restarted `api` before the
  tailnet address existed, and it came back without its network. Fixed in
  PR #129 (`docs/DEPLOY.md` §2 "Spot means interruptions"); nothing alerts
  yet (`docs/SPECIFICATION.md` §8.1).

## Consequences

- Two places run compose: the host (workspace, development `db`) and the
  VM (production). `AGENTS.md` §4 "Where you are", `ops/host/README.md`
  "Production on Google Cloud" and `ops/gcp/` describe each.
- After 2027-01-06 the stack stops unless the owner upgrades or moves it
  back (`docs/DEPLOY.md` §8); the decision (D3) is due 2026-12-15.
