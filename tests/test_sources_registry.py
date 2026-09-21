from evcrawl.sources import REGISTRY, SourceAdapter
from evcrawl.sources.blueticket import BlueTicketAdapter
from evcrawl.sources.sympla import SymplaAdapter
from evcrawl.sources.uticket import UTicketAdapter
from evcrawl.sources.zig import ZigAdapter


def test_registry_contains_the_phase_1_and_2_sources() -> None:
    assert REGISTRY["sympla"] is SymplaAdapter
    assert REGISTRY["uticket"] is UTicketAdapter
    assert REGISTRY["blueticket"] is BlueTicketAdapter
    assert REGISTRY["zig"] is ZigAdapter
    assert all(issubclass(cls, SourceAdapter) for cls in REGISTRY.values())


def test_registered_adapters_declare_name_and_rate_limit() -> None:
    for name, cls in REGISTRY.items():
        assert cls.name == name
        assert cls.rate_limit.requests_per_second <= 1.0
        assert cls.rate_limit.max_retries <= 5


def test_extra_headers_never_carry_a_user_agent() -> None:
    # AGENTS.md §10: the project User-Agent is never overridden per source.
    for cls in REGISTRY.values():
        assert not {k.lower() for k in cls.extra_headers} & {"user-agent"}
    assert UTicketAdapter.extra_headers == {"Referer": "https://uticket.com.br/"}  # ADR 0006
