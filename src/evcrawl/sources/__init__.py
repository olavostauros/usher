"""Registry of source adapters. Register new adapters here (AGENTS.md §6 step 7)."""

from evcrawl.sources.base import Discovered, EventRecord, RateLimit, SourceAdapter, fingerprint
from evcrawl.sources.bilheteriadigital import BilheteriaDigitalAdapter
from evcrawl.sources.blueticket import BlueTicketAdapter
from evcrawl.sources.ingresse import IngresseAdapter
from evcrawl.sources.sympla import SymplaAdapter
from evcrawl.sources.ticketbolt import TicketBoltAdapter
from evcrawl.sources.uticket import UTicketAdapter
from evcrawl.sources.zig import ZigAdapter

REGISTRY: dict[str, type[SourceAdapter]] = {
    SymplaAdapter.name: SymplaAdapter,
    UTicketAdapter.name: UTicketAdapter,
    BlueTicketAdapter.name: BlueTicketAdapter,
    TicketBoltAdapter.name: TicketBoltAdapter,
    ZigAdapter.name: ZigAdapter,
    IngresseAdapter.name: IngresseAdapter,
    BilheteriaDigitalAdapter.name: BilheteriaDigitalAdapter,
}

__all__ = ["REGISTRY", "Discovered", "EventRecord", "RateLimit", "SourceAdapter", "fingerprint"]
