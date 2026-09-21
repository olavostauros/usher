"""Registry of source adapters. Register new adapters here (AGENTS.md §6 step 7)."""

from evcrawl.sources.base import EventRecord, RateLimit, SourceAdapter
from evcrawl.sources.blueticket import BlueTicketAdapter
from evcrawl.sources.sympla import SymplaAdapter

REGISTRY: dict[str, type[SourceAdapter]] = {
    SymplaAdapter.name: SymplaAdapter,
    BlueTicketAdapter.name: BlueTicketAdapter,
}

__all__ = ["REGISTRY", "EventRecord", "RateLimit", "SourceAdapter"]
