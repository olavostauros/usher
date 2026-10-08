"""City registry: the cities the pipeline crawls and their zones (docs/SPECIFICATION.md §4).

One row per city, Greater Vitória first and then the 26 other state capitals by
UF. Each row carries the source-specific key every adapter uses to scope its
listing (``keys``); a source without a key for a city has no listing there.
Every row is ``enabled`` since spec task 8 (2026-09-23) turned the capitals on;
``enabled = False`` is how a city is paused. A disabled city still resolves for
``--city``, it is just not part of ``discover(None)`` or of the scheduler's
standing jobs.
"""

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_TIMEZONE = "America/Sao_Paulo"

# UF -> IANA zone (spec §2, "Capitals and zones"). Fernando de Noronha
# (``America/Noronha``) is not a UF; Zig's per-event zone handles its events.
UF_TIMEZONE: dict[str, str] = {
    "AC": "America/Rio_Branco",
    "AL": "America/Maceio",
    "AM": "America/Manaus",
    "AP": "America/Belem",
    "BA": "America/Bahia",
    "CE": "America/Fortaleza",
    "DF": DEFAULT_TIMEZONE,
    "ES": DEFAULT_TIMEZONE,
    "GO": DEFAULT_TIMEZONE,
    "MA": "America/Fortaleza",
    "MG": DEFAULT_TIMEZONE,
    "MS": "America/Campo_Grande",
    "MT": "America/Cuiaba",
    "PA": "America/Belem",
    "PB": "America/Fortaleza",
    "PE": "America/Recife",
    "PI": "America/Fortaleza",
    "PR": DEFAULT_TIMEZONE,
    "RJ": DEFAULT_TIMEZONE,
    "RN": "America/Fortaleza",
    "RO": "America/Porto_Velho",
    "RR": "America/Boa_Vista",
    "RS": DEFAULT_TIMEZONE,
    "SC": DEFAULT_TIMEZONE,
    "SE": "America/Maceio",
    "SP": DEFAULT_TIMEZONE,
    "TO": "America/Araguaina",
}


def fold(text: str) -> str:
    """Lowercase, accent-insensitive comparison key (``"São Luís"`` -> ``"sao luis"``)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().strip()


def timezone_for_uf(uf: str | None) -> str:
    """The zone of a UF; the Brasília zone when the UF is missing or unknown."""
    if not uf:
        return DEFAULT_TIMEZONE
    return UF_TIMEZONE.get(uf.strip().upper(), DEFAULT_TIMEZONE)


@dataclass(frozen=True)
class City:
    """One registry row; ``keys`` maps a source name to that source's key for the city."""

    slug: str
    name: str
    uf: str
    ibge_code: int
    timezone: str
    enabled: bool = True
    keys: Mapping[str, str] = field(default_factory=dict, hash=False, compare=False)

    def key(self, source: str) -> str | None:
        """The source's own key for this city, or ``None`` when it has no listing here."""
        return self.keys.get(source)


def _city(
    slug: str, name: str, uf: str, ibge_code: int, *, enabled: bool = True, **keys: str
) -> City:
    # The recon of 2026-09-22 (spec §2, "Registry seed"): Sympla's slug is
    # ``<unaccented-name>-<uf>``; the other four sources take the display name,
    # matched accent- and case-folded by the adapters.
    sympla = keys.pop("sympla", fold(name).replace(" ", "-") + "-" + uf.lower())
    return City(
        slug=slug,
        name=name,
        uf=uf,
        ibge_code=ibge_code,
        timezone=UF_TIMEZONE[uf],
        enabled=enabled,
        keys={
            "sympla": sympla,
            "uticket": keys.pop("uticket", name),
            "blueticket": keys.pop("blueticket", name),
            "ticketbolt": keys.pop("ticketbolt", name),
            "zig": keys.pop("zig", name),
        },
    )


GREATER_VITORIA: tuple[City, ...] = (
    _city("vitoria", "Vitória", "ES", 3205309),
    _city("vila-velha", "Vila Velha", "ES", 3205200),
    _city("serra", "Serra", "ES", 3205002),
    _city("cariacica", "Cariacica", "ES", 3201308),
    _city("viana", "Viana", "ES", 3205101),
    _city("guarapari", "Guarapari", "ES", 3202405),
)

# The 26 other capitals, by UF; enabled since spec task 8 (conditional fetch landed first).
CAPITALS: tuple[City, ...] = (
    _city("rio-branco", "Rio Branco", "AC", 1200401),
    _city("maceio", "Maceió", "AL", 2704302),
    _city("manaus", "Manaus", "AM", 1302603),
    _city("macapa", "Macapá", "AP", 1600303),
    _city("salvador", "Salvador", "BA", 2927408),
    _city("fortaleza", "Fortaleza", "CE", 2304400),
    _city("brasilia", "Brasília", "DF", 5300108),
    _city("goiania", "Goiânia", "GO", 5208707),
    _city("sao-luis", "São Luís", "MA", 2111300),
    _city("belo-horizonte", "Belo Horizonte", "MG", 3106200),
    _city("campo-grande", "Campo Grande", "MS", 5002704),
    _city("cuiaba", "Cuiabá", "MT", 5103403),
    _city("belem", "Belém", "PA", 1501402),
    _city("joao-pessoa", "João Pessoa", "PB", 2507507),
    _city("recife", "Recife", "PE", 2611606),
    _city("teresina", "Teresina", "PI", 2211001),
    _city("curitiba", "Curitiba", "PR", 4106902),
    _city("rio-de-janeiro", "Rio de Janeiro", "RJ", 3304557),
    _city("natal", "Natal", "RN", 2408102),
    _city("porto-velho", "Porto Velho", "RO", 1100205),
    _city("boa-vista", "Boa Vista", "RR", 1400100),
    _city("porto-alegre", "Porto Alegre", "RS", 4314902),
    _city("florianopolis", "Florianópolis", "SC", 4205407),
    _city("aracaju", "Aracaju", "SE", 2800308),
    _city("sao-paulo", "São Paulo", "SP", 3550308),
    _city("palmas", "Palmas", "TO", 1721000),
)

CITIES: tuple[City, ...] = GREATER_VITORIA + CAPITALS

_BY_SLUG: dict[str, City] = {c.slug: c for c in CITIES}
# Every spelling ``resolve()`` accepts, folded: slug, ``<slug>-<uf>`` (the Phase 1/2
# ``--city`` forms such as ``vitoria-es``), the display name, and its dashed form.
_ALIASES: dict[str, City] = {}
for _c in CITIES:
    _name = fold(_c.name)
    for _alias in (_c.slug, f"{_c.slug}-{_c.uf.lower()}", _name, _name.replace(" ", "-")):
        _ALIASES.setdefault(_alias, _c)


def slugs() -> list[str]:
    """Every registry slug, in registry order."""
    return [c.slug for c in CITIES]


def by_slug(slug: str) -> City:
    """The city with exactly this slug."""
    try:
        return _BY_SLUG[slug]
    except KeyError:
        raise ValueError(f"unknown city {slug!r}; known: {', '.join(slugs())}") from None


def resolve(text: str) -> City:
    """The city ``--city`` names: slug, old ``-es`` form or display name, any case or accents."""
    key = fold(text).replace("_", "-")
    city = _ALIASES.get(key) or _ALIASES.get(key.replace(" ", "-"))
    if city is None:
        raise ValueError(f"unknown city {text!r}; known: {', '.join(slugs())}")
    return city


def for_source(source: str, *, enabled_only: bool = True) -> list[City]:
    """The cities a source lists, in registry order; enabled ones only by default."""
    return [c for c in CITIES if c.key(source) is not None and (c.enabled or not enabled_only)]


__all__ = [
    "CAPITALS",
    "CITIES",
    "DEFAULT_TIMEZONE",
    "GREATER_VITORIA",
    "UF_TIMEZONE",
    "City",
    "by_slug",
    "fold",
    "for_source",
    "resolve",
    "slugs",
    "timezone_for_uf",
]
