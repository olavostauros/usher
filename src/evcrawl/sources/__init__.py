"""Registry of source adapters. Register new adapters here (AGENTS.md §6 step 7)."""

from evcrawl.sources.base import EventRecord, RateLimit, SourceAdapter
from evcrawl.sources.blueticket import BlueTicketAdapter
from evcrawl.sources.sympla import SymplaAdapter
from evcrawl.sources.uticket import UTicketAdapter
from evcrawl.sources.zig import ZigAdapter

REGISTRY: dict[str, type[SourceAdapter]] = {
    SymplaAdapter.name: SymplaAdapter,
    UTicketAdapter.name: UTicketAdapter,
    BlueTicketAdapter.name: BlueTicketAdapter,
    ZigAdapter.name: ZigAdapter,
}

__all__ = ["REGISTRY", "EventRecord", "RateLimit", "SourceAdapter"]
