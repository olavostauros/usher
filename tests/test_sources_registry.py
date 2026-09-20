import pytest

from evcrawl.sources import REGISTRY, SourceAdapter
from evcrawl.sources.sympla import SymplaAdapter


def test_registry_contains_sympla() -> None:
    assert REGISTRY["sympla"] is SymplaAdapter
    assert all(issubclass(cls, SourceAdapter) for cls in REGISTRY.values())


async def test_sympla_fetch_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        await SymplaAdapter().fetch("x")
