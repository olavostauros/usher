from evcrawl.sources import REGISTRY, SourceAdapter
from evcrawl.sources.sympla import SymplaAdapter


def test_registry_contains_sympla() -> None:
    assert REGISTRY["sympla"] is SymplaAdapter
    assert all(issubclass(cls, SourceAdapter) for cls in REGISTRY.values())


def test_registered_adapters_declare_name_and_rate_limit() -> None:
    for name, cls in REGISTRY.items():
        assert cls.name == name
        assert cls.rate_limit.requests_per_second <= 1.0
        assert cls.rate_limit.max_retries <= 5
