from evcrawl.sources import REGISTRY, SourceAdapter
from evcrawl.sources.bilheteriadigital import BilheteriaDigitalAdapter
from evcrawl.sources.blueticket import BlueTicketAdapter
from evcrawl.sources.ingresse import IngresseAdapter
from evcrawl.sources.sympla import SymplaAdapter
from evcrawl.sources.ticketbolt import TicketBoltAdapter
from evcrawl.sources.uticket import UTicketAdapter
from evcrawl.sources.zig import ZigAdapter


def test_registry_contains_the_phase_1_and_2_sources() -> None:
    assert REGISTRY["sympla"] is SymplaAdapter
    assert REGISTRY["uticket"] is UTicketAdapter
    assert REGISTRY["blueticket"] is BlueTicketAdapter
    assert REGISTRY["ticketbolt"] is TicketBoltAdapter
    assert REGISTRY["zig"] is ZigAdapter
    assert REGISTRY["ingresse"] is IngresseAdapter
    assert REGISTRY["bilheteriadigital"] is BilheteriaDigitalAdapter
    assert all(issubclass(cls, SourceAdapter) for cls in REGISTRY.values())


def test_registered_adapters_declare_name_and_rate_limit() -> None:
    for name, cls in REGISTRY.items():
        assert cls.name == name
        assert cls.rate_limit.requests_per_second <= 1.0
        assert cls.rate_limit.max_retries <= 5


def test_extra_headers_are_per_host_and_never_carry_a_user_agent() -> None:
    # AGENTS.md §10: the project User-Agent is never overridden per source.
    for cls in REGISTRY.values():
        for host, headers in cls.extra_headers.items():
            assert host == host.lower() and "/" not in host and ":" not in host  # a bare hostname
            assert not {k.lower() for k in headers} & {"user-agent"}
    # The two owner-approved headers (ADR 0006 and ADR 0008 addenda), each on one host.
    assert UTicketAdapter.extra_headers == {
        "api.uticket.com.br": {"Referer": "https://uticket.com.br/"}
    }
    assert TicketBoltAdapter.extra_headers == {"www.ticketbolt.com": {"RSC": "1"}}
    for cls in (
        SymplaAdapter,
        BlueTicketAdapter,
        ZigAdapter,
        IngresseAdapter,
        BilheteriaDigitalAdapter,
    ):
        assert cls.extra_headers == {}
