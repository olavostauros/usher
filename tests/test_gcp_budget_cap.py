"""ops/gcp/budget-cap/main.py stops production at 100% of a budget (docs/DEPLOY.md §1, step 3).

The decision and the API calls run against a fake `api`; nothing reaches Google.
"""

import base64
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

MAIN_PY = Path(__file__).resolve().parents[1] / "ops" / "gcp" / "budget-cap" / "main.py"


def load_main() -> ModuleType:
    spec = importlib.util.spec_from_file_location("budget_cap_main", MAIN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["budget_cap_main"] = module
    spec.loader.exec_module(module)
    return module


main = load_main()


class FakeApi:
    def __init__(self, listing: dict[str, Any], pause_status: int | None = None) -> None:
        self.listing = listing
        self.pause_status = pause_status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, token: str) -> dict[str, Any]:
        self.calls.append((method, url))
        if url.endswith(":pause") and self.pause_status:
            raise urllib.error.HTTPError(url, self.pause_status, "x", None, None)  # type: ignore[arg-type]
        if method == "GET":
            return self.listing
        return {}


def message(cost: float, budget: float) -> str:
    body = {"budgetDisplayName": "usher-monthly", "costAmount": cost, "budgetAmount": budget}
    return base64.b64encode(json.dumps(body).encode()).decode()


@pytest.mark.parametrize(
    ("cost", "budget", "expected"),
    [(0, 450, False), (449.99, 450, False), (450, 450, True), (612.3, 450, True), (5, 0, False)],
)
def test_over_budget_only_at_or_above_the_amount(
    cost: float, budget: float, expected: bool
) -> None:
    assert main.over_budget(main.decode(message(cost, budget))) is expected


def test_stop_instances_stops_running_labelled_vms_only() -> None:
    api = FakeApi(
        {
            "items": {
                "zones/southamerica-east1-b": {
                    "instances": [
                        {"name": "usher-prod", "status": "RUNNING"},
                        {"name": "old", "status": "TERMINATED"},
                    ]
                },
                "zones/southamerica-east1-a": {"warning": {"code": "NO_RESULTS_ON_PAGE"}},
            }
        }
    )
    assert main.stop_instances("t", api) == ["southamerica-east1-b/usher-prod"]
    method, url = api.calls[0]
    assert method == "GET" and "filter=labels.usher%3Dprod" in url
    assert api.calls[1] == (
        "POST",
        f"{main.COMPUTE}/projects/usher-prod/zones/southamerica-east1-b/instances/usher-prod/stop",
    )
    assert len(api.calls) == 2


def test_pause_tolerates_a_missing_job_but_not_other_errors() -> None:
    main.pause_restart_job("t", FakeApi({}, pause_status=404))
    with pytest.raises(urllib.error.HTTPError):
        main.pause_restart_job("t", FakeApi({}, pause_status=403))


def test_under_budget_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_token() -> str:
        raise AssertionError("must not fetch a token under budget")

    monkeypatch.setattr(main, "_token", no_token)
    main.budget_cap(SimpleNamespace(data={"message": {"data": message(10, 450)}}))
