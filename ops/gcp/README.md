# ops/gcp — production on Google Cloud

The runbook is `docs/DEPLOY.md` at the repository root; this directory holds what
it deploys. Everything lives in project `usher-prod`, region
`southamerica-east1`, labelled `usher=prod`.

## The VM

`usher-prod`, created by `atlas` in `docs/DEPLOY.md` step 5 (first pass with
80 GB on 2026-10-07, redone the same day with 10 GB). Recorded here so a
future redo starts from the same line instead of rebuilding it from §5.
Run from the repository root, on the host, after the owner's go:

```bash
gcloud compute instances create usher-prod --project=usher-prod --zone=southamerica-east1-b \
  --machine-type=e2-standard-2 --provisioning-model=SPOT --instance-termination-action=STOP \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=20GB --boot-disk-type=pd-balanced --boot-disk-auto-delete \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
  --no-service-account --no-scopes \
  --network=usher-vpc --subnet=usher-sp \
  --tags=usher-prod --labels=usher=prod \
  --metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE \
  --metadata-from-file=startup-script=ops/gcp/startup.sh
# the boot disk gets neither the label nor the snapshot schedule from the line above:
gcloud compute disks add-resource-policies usher-prod --zone=southamerica-east1-b \
  --project=usher-prod --resource-policies=usher-daily
gcloud compute disks update usher-prod --zone=southamerica-east1-b \
  --project=usher-prod --update-labels=usher=prod
# then, if it was paused while no VM existed:
gcloud scheduler jobs resume usher-restart --location=southamerica-east1 --project=usher-prod
```

The boot disk was grown from 10 GB to 20 GB on 2026-10-07 (step 9: the
first release filled it). A future redo creates it at 20 GB
(`--boot-disk-size=20GB`); growing a live one is online, after the owner's
yes, and the VM side is `ops/gcp/vm-setup.md` §6:

```bash
gcloud compute disks resize usher-prod --size=20GB --zone=southamerica-east1-b --project=usher-prod
```

Read-only check afterwards (no `serviceAccounts` block, `SPOT`/`STOP`,
`diskSizeGb: '20'`, `labels: usher: prod` on both):

```bash
gcloud compute instances describe usher-prod --zone=southamerica-east1-b --project=usher-prod \
  --format='yaml(status,scheduling,labels,serviceAccounts,shieldedInstanceConfig,disks[].diskSizeGb)'
gcloud compute disks describe usher-prod --zone=southamerica-east1-b --project=usher-prod \
  --format='yaml(sizeGb,labels,resourcePolicies)'
```

## budget-cap/

`usher-budget-cap`, the Cloud Run function that stops production when a
budget reaches 100% (`docs/DEPLOY.md` §1). Tests: `tests/test_gcp_budget_cap.py`.
Redeploy from the repository root, on the host, after a merged change:

```bash
SA=usher-budget-cap@usher-prod.iam.gserviceaccount.com
gcloud functions deploy usher-budget-cap --gen2 --project=usher-prod \
  --region=southamerica-east1 --runtime=python312 --source=ops/gcp/budget-cap \
  --entry-point=budget_cap --trigger-topic=usher-budget \
  --service-account=$SA --trigger-service-account=$SA --memory=256Mi \
  --max-instances=1 --no-allow-unauthenticated --ingress-settings=internal-only \
  --update-labels=usher=prod
```

Smoke test (under budget, must log "nothing to do" and stop nothing):

```bash
gcloud pubsub topics publish usher-budget --project=usher-prod \
  --message='{"budgetDisplayName":"smoke-test","costAmount":1.0,"budgetAmount":450.0}'
gcloud logging read 'resource.labels.service_name="usher-budget-cap" AND textPayload:"smoke-test"' \
  --project=usher-prod --freshness=10m --format='value(timestamp,textPayload)'
```
