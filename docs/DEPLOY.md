# docs/DEPLOY.md — Usher on Google Cloud

How the Usher operation (the crawler, the shared database, the
events API, its tunnel and the dashboards) moves from the owner's computer
to Google Cloud without spending the prepaid credit faster than it lasts.

This is a runbook for the host owner and for the `atlas` agent
(`.claude/agents/atlas.md`). It does not change AGENTS.md: the `runtime`
image is still the unit of release (ADR 0001), the compose file is still the
wiring, and the decision to move is ADR 0019 (proposed until the VM serves
the public API). Status: **in progress** (written 2026-10-07; region
`southamerica-east1` chosen by the owner the same day). **Where it stands
(2026-10-07, 17:50 UTC):** **production runs on the VM.** Steps 0-9 done:
the scheduler, the API, the tunnel connector and Grafana run on
`usher-prod` from build `046d062`, `usher.service` is enabled, and the
host's `evcrawl`, `api` and `cloudflared` are stopped. The first step 5 VM
was created, tested and deleted the same day because its 80 GB disk was
sized for the self-hosted geocoder that ADR 0018 removed; the redo started
at 10 GB and was **grown online to 20 GB** during step 9 (the 10 GB sizing
counted `pgdata` but not the images, the build cache and the swapfile, and
the first release filled it). `usher-restart` already survived a real
preemption (17:21 UTC, restarted 17:25 UTC, mid step 6, nothing lost).
**Next: step 10, three days of watching (atlas), then step 11.**

## 1. The money

Read from the billing console on 2026-10-07 (account "Minha conta de
faturamento", `01F985-1C44C6-40CDE9`):

| Item | Value |
|------|-------|
| Account type | **Free Trial**, not upgraded |
| Credit | **R$ 1.779,29**, 100% left, one-off |
| Credit window | 2026-10-07 → **2027-01-06** (91 days) |
| Spend so far | R$ 0,00 |
| Projects on the account | `My First Project` (`project-7a10ce5d-d61a-44da-b28`), empty |

What that means for Usher:

- **The ceiling is R$ 19,55/day** (credit ÷ 91 days). Anything above that
  stops the stack before 2027-01-06. **Our target is ≤ R$ 15/day, a monthly
  budget of R$ 450**, which spends about 76% of the credit by the end of the
  window and keeps ~R$ 400 for the import day, mistakes and the cut-over.
- **A trial account never charges the card.** When the credit is gone or the
  window ends, Google stops the resources; the risk is the stack going dark
  early, not a bill. That changes the moment someone clicks **"Fazer
  upgrade"**: from then on spend beyond the credit is real money. **Upgrading
  is the owner's decision alone**; no agent clicks it, ever.
- **Trial limits:** at most 8 vCPUs running at once, no GPUs. The plan below
  uses 2.
- **Before 2026-12-15 the owner decides what happens after 2027-01-06**
  (upgrade and keep paying, move back to the host, or another provider).
  Per the trial terms, resources of an ended trial are deleted after a grace
  period; check the exact rule in the console when deciding. Section 8 is
  the way back.

### Budget guardrails (built in step 2, before anything that costs)

1. **Budget `usher-monthly`**: R$ 450/month on the billing account, scoped
   to the `usher-prod` project, counting cost **before** credits
   (`exclude-all-credits`; otherwise the trial credit hides every real cost), alerts at 50%, 75%, 90%, 100% of actual
   spend and 100% of forecast, e-mailed to the billing admins.
2. **Credit budget `usher-credit`**: a second budget of R$ 1.600 (90% of
   the credit) over a custom period 2026-10-07 → 2027-01-06, alerts at 50%,
   75%, 90%, 100%. It catches a slow leak the monthly one forgives.
3. **Hard stop.** Budgets only alert. Both budgets publish to the Pub/Sub
   topic `usher-budget`; a small Cloud Run function `usher-budget-cap`
   first **pauses** the Cloud Scheduler job `usher-restart` (otherwise it
   would start the VM again in 5 minutes), then stops every VM labelled
   `usher=prod`, when actual spend reaches 100% of either budget. Stopping
   keeps the disks (cents per day); the owner resumes the job and starts
   the VM after looking. The function, topic and job sit in the free tier.
4. **Labels on everything**: `usher=prod`, so the cost table filters on it
   and the cap finds what to stop.
5. **Daily burn check** by `atlas` on every session: the billing overview
   (Chrome, read-only) or the BigQuery billing export, compared with the
   R$ 15/day target. Two days in a row above R$ 19,55 is a stop-and-ask.

### Things that quietly eat credit (do not create them)

| Resource | Why it is out |
|----------|---------------|
| Cloud NAT, load balancers, static IPs | Fixed hourly fees. The VM uses one ephemeral IPv4 for outbound; inbound is Tailscale and the Cloudflare tunnel, both outbound-initiated |
| Cloud SQL, Memorystore, GKE | Managed minimums several times the whole VM. Postgres and Redis stay containers |
| Ops Agent / Cloud Logging of container logs | Ingestion fees; Docker's local `json-file` logs with rotation are enough, Grafana is the dashboard |
| pd-ssd, extreme disks, multi-regional snapshots | pd-balanced and regional snapshots are enough |
| Artifact Registry pulls across regions | The image is built on the VM from the tagged commit, or pulled from `ghcr.io` (ADR 0005) |
| A second always-on VM (the `dev` workspace) | The team workspace stays on the owner's host (section 3) |
| An on-demand VM | R$ 460/month for `e2-standard-2` alone; the VM is Spot (D4, §2) |

## 2. Estimate before creating (step 0)

Prices verified 2026-10-07 from the Cloud Billing Catalog API in BRL
(`cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus?currencyCode=BRL`
with `gcloud auth print-access-token`; the console's price list is too slow
to filter). Unit prices, `southamerica-east1`, on-demand vs Spot:

| SKU | On-demand | Spot |
|-----|----------:|-----:|
| E2 vCPU, per hour | R$ 0,2054 | R$ 0,0451 |
| E2 RAM, per GiB-hour | R$ 0,0275 | R$ 0,0060 |
| External IPv4, per hour | R$ 0 for the first 744 h/month, then 0,0297 | R$ 0,0148 |
| Balanced PD, per GiB-month | R$ 0,8896 | |
| Standard snapshot, per GiB-month | R$ 0,4745 | |

`e2-standard-2` on-demand is R$ 460/month for the VM alone, ~R$ 550 with
disk and snapshots: 93% of the credit by 2027-01-06, no margin. **The owner
chose Spot (D4, 2026-10-07).**

| Line | Size | R$/month (verified 2026-10-07) |
|------|------|-------------------------------:|
| VM `usher-prod`, `e2-standard-2` **Spot** (2 vCPU, 8 GB) | 730 h | 101 |
| External IPv4 on a Spot VM | 730 h | 11 |
| Boot + data disk, pd-balanced | **20 GB** since 2026-10-07 17:47 UTC (grown online from 10 GB during step 9): the host's `pgdata` measured 618.8 MB, but the first release filled the 10 GB disk (base system ~2.5 GB, 2 GiB swapfile, ~2.8 GB of images, ~1 GB build cache, ~1 GB volumes); the deleted VM had 80 GB (R$ 71) for the geocoder ADR 0018 removed | ~18 |
| Snapshot schedule, 7 daily, regional | a few GB stored, incremental | ~3 |
| Egress (API through the tunnel; crawled pages are ingress, free) | a few GB | < 10 |
| Pub/Sub, Cloud Run function, Cloud Scheduler (≤ 3 jobs), budgets | free tier | 0 |
| **Total** | | **~135** (~R$ 410 by 2027-01-06, 23% of the credit). Before ADR 0018 the plan said ~210 |

**Gate:** if a later re-estimate goes above R$ 450, trim in this order: a
smaller disk, fewer snapshots, then ask the owner (the region is fixed at
`southamerica-east1`, D1). Never shrink below what Postgres
needs (section 4).

### Spot means interruptions

Google may stop a Spot VM at any time (30 s notice). The VM is created with
`--provisioning-model=SPOT --instance-termination-action=STOP`, so the disk
and everything on it stay. A Cloud Scheduler job `usher-restart` calls the
Compute API `instances.start` on `usher-prod` every 5 minutes (a no-op while
it runs); compose services are `restart: unless-stopped` and `usher.service`
starts them on boot, so the crawler and the API come back by themselves.
Expect a few minutes of `/healthz` failures per interruption; Cloudflare
answers 502/530 meanwhile. If Spot capacity is gone in the zone for hours,
the owner decides whether to switch the VM to on-demand for a while (it
costs ~R$ 15/day: budget it first).

**Gap seen 2026-10-08 (proposal, the owner decides):** after the 15:12 UTC
preemption every container came back, but `api` started without its compose
network: nothing published on `:8000`, `cloudflared` could not reach
`api:8000`, Cloudflare answered 502 for ~2 h 54 min, and nothing alerted
(§9 "Log"). `restart: unless-stopped` does not help, because the container
never exits; it only turns `unhealthy`. Two ways to close it:

1. **A post-boot check (small, proposed first):** a second oneshot after
   `usher.service` (or an `ExecStartPost=`) that waits up to ~3 minutes for
   every service to be `healthy` and runs
   `docker compose up -d --force-recreate --no-deps <service>` once for any
   that is not, logging what it did. Lives in `ops/gcp/`, installed by
   `vm-setup.sh service`; no new external service.
2. **Bring forward freshness spec 3's container healthcheck and Discord
   alert** (`docs/specs/phase-3-freshness.md`): it tells the owner, it does
   not repair; it is the larger piece and also covers failures outside boot.

Neither is built until the owner picks; 1 does not replace 2.

## 3. What moves and what stays

| Piece | Today (host) | On Google Cloud |
|-------|--------------|-----------------|
| `db` (PostGIS) | compose, `pgdata18` (PostgreSQL 18 since F-015, ops/host/README.md "PostgreSQL 18") | same service, same image, on the VM; data by `pg_dump`/`pg_restore` |
| `redis` | compose | same, on the VM |
| geocoder | the public Nominatim, cached in `geocode_cache` (ADR 0018) | same; the cache travels with the dump, no service |
| `evcrawl` (scheduler) | compose | same; **only one scheduler crawls at a time**: the host's is stopped before the VM's starts |
| `api` | compose, `DEV_SSH_BIND:8000` | same, bound to the VM's tailnet address |
| `cloudflared` | compose, tunnel `usher-api` | same tunnel id on the VM; both connectors overlap for minutes during cut-over, then the host's stops |
| `grafana` | compose, `DEV_SSH_BIND:3000` | same, bound to the VM's tailnet address |
| `dev` (team workspace) | compose | **stays on the host** (owner decision D2) |

The host keeps its `db` as the team's development database (AGENTS.md §4
"One shared database"), refreshed from a production dump when the owner
says so. Production data is the VM's.

## 4. Sizing

The compose limits add up to ~16 GB with `dev`; without it, and with the
production values below in the VM's `.env`, they fit 8 GB:

```
DB_MEMORY_LIMIT=2g
REDIS_MEMORY_LIMIT=256m
EVCRAWL_MEMORY_LIMIT=1536m
API_MEMORY_LIMIT=512m
CLOUDFLARED_MEMORY_LIMIT=128m
GRAFANA_MEMORY_LIMIT=384m
```

Plus 2 GB of swap on the VM as a cushion. Without a self-hosted geocoder
(ADR 0018) the limits sum to ~4.8 GB, which leaves room on 8 GB. Disk: the
owner measures the `pgdata` volume on the host first (`docker system df -v`),
and the disk is that size × 1.5 **plus ~8 GB** for what lives next to it
(the base system, the 2 GiB swapfile, the five images with two `evcrawl:dev`
builds side by side, the build cache; measured ~7 GB on 2026-10-07), rounded
up: 20 GB today. A disk sized on `pgdata` alone fills at the first release
(step 9 did). If it is above ~65 GB, revisit the estimate. Growing is
online: `gcloud compute disks resize` (`ops/gcp/README.md` "The VM"), then
`growpart` and `resize2fs` on the VM (`ops/gcp/vm-setup.md` §6).

## 5. Shape of the deployment

- **Project** `usher-prod`, new, linked to the trial billing account
  (`My First Project` is left alone). Only the APIs used: Compute Engine,
  Pub/Sub, Cloud Run, Cloud Build (for the function), Cloud Scheduler,
  Cloud Billing Budget, BigQuery (billing export).
- **Network** `usher-vpc`, custom mode, one subnet in the chosen region.
  The default network and its `allow-ssh`/`allow-rdp`/`allow-icmp` rules
  from `0.0.0.0/0` are not used. One ingress rule: TCP 22 from
  `35.235.240.0/20` (IAP) only. Nothing else listens publicly; the API is
  public only through Cloudflare (ADR 0015) and everything else is tailnet.
- **VM** `usher-prod`, Debian 12, `e2-standard-2` Spot (`STOP` on
  preemption), zone `southamerica-east1-b` unless capacity says otherwise,
  Shielded VM on, label `usher=prod`, no service account at all (the stack
  needs no Google API), OS Login, no project-wide SSH keys.
- **On the VM**: Docker Engine + compose plugin from Docker's apt repo,
  `/etc/docker/daemon.json` with `"log-driver": "json-file"` and
  `"log-opts": {"max-size": "20m", "max-file": "5"}`, Tailscale joined to the
  team tailnet by the owner (interactive login, never an auth key handled by
  an agent), the repo checked out at the release tag, `.env` written by the
  owner from 1Password (vault `usher`), `ops/host/cloudflared/credentials.json`
  copied by the owner (mode 600, never read by an agent), `usher.service`
  installed like on the host.
- **Backups**: a daily snapshot schedule (7 days) on the data disk, plus
  `ops/backup.sh` (`pg_dump`) as on any release deployment.

## 6. Steps

Each step says who runs it. **owner** = the person, by hand (anything with
`docker`, logins, secrets, the billing upgrade question). **atlas** = the
agent, from the owner's host session, with `gcloud` and the Chrome console,
asking before every step that creates or deletes something.

| # | Step | Who | Done when |
|---|------|-----|-----------|
| 0 | Fill the estimate in §2, pick region and machine, update ADR 0019 | atlas, owner approves | **Done 2026-10-07**: São Paulo, `e2-standard-2` Spot, ~R$ 210/month then (~R$ 135 with the smaller disk of the redo) |
| 1 | Install the Google Cloud CLI on the host (`gcloud`, not in the image: it is a host-owner tool; installed 2026-10-07 under `~/.local/share/google-cloud-sdk`, 588.0.0), `gcloud auth login` | owner | `gcloud config list` shows the owner's account |
| 2 | Create project `usher-prod`, link billing, enable only the APIs in §5; create budgets `usher-monthly` and `usher-credit`, topic `usher-budget`, billing export to BigQuery dataset `billing` | atlas | **Done 2026-10-07** except the export: project `usher-prod` (number 805713488595) linked; budgets `usher-monthly` (`bbd63138-…`) and `usher-credit` (`711516d4-…`) with `--credit-types-treatment=exclude-all-credits` (with credits included, a trial budget always sees R$ 0), both on topic `usher-budget`; dataset `usher-prod:billing` in `southamerica-east1`. The export itself is a console-only form: the owner turns on "Exportação de faturamento → BigQuery → Custo de uso padrão" into that dataset |
| 3 | Write and deploy `usher-budget-cap` (Cloud Run function, Python, stops `usher=prod` VMs at 100%). Tested right after step 5, before anything runs on the VM, by publishing a fake budget message at 100% | atlas (code in `ops/gcp/budget-cap/`, own PR) | **Deployed 2026-10-07**: Cloud Run function `usher-budget-cap` (gen2, `python312`, `southamerica-east1`, 256 MiB, max 1 instance, internal-only ingress, no unauthenticated calls) on topic `usher-budget`, running as service account `usher-budget-cap` with `compute.instanceAdmin.v1`, `cloudscheduler.admin`, `run.invoker`, `eventarc.eventReceiver`. Under-budget smoke message logged "under budget, nothing to do"; the 100% test passed against the first VM (step 5). Redeploy: the `gcloud functions deploy` line in `ops/gcp/README.md`. The function finds the VM by label, so the redone VM needs no change here |
| 4 | Create `usher-vpc`, subnet, the IAP-only firewall rule | atlas | **Done 2026-10-07**: `usher-vpc` (custom), subnet `usher-sp` 10.10.0.0/24, rule `usher-allow-iap-ssh` (TCP 22 from 35.235.240.0/20, target tag `usher-prod`). The project's auto-created `default` network still has its 0.0.0.0/0 SSH/RDP/ICMP rules; nothing uses it; deleting it is the owner's call |
| 5 | Create the Spot VM (+ swap, snapshot schedule, label, no service account) and the `usher-restart` Cloud Scheduler job | atlas | **Done 2026-10-07 (redone 16:54 UTC, VM up).** `usher-prod` in `southamerica-east1-b`, `e2-standard-2` Spot (`STOP`), Debian 12 (`debian-12-bookworm-v20260921`), **10 GB** pd-balanced auto-delete boot disk (`pgdata` measured 618.8 MB, §4; grow online if the copied volumes ever need it), Shielded VM (secure boot, vTPM, integrity monitoring), OS Login, project SSH keys blocked, **no service account at all** (`--no-service-account --no-scopes`), network `usher-vpc`/`usher-sp`, internal 10.10.0.3, one ephemeral IPv4 (no static address), tag `usher-prod`, label `usher=prod` on the instance and on the disk, swap from `ops/gcp/startup.sh` (boot metadata; the serial log shows the 2 GiB swap made at first boot). The disk carries the snapshot schedule `usher-daily` (06:00, 7 days, regional, kept from the first pass). Job `usher-restart` every 5 min (America/Sao_Paulo) as service account `usher-restart` (`compute.instanceAdmin.v1`), **resumed** right after the VM was verified; its first run (16:55 UTC) was a no-op start on the running VM. The exact create line is in `ops/gcp/README.md` "The VM". **History**: the first pass (80 GB, VM up 14:48 UTC) passed the **cap test** (a fake 100% alert paused the job and stopped the VM; resuming the job restarted it in 18 s) and was deleted the same day at the owner's request because the 80 GB disk was sized for the self-hosted geocoder ADR 0018 removed; no snapshot was taken, the policy, the network, the budgets and the function stayed. The cap finds the VM by label, so the redo needed no change there and the test was not repeated |
| 6 | On the VM: Docker, daemon.json, Tailscale, clone at the release tag, `.env`, tunnel credentials, `usher.service` | owner, following `ops/gcp/vm-setup.md` (script `ops/gcp/vm-setup.sh`: packages, deploy key, clone, env, service; unit `ops/gcp/usher.service` is the host's without `dev`), written 2026-10-07 | `tailscale status` shows the VM; `docker compose config` passes. **Done 2026-10-07 (redone 17:40 UTC on the recreated VM)**: Docker + compose, `json-file` logs, 2 GiB `/swapfile` active, tailnet `usher-prod` **100.122.99.101** (joined under the right name, the old node was gone), read-only deploy key `usher-prod` (id 165698117, added 17:17 UTC; the first VM's key was already deleted), checkout `~/usher` at `main` (`215a6e6`, the merge of PR #77), `.env` and tunnel credentials copied by the owner with `gcloud compute scp` (credentials mode 600), `DEV_SSH_BIND` set to the tailnet address, `CLOUDFLARED_USER` set, `usher.service` installed and **disabled** until step 9. `docker compose config --quiet` passed. The VM was **preempted at 17:21 UTC** between the clone and the secrets copy; `usher-restart` had it back at 17:25 UTC and the disk kept everything, so the step only needed the two copies retried. **History**: the first pass (tailnet 100.100.78.57, checkout `fd540e9`) died with the first VM's disk; its deploy key and tailnet node were removed before the redo |
| 7 | Copy the data: `pg_dump -Fc` on the host → `pg_restore` on the VM. Roles do not travel (no password hashes leave the host): before the restore, create `grafana_ro`, `api_ro` and `team` NOLOGIN with the flags of `db/roles.py`; after it, build the runtime image on the VM (`api` has no `build:` of its own, it reuses the `evcrawl` service's `evcrawl:dev`) and run the `evcrawl migrate` one-off with the three role passwords (§5 of AGENTS.md), which is a no-op upgrade that sets the passwords, the bounds and `team`'s grants | owner | Row counts of `events`, `venues`, `source_listings`, `geocode_cache` match. **Done 2026-10-07 17:38 UTC**: `pg_dump --format=custom --no-owner` through the host's `db` container, `gcloud compute scp` to the VM, the three roles created NOLOGIN with the flags of `db/roles.py`, `pg_restore --no-owner` **without** `--exit-on-error` (the dump carries the `tiger`/`topology` schemas the PostGIS image already makes, so exactly 3 "already exists" errors are ignored; with the flag the restore stops before any of our tables), counts matched exactly (events 22363, venues 9930, source_listings 22378, geocode_cache 220), runtime image built on the VM, migrate one-off ran `0009 → 0010` (the host is still at `0009`) and reported `grafana_ro` and `api_ro` "exists, bounded, login set", `team` "exists, login set". `redis` came up with the one-off. Gotcha met on the way: with Tailscale managing DNS the VM could not resolve the registry (`server misbehaving` from MagicDNS); `sudo tailscale set --accept-dns=false` fixed it and is now in `ops/gcp/vm-setup.md` |
| 8 | Start on the VM without the scheduler (image built in step 7): `db redis api grafana`, then `cloudflared` (now two connectors) | owner | `curl https://api.eventolivre.com/healthz` → `db: ok` from both connectors (check `cloudflared tunnel info usher-api`). **Done 2026-10-07 17:41 UTC**: `db redis api grafana` up on the tailnet address, `/healthz` on `100.122.99.101:8000` → `db: ok`; `cloudflared` prechecks all PASS, and the public URL answered with the VM's `git_sha` (`215a6e6`, which the host's `682ac4f` build could not produce), so the second connector carried traffic. `cloudflared tunnel info` needs the origin certificate the container does not have; the sha in `/healthz` is the simpler proof of which connector answered |
| 9 | Cut-over: stop `evcrawl`, `api`, `cloudflared` on the host; release on the VM (`ops/host/release.sh`, the TicketBolt smoke caveat in AGENTS.md §13 applies), start `evcrawl`, then `systemctl enable usher.service` (disabled until now: a Spot restart would otherwise start a second scheduler and tunnel connector) | owner | `ops/host/status.py` on the VM: scheduler up, fresh runs; the public API answers from the VM only. **Done 2026-10-07 17:50 UTC**: host's `evcrawl`, `api`, `cloudflared` stopped 17:44 UTC (`db`, `redis`, `dev`, `grafana` stay for the workspace); on the VM `git pull` to `046d062`, then `release.sh`: the first run died at the image export with **no space left on device** (10 GB disk 100% full, see §2 and §4), the owner approved growing it to **20 GB** online (`gcloud compute disks resize`, then `cloud-guest-utils` + `growpart /dev/sda 1` + `resize2fs`, no reboot, 47% used after), the second run built `046d062`, migrate was a no-op with all three logins set, the smoke passed on four sources and stopped on TicketBolt's waiting room as on the host, so `up -d evcrawl api` by hand; scheduler started 17:50:28 UTC with 11 jobs over 32 cities, first heartbeat ok; `systemctl enable usher.service` → `enabled`. The host's `usher.service` still listed the three stopped services and would have started them at the next boot: fixed with security finding F-030 (`docs/security/reports/2026-10-07-usher-stack.md`), the unit now starts `db redis dev grafana` only; the owner reinstalls it (`ops/host/README.md` "Production on Google Cloud") |
| 10 | Watch three days: daily burn vs R$ 15, scheduler heartbeat, `/healthz`, disk use | atlas | Three daily lines in §9 "Log". **Day 0 (2026-10-07, ~18:00 UTC)**: VM `RUNNING` since 17:25 UTC on the 20 GB disk, `usher-restart` `ENABLED` (last no-op start 17:55 UTC), one preemption today (17:21 UTC), `/healthz` `046d062` and `db: ok` three times, `/v1/events?city=vitoria&limit=1` → 200; the billing console still shows R$ 0,00 and 100% of the credit, and says itself that costs can take more than a day to appear, so the first line with real VM hours is 2026-10-08's or 2026-10-09's. The heartbeat and disk use are the owner's `status.py`/`df -h /` on the VM; atlas reads only the public URL and `gcloud` |
| 11 | Docs: `ops/host/README.md` "Production on Google Cloud", AGENTS.md §13 live state, ADR 0019 accepted, cerberus re-audit of the VM | atlas, owner merges | PR merged |

Steps 0-5 cost almost nothing until the VM exists; the VM's clock starts in
step 5. Do steps 5-9 in one sitting if possible.

## 7. Release on the VM afterwards

Same as the host (`ops/host/README.md` "Release the crawler"): `git pull`
to the tag, `ops/host/release.sh`, run by the owner over
`gcloud compute ssh usher-prod --tunnel-through-iap`. `ops/host/status.py`
works there unchanged. Grafana and the API's tailnet port are on the VM's
tailnet name.

## 8. The way back (and the end of the trial)

Reverse of step 9: stop `evcrawl` on the VM, `pg_dump` it, restore on the
host, start the host's stack, start the host's `cloudflared`, stop the VM's.
Then stop the VM (keeps the disk) or, once the owner says so, delete the
project (`gcloud projects delete usher-prod` is the owner's command; it
removes everything and stops all spend).

## 9. Log

One line per day once the VM runs: date, spend yesterday (R$), credit
left (R$), days left, scheduler up?, `/healthz`.

| Date | Spend (R$) | Credit left (R$) | Days left | Scheduler | API |
|------|-----------:|-----------------:|----------:|-----------|-----|
| 2026-10-07 | 0,00 | 1.779,29 | 91 | host | host |
| 2026-10-07 (step 2) | 0,00 | 1.779,29 | 91 | host | host |
| 2026-10-07 (step 5, VM up 14:48 UTC) | 0,00 | 1.779,29 | 91 | host | host |
| 2026-10-07 (VM deleted, `usher-restart` paused; redo steps 5-6 with a smaller disk) | 0,00 | 1.779,29 | 91 | host | host |
| 2026-10-07 evening (docs refresh: no instance, job paused, deploy key gone, tailnet node still listed) | 0,00 | 1.779,29 | 91 | host | host |
| 2026-10-07 evening (owner removed the tailnet node; `pgdata` 618.8 MB → 10 GB disk; step 5 redo is next) | 0,00 | 1.779,29 | 91 | host | host |
| 2026-10-07 (step 5 redone, VM up 16:54 UTC, 10 GB disk, `usher-restart` resumed; console says 90 days left; next: step 6) | 0,00 | 1.779,29 | 90 | host | host |
| 2026-10-07 (step 6 done 17:40 UTC; first Spot preemption 17:21 UTC, back 17:25 UTC via `usher-restart`; credit not re-read; next: step 7) | 0,00 | 1.779,29 | 90 | host | host |
| 2026-10-07 (step 7 done 17:38 UTC: data restored, counts match, VM `db` at `0010`; credit not re-read; next: step 8) | 0,00 | 1.779,29 | 90 | host | host |
| 2026-10-07 (step 8 done 17:41 UTC: `api`, `grafana`, `cloudflared` up on the VM, public URL answered from the VM's connector) | 0,00 | 1.779,29 | 90 | host | both |
| 2026-10-07 (step 9 done 17:50 UTC: host stopped, disk grown 10 → 20 GB, release `046d062`, scheduler on the VM, unit enabled; credit not re-read; next: step 10, first daily line on 2026-10-08) | 0,00 | 1.779,29 | 90 | **VM** | **VM** |
| 2026-10-07 (step 10 day 0, ~18:00 UTC: console re-read, R$ 0,00 for 1-7 Oct and 100% credit, billing lags ≥ 1 day so today's VM hours are not in yet; 1 preemption today) | 0,00 | 1.779,29 | 90 | VM (`046d062`, 1 preemption today) | VM (`046d062`, `db: ok` ×3, `/v1/events` 200) |
| 2026-10-08 (F-015, PR #86: VM `db` moved to PostgreSQL 18.6 / PostGIS 3.6.4 by dump and restore, ops/host/README.md "PostgreSQL 18"; writers and API down 11:22-11:31 UTC, no cycle missed; snapshot `usher-prod-pre-pg18-quiet` 11:22 UTC; after: 20,482 active events, 22,870 listings, 10,119 venues, as before; disk 55% → 66% with the old `pgdata`, the 263 MB dump and the 16-3.4 image kept until 2026-10-15; no preemption since 2026-10-07 17:25 UTC; credit not re-read) | — | — | 90 | VM (`046d062`, on 18 from 11:31 UTC) | VM (`046d062`, `db: ok`) |
| 2026-10-08 (incident: public API down ~15:16-18:10 UTC, Cloudflare 502; Spot preemption 15:12, back 15:15 via `usher-restart`, `usher.service` `up -d` OK at 15:16:10, but `usher-api-1` started without its compose network (only `lo`): nothing published on :8000, cloudflared could not reach `api:8000`, `/healthz` 503 every 30 s, no auth errors (log kept as `~/api-incident-2026-10-08.log` on the VM); fixed 18:10 by `docker compose up -d --force-recreate --no-deps api`, then `db: ok` and `/v1/events` 200, re-checked from the host 18:15; root cause of the failed attach not yet read from the docker journal; nothing alerts on an unhealthy `api` (§2 "Spot means interruptions", gap); checkout `f278a3a`, behind `origin/main`; credit not re-read) | — | — | 90 | VM (`046d062`, heartbeat fresh throughout) | VM (`046d062`, down ~2 h 54 min, back 18:10 UTC) |

## 10. Decisions for the owner (before step 0)

- **D1 Region**: **`southamerica-east1`, decided 2026-10-07.** If the
  step 0 estimate is above R$ 450, the gate in §2 trims the disk and the
  snapshots, not the region.
- **D2 Workspace**: **`dev` stays on the host** (taken as recommended,
  2026-10-07).
- **D3 After 2027-01-06**: upgrade, come back to the host, or elsewhere.
  Due 2026-12-15.
- **D4 Spot VM**: **yes, decided 2026-10-07**, with the `usher-restart`
  job restarting it (§2 "Spot means interruptions").
