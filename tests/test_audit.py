"""ops/audit/audit.py never copies a secret value into a finding's evidence.

The VM's ``usher-audit.service`` writes its report where the VM agent reads
it, and host reports are committed, so evidence that quotes a compose line or
a ``git log -p`` hit must carry the variable name and not its value.
"""

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

AUDIT_PY = Path(__file__).resolve().parents[1] / "ops" / "audit" / "audit.py"


def load_audit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit", AUDIT_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit"] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


audit = load_audit()


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("POSTGRES_PASSWORD: hunter2secret", "POSTGRES_PASSWORD: ***"),
        ('+  api_key = "s3cr3tvalue1"', '+  api_key = "***"'),
        ("+GRAFANA_ADMIN_PASSWORD=abcdefgh12", "+GRAFANA_ADMIN_PASSWORD=***"),
        ("+token: 'ghp_abcdefgh'", "+token: '***'"),
        ("WEBHOOK_SECRET=x y TEAM_DB_PASSWORD=z", "WEBHOOK_SECRET=*** y TEAM_DB_PASSWORD=***"),
    ],
)
def test_mask_secrets_hides_values(raw: str, masked: str) -> None:
    assert audit.mask_secrets(raw) == masked


@pytest.mark.parametrize(
    "text",
    [
        "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in .env}",
        "TEAM_DB_PASSWORD=$TEAM_DB_PASSWORD",
        "Config.Env names: ['TEAM_DB_PASSWORD', 'REDIS_URL']",
        "sshd -T: passwordauthentication no",
    ],
)
def test_mask_secrets_keeps_references_and_names(text: str) -> None:
    assert audit.mask_secrets(text) == text


def test_add_masks_evidence() -> None:
    args = argparse.Namespace(container=None, inside=True, repo=".", member=None)
    a = audit.Audit(args)
    f = a.add(
        severity="HIGH",
        title="t",
        category="secrets",
        component="git history",
        evidence="+POSTGRES_PASSWORD=supersecret99\n+REDIS_TOKEN: abcdefghij",
        impact="",
        fix="",
        verify="",
    )
    assert "supersecret99" not in f.evidence
    assert "abcdefghij" not in f.evidence
    assert f.evidence == "+POSTGRES_PASSWORD=***\n+REDIS_TOKEN: ***"
