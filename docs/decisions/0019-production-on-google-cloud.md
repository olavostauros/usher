# 0019. Production runs on one Google Cloud VM inside the trial credit

Date: 2026-10-07. Status: proposed (`docs/DEPLOY.md`). It becomes accepted when
the VM serves the public API (step 9 of `docs/DEPLOY.md` §6). The step 0 estimate
was approved the same day. Numbered 0016 until 2026-10-07, which collided
with the `events_upcoming` ADR of 2026-09-25; renumbered, no other change.

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

## Consequences

- Two places run compose; `AGENTS.md` §4 "Where you are" and
  `ops/host/README.md` need a VM section once it is live.
- After 2027-01-06 the stack stops unless the owner upgrades or moves it
  back (`docs/DEPLOY.md` §8); the decision is due 2026-12-15.
