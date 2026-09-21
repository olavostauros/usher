"""Registry of source adapters. Register new adapters here (AGENTS.md §6 step 7)."""

from evcrawl.sources.base import EventRecord, RateLimit, SourceAdapter
from evcrawl.sources.sympla import SymplaAdapter
from evcrawl.sources.uticket import UTicketAdapter

REGISTRY: dict[str, type[SourceAdapter]] = {
    SymplaAdapter.name: SymplaAdapter,
    UTicketAdapter.name: UTicketAdapter,
}

__all__ = ["REGISTRY", "EventRecord", "RateLimit", "SourceAdapter"]
