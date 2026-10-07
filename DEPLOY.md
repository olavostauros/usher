# DEPLOY.md — Usher on Google Cloud

How the Usher operation (the crawler, the shared database, the geocoder, the
events API, its tunnel and the dashboards) moves from the owner's computer
to Google Cloud without spending the prepaid credit faster than it lasts.

This is a runbook for the host owner and for the `atlas` agent
(`.claude/agents/atlas.md`). It does not change AGENTS.md: the `runtime`
image is still the unit of release (ADR 0001), the compose file is still the
wiring, and the decision to move is ADR 0016 (proposed until the owner
accepts it). Status: **in progress** (written 2026-10-07; region `southamerica-east1`
chosen by the owner the same day).

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
| Cloud SQL, Memorystore, GKE | Managed minimums several times the whole VM. Postgres, Redis and Nominatim stay containers |
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
| Boot + data disk, pd-balanced | 80 GB (resize to the host's measured volumes × 1.5) | 71 |
| Snapshot schedule, 7 daily, regional | ~40 GB stored, incremental | ~19 |
| Egress (API through the tunnel; crawled pages are ingress, free) | a few GB | < 10 |
| Pub/Sub, Cloud Run function, Cloud Scheduler (≤ 3 jobs), budgets | free tier | 0 |
| **Total** | | **~210** (~R$ 640 by 2027-01-06, 36% of the credit) |

**Gate:** if a later re-estimate goes above R$ 450, trim in this order: a
smaller disk, fewer snapshots, then ask the owner (the region is fixed at
`southamerica-east1`, D1). Never shrink below what Nominatim and Postgres
need (section 4).

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

## 3. What moves and what stays

| Piece | Today (host) | On Google Cloud |
|-------|--------------|-----------------|
| `db` (PostGIS) | compose, `pgdata` | same service, same image, on the VM; data by `pg_dump`/`pg_restore` |
| `redis` | compose | same, on the VM |
| `nominatim` | compose, finished import | same; the finished `nominatim` volume is copied, not re-imported (an import on 8 GB takes too long and costs a day of a bigger VM) |
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
NOMINATIM_MEMORY_LIMIT=3g
NOMINATIM_SHARED_BUFFERS=512MB
NOMINATIM_MAINTENANCE_WORK_MEM=512MB   # only matters for an import; there is none
NOMINATIM_EFFECTIVE_CACHE_SIZE=2GB
NOMINATIM_SHM_SIZE=512m
EVCRAWL_MEMORY_LIMIT=1536m
API_MEMORY_LIMIT=512m
CLOUDFLARED_MEMORY_LIMIT=128m
GRAFANA_MEMORY_LIMIT=384m
```

Plus 2 GB of swap on the VM as a cushion. Disk: the owner measures the
`pgdata` and `nominatim` volumes on the host first
(`docker system df -v`), after freezing Nominatim (`ops/host/README.md`
"Geocoder", "Frozen": it drops the update tables the copy would carry), and the disk is that sum × 1.5, rounded up to
10 GB. If the measured sum is above ~65 GB, revisit the estimate.

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
  `ops/backup.sh` (`pg_dump`) as on any release deployment. Snapshots of
  `nominatim` are fine; it is the expensive thing to rebuild.

## 6. Steps

Each step says who runs it. **owner** = the person, by hand (anything with
`docker`, logins, secrets, the billing upgrade question). **atlas** = the
agent, from the owner's host session, with `gcloud` and the Chrome console,
asking before every step that creates or deletes something.

| # | Step | Who | Done when |
|---|------|-----|-----------|
| 0 | Fill the estimate in §2, pick region and machine, update ADR 0016 | atlas, owner approves | **Done 2026-10-07**: São Paulo, `e2-standard-2` Spot, ~R$ 210/month |
| 1 | Install the Google Cloud CLI on the host (`gcloud`, not in the image: it is a host-owner tool; installed 2026-10-07 under `~/.local/share/google-cloud-sdk`, 588.0.0), `gcloud auth login` | owner | `gcloud config list` shows the owner's account |
| 2 | Create project `usher-prod`, link billing, enable only the APIs in §5; create budgets `usher-monthly` and `usher-credit`, topic `usher-budget`, billing export to BigQuery dataset `billing` | atlas | **Done 2026-10-07** except the export: project `usher-prod` (number 805713488595) linked; budgets `usher-monthly` (`bbd63138-…`) and `usher-credit` (`711516d4-…`) with `--credit-types-treatment=exclude-all-credits` (with credits included, a trial budget always sees R$ 0), both on topic `usher-budget`; dataset `usher-prod:billing` in `southamerica-east1`. The export itself is a console-only form: the owner turns on "Exportação de faturamento → BigQuery → Custo de uso padrão" into that dataset |
| 3 | Write and deploy `usher-budget-cap` (Cloud Run function, Python, stops `usher=prod` VMs at 100%). Tested right after step 5, before anything runs on the VM, by publishing a fake budget message at 100% | atlas (code in `ops/gcp/budget-cap/`, own PR) | **Deployed 2026-10-07**: Cloud Run function `usher-budget-cap` (gen2, `python312`, `southamerica-east1`, 256 MiB, max 1 instance, internal-only ingress, no unauthenticated calls) on topic `usher-budget`, running as service account `usher-budget-cap` with `compute.instanceAdmin.v1`, `cloudscheduler.admin`, `run.invoker`, `eventarc.eventReceiver`. Under-budget smoke message logged "under budget, nothing to do". **Still open:** the 100% test against the fresh VM in step 5. Redeploy: the `gcloud functions deploy` line in `ops/gcp/README.md` |
| 4 | Create `usher-vpc`, subnet, the IAP-only firewall rule | atlas | **Done 2026-10-07**: `usher-vpc` (custom), subnet `usher-sp` 10.10.0.0/24, rule `usher-allow-iap-ssh` (TCP 22 from 35.235.240.0/20, target tag `usher-prod`). The project's auto-created `default` network still has its 0.0.0.0/0 SSH/RDP/ICMP rules; nothing uses it; deleting it is the owner's call |
| 5 | Create the Spot VM (+ swap, snapshot schedule, label, service account) and the `usher-restart` Cloud Scheduler job | atlas | **Done 2026-10-07**: `usher-prod` in `southamerica-east1-b`, `e2-standard-2` Spot (`STOP`), Debian 12, 80 GB pd-balanced (grow online when the copied volumes need it; the host's `pgdata` backup is ~315 MB compressed), Shielded VM, OS Login, project SSH keys blocked, **no service account at all**, tag `usher-prod`, label `usher=prod`, swap from `ops/gcp/startup.sh` (boot metadata). Snapshot schedule `usher-daily` (06:00, 7 days, regional). Job `usher-restart` every 5 min (America/Sao_Paulo) as service account `usher-restart` (`compute.instanceAdmin.v1`). **Cap test passed**: a fake 100% alert paused the job and stopped the VM; resuming the job restarted it in 18 s |
| 6 | On the VM: Docker, daemon.json, Tailscale, clone at the release tag, `.env`, tunnel credentials, `usher.service` | owner, following `ops/gcp/vm-setup.md` (script `ops/gcp/vm-setup.sh`: packages, deploy key, clone, env, service; unit `ops/gcp/usher.service` is the host's without `dev`), written 2026-10-07 | `tailscale status` shows the VM; `docker compose config` passes. **Done 2026-10-07**: Docker + compose v5.6.0, `json-file` logs, tailnet `usher-prod` 100.100.78.57, read-only deploy key `usher-prod`, checkout at `main` (`fd540e9`), `.env` and tunnel credentials copied by the owner, system unit installed and **disabled** until step 9 |
| 7 | Copy the data: freeze Nominatim on the host (`ops/host/README.md` "Frozen"; or the clipped reimport of "Clipped", ADR 0017, whose VM `.env` also needs `EVCRAWL_GEOCODE_FALLBACK_URL`), then `pg_dump -Fc` on the host → `pg_restore` on the VM; tar the stopped `nominatim` volume host → VM. Roles do not travel (no password hashes leave the host): before the restore, create `grafana_ro`, `api_ro` and `team` NOLOGIN with the flags of `db/roles.py`; after it, build the runtime image on the VM (`api` has no `build:` of its own, it reuses the `evcrawl` service's `evcrawl:dev`) and run the `evcrawl migrate` one-off with the three role passwords (§5 of AGENTS.md), which is a no-op upgrade that sets the passwords, the bounds and `team`'s grants | owner | Row counts of `events`, `venues`, `source_listings` match; Nominatim `/status` OK |
| 8 | Start on the VM without the scheduler (image built in step 7): `db redis nominatim api grafana`, then `cloudflared` (now two connectors) | owner | `curl https://api.eventolivre.com/healthz` → `db: ok` from both connectors (check `cloudflared tunnel info usher-api`) |
| 9 | Cut-over: stop `evcrawl`, `api`, `cloudflared` on the host; release on the VM (`ops/host/release.sh`, the TicketBolt smoke caveat in CLAUDE.md applies), start `evcrawl`, then `systemctl enable usher.service` (disabled until now: a Spot restart would otherwise start a second scheduler and tunnel connector) | owner | `ops/host/status.py` on the VM: scheduler up, fresh runs; the public API answers from the VM only |
| 10 | Watch three days: daily burn vs R$ 15, scheduler heartbeat, `/healthz`, disk use | atlas | Three daily lines in §9 "Log" |
| 11 | Docs: `ops/host/README.md` "Production on Google Cloud", CLAUDE.md live state, ADR 0016 accepted, cerberus re-audit of the VM | atlas, owner merges | PR merged |

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
