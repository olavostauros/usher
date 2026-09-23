"""The adapter contract's helpers: ``Discovered`` and ``fingerprint`` (ADR 0013)."""

import dataclasses
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from evcrawl.sources.base import Discovered, fingerprint


def test_fingerprint_is_canonical_and_key_order_independent() -> None:
    a = fingerprint({"b": 1, "a": {"y": 2, "x": [1, 2]}, "s": "Vitória"})
    b = fingerprint({"s": "Vitória", "a": {"x": [1, 2], "y": 2}, "b": 1})
    assert a == b
    assert re.fullmatch(r"[0-9a-f]{64}", a)  # fits source_listings.listing_fingerprint


def test_fingerprint_drops_volatile_keys_and_prefixes() -> None:
    base: dict[str, object] = {"id": 1, "name": "x", "score": 10, "displayDate": "Qua"}
    drop, prefixes = ("score",), ("display",)
    stable = fingerprint(base, drop=drop, drop_prefixes=prefixes)
    assert stable == fingerprint({"id": 1, "name": "x"})
    volatile = dict(base, score=11, displayDate="Qui", displayTime="20:00")
    assert fingerprint(volatile, drop=drop, drop_prefixes=prefixes) == stable
    assert fingerprint(dict(base, name="y"), drop=drop, drop_prefixes=prefixes) != stable
    assert fingerprint(base) != stable  # nothing dropped: the volatile keys count


def test_fingerprint_accepts_values_json_cannot_encode() -> None:
    item = {"when": datetime(2026, 9, 23, tzinfo=UTC), "amount": Decimal("1.50")}
    assert fingerprint(item) == fingerprint(dict(item))


def test_discovered_is_a_frozen_value() -> None:
    a = Discovered("https://x/e/1", "1", "f" * 64)
    assert a == Discovered("https://x/e/1", "1", "f" * 64)
    assert len({a, Discovered("https://x/e/1", "1", "f" * 64)}) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.url = "https://x/e/2"  # type: ignore[misc]
