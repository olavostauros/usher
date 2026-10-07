# ops/gcp — production on Google Cloud

The runbook is `DEPLOY.md` at the repository root; this directory holds what
it deploys. Everything lives in project `usher-prod`, region
`southamerica-east1`, labelled `usher=prod`.

## budget-cap/

`usher-budget-cap`, the Cloud Run function that stops production when a
budget reaches 100% (`DEPLOY.md` §1). Tests: `tests/test_gcp_budget_cap.py`.
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
