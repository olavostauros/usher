"""usher-budget-cap: stop production when a budget reaches 100% (docs/DEPLOY.md §1, step 3).

A Cloud Run function on the Pub/Sub topic `usher-budget`, where both budgets
(`usher-monthly`, `usher-credit`) publish their notifications, several times a
day. When the actual cost in a notification reaches its budget, it pauses the
Cloud Scheduler job `usher-restart` (otherwise that job would start the Spot VM
again within 5 minutes) and then stops every instance labelled `usher=prod`.
Stopping keeps the disks; the owner resumes the job and starts the VM after
looking. Forecasts alone never stop anything.

Standard library only: the access token comes from the metadata server of the
function's own service account (`usher-budget-cap`, docs/DEPLOY.md §5).
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

try:
    from functions_framework import cloud_event  # type: ignore[import-not-found,unused-ignore]
except ImportError:  # tests and the dev image do not ship the framework

    def cloud_event(func: Callable[[Any], None]) -> Callable[[Any], None]:
        return func


PROJECT = os.environ.get("USHER_PROJECT", "usher-prod")
REGION = os.environ.get("USHER_REGION", "southamerica-east1")
RESTART_JOB = f"projects/{PROJECT}/locations/{REGION}/jobs/usher-restart"
LABEL_FILTER = "labels.usher=prod"
TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
COMPUTE = "https://compute.googleapis.com/compute/v1"
SCHEDULER = "https://cloudscheduler.googleapis.com/v1"


def log(message: str) -> None:
    """One line on stdout: Cloud Run sends it to Cloud Logging whatever the framework configured."""
    print(f"usher-budget-cap: {message}", flush=True)


def decode(message_data: str) -> dict[str, Any]:
    """The budget notification JSON inside a Pub/Sub message's base64 `data`."""
    payload: dict[str, Any] = json.loads(base64.b64decode(message_data))
    return payload


def over_budget(notification: dict[str, Any]) -> bool:
    """True when the actual cost has reached the budget amount."""
    cost = float(notification.get("costAmount", 0))
    budget = float(notification.get("budgetAmount", 0))
    return budget > 0 and cost >= budget


def _token() -> str:
    request = urllib.request.Request(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=10) as response:
        token: str = json.load(response)["access_token"]
    return token


def _api(method: str, url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
        data=b"" if method == "POST" else None,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    result: dict[str, Any] = json.loads(body) if body else {}
    return result


def pause_restart_job(token: str, api: Callable[[str, str, str], dict[str, Any]] = _api) -> None:
    try:
        api("POST", f"{SCHEDULER}/{RESTART_JOB}:pause", token)
        log(f"paused {RESTART_JOB}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        log(f"no {RESTART_JOB} to pause")


def stop_instances(token: str, api: Callable[[str, str, str], dict[str, Any]] = _api) -> list[str]:
    """Stop every running `usher=prod` instance; returns `zone/name` of each one stopped."""
    query = urllib.parse.urlencode({"filter": LABEL_FILTER})
    listing = api("GET", f"{COMPUTE}/projects/{PROJECT}/aggregated/instances?{query}", token)
    stopped = []
    for scope, entry in listing.get("items", {}).items():
        zone = scope.removeprefix("zones/")
        for instance in entry.get("instances", []):
            if instance.get("status") in ("TERMINATED", "STOPPING", "SUSPENDED"):
                continue
            name = instance["name"]
            api("POST", f"{COMPUTE}/projects/{PROJECT}/zones/{zone}/instances/{name}/stop", token)
            stopped.append(f"{zone}/{name}")
    return stopped


@cloud_event  # type: ignore[untyped-decorator,unused-ignore]
def budget_cap(event: Any) -> None:
    notification = decode(event.data["message"]["data"])
    name = notification.get("budgetDisplayName", "?")
    cost, budget = notification.get("costAmount"), notification.get("budgetAmount")
    if not over_budget(notification):
        log(f"budget {name}: {cost} of {budget}, under budget, nothing to do")
        return
    log(f"budget {name}: {cost} of {budget} reached, stopping usher=prod")
    token = _token()
    pause_restart_job(token)
    stopped = stop_instances(token)
    log(f"budget {name}: stopped {', '.join(stopped) or 'nothing (none running)'}")
