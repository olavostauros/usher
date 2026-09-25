"""``/v1/events`` and ``/v1/events/{id}`` (Phase 4 task 3).

Offline: the parameter rules, the cursor, the recurring-session rule, the
rate limiter and the response shape, on a fake ``Reader``. Against the
scratch database, as ``api_ro``: a seeded catalogue around Vitória and São
Paulo (online, free, paid, ended, cancelled, removed, under review, a venue
without a location, a recurring event with several sessions) and the radius
boundary, the ordering, the cursor round trip, 404/410, decimal prices, and
``EXPLAIN`` of the three query shapes without a sequential scan.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, RowMapping, text
from sqlalchemy.orm import Session

from evcrawl.api.app import create_app
from evcrawl.api.db import ApiDatabase
from evcrawl.api.events import (
    SERIES_PREFIX,
    Cursor,
    InvalidSearch,
    Search,
    list_sql,
)
from evcrawl.api.ratelimit import RateLimiter
from evcrawl.config import Settings
from evcrawl.db.models import (
    Category,
    Event,
    EventStatus,
    Organizer,
    PriceLot,
    Source,
    SourceListing,
    Venue,
)
from evcrawl.pipeline.normalize import normalize_key
from tests.test_api_app import api_ro_db  # noqa: F401  (a fixture)

VITORIA = (-20.3155, -40.3128)
SAO_PAULO = (-23.5505, -46.6333)
# One degree of latitude is about 110.7 km here: 9.5 km inside a 10 km radius, 10.5 km outside.
INSIDE = (VITORIA[0] + 9.5 / 110.7, VITORIA[1])
OUTSIDE = (VITORIA[0] + 10.5 / 110.7, VITORIA[1])


# --- offline


def build(**kwargs: Any) -> Search:
    params: dict[str, Any] = dict(
        lat=None,
        lon=None,
        radius_km=None,
        city=None,
        window_from=None,
        window_to=None,
        categories=None,
        free=None,
        online=None,
        q=None,
        sort=None,
        limit=20,
        cursor=None,
        now=datetime(2026, 10, 1, 12, tzinfo=UTC),
    )
    params.update(kwargs)
    return Search.build(**params)


def test_a_search_needs_a_point_or_a_city_and_takes_the_spec_defaults() -> None:
    near = build(lat=VITORIA[0], lon=VITORIA[1])
    assert (near.radius_km, near.sort, near.online) == (25.0, "distance", "exclude")
    assert near.window_from == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert near.window_to == datetime(2026, 10, 31, 12, tzinfo=UTC)
    city = build(city="vitoria")
    assert (city.sort, city.online, city.city) == ("starts_at", "include", "vitoria")


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({}, "city"),
        ({"lat": -20.0}, "lon"),
        ({"lon": -40.0}, "lat"),
        ({"city": "vitoria", "radius_km": 5}, "radius_km"),
        ({"city": "vitoria", "sort": "distance"}, "sort"),
        ({"city": "atlantis"}, "city"),
        ({"city": "vitoria", "window_from": "2026-10-10", "window_to": "2026-10-09"}, "to"),
        ({"city": "vitoria", "window_from": "2026-10-01", "window_to": "2027-04-01"}, "to"),
        ({"city": "vitoria", "window_from": "yesterday"}, "from"),
        ({"city": "vitoria", "q": "!!!"}, "q"),
        ({"city": "vitoria", "categories": ["mu sic"]}, "category"),
        ({"city": "vitoria", "categories": [f"c{i}" for i in range(11)]}, "category"),
        ({"city": "vitoria", "cursor": "not-a-cursor"}, "cursor"),
    ],
)
def test_invalid_searches_name_the_parameter(kwargs: dict[str, Any], field: str) -> None:
    with pytest.raises(InvalidSearch) as exc:
        build(**kwargs)
    assert exc.value.field == field


def test_window_dates_are_whole_utc_days_and_datetimes_are_instants() -> None:
    s = build(city="vitoria", window_from="2026-10-03", window_to="2026-10-03")
    assert s.window_from == datetime(2026, 10, 3, tzinfo=UTC)
    assert s.window_to == datetime(2026, 10, 4, tzinfo=UTC)  # the whole day
    s = build(
        city="vitoria", window_from="2026-10-03T20:00:00-03:00", window_to="2026-10-04T00:00Z"
    )
    assert s.window_from == datetime(2026, 10, 3, 23, tzinfo=UTC)
    assert s.window_to == datetime(2026, 10, 4, tzinfo=UTC)
    assert build(city="vitoria", window_to="2026-10-05").window_from == datetime(
        2026, 10, 1, 12, tzinfo=UTC
    )


def test_query_words_are_normalized_like_titles_and_capped() -> None:
    s = build(city="vitoria", q="  Jázz & BLUES  ")
    assert s.words == ("jazz", "blues")
    assert len(build(city="vitoria", q="a b c d e f g h").words) == 6
    sql, params = list_sql(build(city="vitoria", q="50% off_"))
    assert (params["w0"], params["w1"]) == ("%50%", "%off%")  # no wildcard survives the key
    assert "title_normalized like :w0" in sql and "title_normalized like :w1" in sql


def test_the_sql_filters_by_point_or_city_and_collapses_sessions() -> None:
    sql, params = list_sql(build(lat=VITORIA[0], lon=VITORIA[1], radius_km=10))
    assert (
        "st_dwithin(location, st_setsrid(st_makepoint(:lon, :lat), 4326)::geography, :radius_m)"
        in sql
    )
    assert params["radius_m"] == 10_000 and "not is_online" in sql
    assert "order by distance_m nulls last, id" in sql
    assert "regexp_replace(title_normalized, :series_re, '')" in sql
    assert "partition by case when venue_id is null then id else 0 end, venue_id, series_key" in sql
    assert params["limit"] == 21

    sql, params = list_sql(build(city="sao-paulo", categories=["music", "Music"], free=True))
    assert ":city = any(cities)" in sql and params["city"] == "sao-paulo"
    assert "st_dwithin" not in sql and "null::bigint as distance_m" in sql
    assert params["categories"] == ["music"] and "category = any(:categories)" in sql
    assert " and is_free\n" in sql or " and is_free " in sql
    assert "order by starts_at, id" in sql

    sql, _ = list_sql(build(lat=VITORIA[0], lon=VITORIA[1], online="include"))
    assert "or is_online)" in sql
    sql, _ = list_sql(build(city="vitoria", online="only", free=False))
    assert "is_online" in sql and "is_free is distinct from true" in sql


def test_the_cursor_round_trips_and_rejects_the_other_sort() -> None:
    by_distance = Cursor("distance", 48213, distance_m=2614)
    assert Cursor.decode(by_distance.encode(), "distance") == by_distance
    past_located = Cursor("distance", 7, distance_m=None)
    assert Cursor.decode(past_located.encode(), "distance") == past_located
    when = datetime(2026, 10, 3, 23, tzinfo=UTC)
    by_time = Cursor("starts_at", 48213, starts_at=when)
    assert Cursor.decode(by_time.encode(), "starts_at") == by_time
    with pytest.raises(InvalidSearch):
        Cursor.decode(by_time.encode(), "distance")
    sql, params = list_sql(build(lat=VITORIA[0], lon=VITORIA[1], cursor=by_distance.encode()))
    assert "(distance_m > :after_d or (distance_m = :after_d and id > :after_id)" in sql
    assert (params["after_d"], params["after_id"]) == (2614, 48213)
    sql, params = list_sql(build(city="vitoria", cursor=by_time.encode()))
    assert "(starts_at, id) > (:after_s, :after_id)" in sql and params["after_s"] == when


@pytest.mark.parametrize(
    ("title", "key"),
    [
        (
            "17h (horário local) | VISITA GUIADA - Museu da Estrada",
            "visita guiada museu da estrada",
        ),
        ("09h30 | Visita", "visita"),
        ("17:00 - Visita", "visita"),
        ("19hs Show da Banda", "show da banda"),
        ("2 Anos de Samba", "2 anos de samba"),
        ("10 Bandas, 1 Palco", "10 bandas 1 palco"),
        ("Visita às 17h", "visita as 17h"),
    ],
)
def test_the_series_key_drops_a_leading_time_of_day_only(title: str, key: str) -> None:
    assert re.sub(SERIES_PREFIX, "", normalize_key(title)) == key


def test_the_rate_limiter_is_a_token_bucket_per_client() -> None:
    now = [0.0]
    limiter = RateLimiter(per_minute=60, clock=lambda: now[0])
    assert all(limiter.take("a") == 0 for _ in range(60))
    wait = limiter.take("a")
    assert 0 < wait <= 1
    assert limiter.take("b") == 0  # another client, another bucket
    now[0] += 2  # two seconds refill two tokens
    assert limiter.take("a") == 0 and limiter.take("a") == 0 and limiter.take("a") > 0


class FakeReader:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows_ = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[RowMapping]:
        self.calls.append((sql, dict(params or {})))
        if "api_event_status" in sql:
            return []
        return self.rows_  # type: ignore[return-value]


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 48213,
        "title": "Festival de Jazz",
        "starts_at": datetime(2026, 10, 3, 23, tzinfo=UTC),
        "ends_at": None,
        "timezone": "America/Sao_Paulo",
        "is_online": False,
        "image_url": None,
        "category": "music",
        "category_name": "Música",
        "venue_id": 912,
        "venue_name": "Teatro",
        "venue_address": "Rua 1",
        "venue_city": "Vitória",
        "venue_uf": "ES",
        "lat": -20.31981234567,
        "lon": -40.33810412345,
        "price_min": Decimal("0.00"),
        "price_max": Decimal("80.00"),
        "currency": "BRL",
        "is_free": True,
        "sources": [{"source": "sympla", "url": "https://sympla.example.org/e/1"}],
        "updated_at": datetime(2026, 9, 24, tzinfo=UTC),
        "distance_m": 2614,
        "sessions": 1,
        "session_rank": 1,
    }
    row.update(over)
    return row


def client(reader: Any, **kwargs: Any) -> TestClient:
    return TestClient(create_app(reader, Settings(), **kwargs))


def test_list_items_are_the_documented_shape_with_utc_times_and_decimal_strings() -> None:
    reader = FakeReader([_row(), _row(id=48214, distance_m=None, is_online=True, venue_id=None)])
    response = client(reader).get("/v1/events", params={"lat": VITORIA[0], "lon": VITORIA[1]})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["next_cursor"] is None
    first, second = body["data"]
    assert first == {
        "id": 48213,
        "title": "Festival de Jazz",
        "starts_at": "2026-10-03T23:00:00Z",
        "ends_at": None,
        "timezone": "America/Sao_Paulo",
        "is_online": False,
        "image_url": None,
        "category": {"slug": "music", "name": "Música"},
        "venue": {
            "id": 912,
            "name": "Teatro",
            "address": "Rua 1",
            "city": "Vitória",
            "uf": "ES",
            "lat": -20.319812,
            "lon": -40.338104,
        },
        "distance_m": 2614,
        "price": {"min": "0.00", "max": "80.00", "currency": "BRL", "is_free": True},
        "sources": [{"source": "sympla", "url": "https://sympla.example.org/e/1"}],
        "sessions": 1,
    }
    assert second["venue"] is None and second["distance_m"] is None
    assert "description" not in first and "price_lots" not in first


def test_next_cursor_appears_only_when_a_further_row_exists() -> None:
    rows = [_row(id=i, distance_m=100 * i) for i in range(1, 4)]
    c = client(FakeReader(rows))
    body = c.get("/v1/events", params={"lat": 1, "lon": 2, "limit": 2}).json()
    assert [e["id"] for e in body["data"]] == [1, 2]
    assert Cursor.decode(body["next_cursor"], "distance") == Cursor("distance", 2, distance_m=200)
    body = c.get("/v1/events", params={"lat": 1, "lon": 2, "limit": 3}).json()
    assert len(body["data"]) == 3 and body["next_cursor"] is None


def test_parameter_errors_are_422_in_the_envelope_with_the_field() -> None:
    c = client(FakeReader([]))
    for params, field in [
        ({}, "city"),
        ({"lat": 1}, "lon"),
        ({"lat": 91, "lon": 0}, "lat"),
        ({"lat": 1, "lon": 2, "radius_km": 101}, "radius_km"),
        ({"city": "vitoria", "limit": 0}, "limit"),
        ({"city": "vitoria", "online": "maybe"}, "online"),
        ({"city": "vitoria", "cursor": "zzz"}, "cursor"),
        ({"city": "nowhere"}, "city"),
    ]:
        response = c.get("/v1/events", params=params)
        assert response.status_code == 422, params
        assert response.json()["error"]["code"] == "invalid_parameter"
        assert response.json()["error"]["field"] == field, params
    assert c.get("/v1/events/abc").status_code == 422


def test_detail_is_404_when_never_seen_and_410_with_the_status_otherwise() -> None:
    class Status(FakeReader):
        async def rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[RowMapping]:
            if "api_event_status" in sql:
                return [{"status": "cancelled"}]  # type: ignore[list-item]
            return []

    assert client(FakeReader([])).get("/v1/events/5").status_code == 404
    gone = client(Status([])).get("/v1/events/5")
    assert gone.status_code == 410
    assert gone.json() == {
        "error": {"code": "gone", "message": "the event is no longer active", "status": "cancelled"}
    }


def test_v1_is_rate_limited_per_client_and_healthz_is_not() -> None:
    c = client(FakeReader([]), limiter=RateLimiter(per_minute=2))
    assert c.get("/v1/cities").status_code == 200
    assert c.get("/v1/cities").status_code == 200
    limited = c.get("/v1/cities")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1
    assert c.get("/healthz").status_code == 200
    # Without EVCRAWL_API_TRUST_PROXY the header does not make a new client.
    assert c.get("/v1/cities", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429
    trusting = TestClient(
        create_app(
            FakeReader([]), Settings(evcrawl_api_trust_proxy=True), RateLimiter(per_minute=1)
        )
    )
    assert trusting.get("/v1/cities", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert trusting.get("/v1/cities", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    assert trusting.get("/v1/cities", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    # The client is the address the proxy appended (last), never one the client wrote (first).
    forged = {"X-Forwarded-For": "3.3.3.3, 1.1.1.1"}
    assert trusting.get("/v1/cities", headers=forged).status_code == 429
    assert (
        trusting.get("/v1/cities", headers={"X-Forwarded-For": "1.1.1.1, 4.4.4.4"}).status_code
        == 200
    )


# --- against the scratch database, as api_ro


def _event(title: str, starts_at: datetime, **kwargs: Any) -> Event:
    return Event(
        title=title,
        title_normalized=normalize_key(title),
        starts_at=starts_at,
        timezone="America/Sao_Paulo",
        content_hash="0" * 64,
        **kwargs,
    )


def _venue(name: str, point: tuple[float, float] | None, city: str = "Vitória") -> Venue:
    return Venue(
        name=name,
        address=f"Rua {name}, {city}",
        address_normalized=normalize_key(f"rua {name} {city}"),
        city=city,
        state="ES" if city != "São Paulo" else "SP",
        location=None if point is None else f"SRID=4326;POINT({point[1]} {point[0]})",
    )


def _seed(engine: Engine) -> dict[str, int]:
    """The catalogue the tests below search; returns title -> id."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    day = timedelta(days=1)
    with Session(engine) as session:
        sympla = Source(name="sympla", base_url="https://sympla.example.org")
        zig = Source(name="zig", base_url="https://zig.example.org")
        music = Category(slug="music", name="Música")
        session.add(Category(slug="uncategorized", name="Sem categoria"))
        teatro = _venue("Teatro", VITORIA)
        museu = _venue("Museu", INSIDE)
        longe = _venue("Longe", OUTSIDE)
        bar = _venue("Bar", None, city="Serra")
        sp = _venue("SP", SAO_PAULO, city="São Paulo")
        org = Organizer(name="Produtora", name_normalized="produtora", url="https://p.example.org")
        events = {
            "Jazz": _event(
                "Jazz",
                now + 2 * day,
                ends_at=now + 2 * day + timedelta(hours=4),
                description="Um festival.",
                image_url="https://img.example.org/jazz.jpg",
                venue=teatro,
                category=music,
                organizer=org,
                price_lots=[
                    PriceLot(name="Gratuito", amount=Decimal("0.00"), position=0),
                    PriceLot(name="Pista", amount=Decimal("80.00"), position=1),
                ],
            ),
            "Show Pago": _event(
                "Show Pago",
                now + 3 * day,
                venue=teatro,
                category=music,
                price_lots=[PriceLot(name="Inteira", amount=Decimal("50.00"))],
            ),
            "Review": _event("Review", now + 4 * day, venue=teatro, dedupe_review=True),
            "Passado": _event("Passado", now - 2 * day, venue=teatro),
            "Cancelado": _event(
                "Cancelado", now + 3 * day, venue=teatro, status=EventStatus.CANCELLED
            ),
            "Sumiu": _event(
                "Sumiu", now + 3 * day, venue=teatro, status=EventStatus.REMOVED_FROM_SOURCE
            ),
            "Visita 1": _event(
                "09h (horário local) | Visita Guiada - Museu",
                now + day + timedelta(hours=12),
                venue=museu,
            ),
            "Visita 2": _event(
                "11h (horário local) | Visita Guiada - Museu",
                now + day + timedelta(hours=14),
                venue=museu,
            ),
            "Visita 3": _event(
                "09h (horário local) | Visita Guiada - Museu",
                now + 2 * day + timedelta(hours=12),
                venue=museu,
            ),
            "Longe": _event("Longe", now + 5 * day, venue=longe),
            "Feira": _event("Feira", now + day, venue=bar),
            "Live": _event("Live", now + day - timedelta(hours=6), is_online=True),
            "Paulista": _event("Paulista", now + 6 * day, venue=sp, category=music),
            "Distante": _event("Distante", now + 40 * day, venue=sp),
        }
        listings = []
        for n, (title, event) in enumerate(events.items(), start=1):
            city = {
                "Feira": "serra",
                "Live": "vitoria",
                "Paulista": "sao-paulo",
                "Distante": "sao-paulo",
            }.get(title, "vitoria")
            listings.append(
                SourceListing(
                    source=sympla,
                    event=event,
                    external_id=f"s-{n}",
                    city=city,
                    source_url=f"https://sympla.example.org/e/{n}",
                    content_hash="0" * 64,
                    first_seen_at=now - 2 * day,
                )
            )
        listings.append(
            SourceListing(
                source=zig,
                event=events["Jazz"],
                external_id="z-1",
                city="vila-velha",
                source_url="https://zig.example.org/e/1",
                content_hash="0" * 64,
                first_seen_at=now - day,
            )
        )
        session.add_all(listings)
        session.commit()
        return {title: e.id for title, e in events.items()}


@pytest.fixture
def catalogue(scratch_engine: Engine, api_ro_db: ApiDatabase) -> tuple[TestClient, dict[str, int]]:  # noqa: F811
    ids = _seed(scratch_engine)
    return TestClient(create_app(api_ro_db, Settings())), ids


def _ids(response: Any) -> list[int]:
    assert response.status_code == 200, response.text
    return [e["id"] for e in response.json()["data"]]


def test_radius_search_orders_by_distance_and_stops_at_the_boundary(
    catalogue: tuple[TestClient, dict[str, int]],
) -> None:
    c, ids = catalogue
    near = {"lat": VITORIA[0], "lon": VITORIA[1], "radius_km": 10}
    response = c.get("/v1/events", params=near)
    data = response.json()["data"]
    at_the_theatre = sorted([ids["Jazz"], ids["Show Pago"], ids["Review"]])  # ties break on id
    assert [e["id"] for e in data] == [*at_the_theatre, ids["Visita 1"]]
    assert [e["distance_m"] for e in data[:3]] == [0, 0, 0]
    by_id = {e["id"]: e for e in data}
    jazz, paid, review = by_id[ids["Jazz"]], by_id[ids["Show Pago"]], by_id[ids["Review"]]
    assert 9_400 <= data[3]["distance_m"] <= 9_600
    assert data[3]["sessions"] == 3 and jazz["sessions"] == 1
    assert data[3]["title"].startswith("09h")
    assert jazz["starts_at"].endswith("Z")
    assert jazz["price"] == {"min": "0.00", "max": "80.00", "currency": "BRL", "is_free": True}
    assert paid["price"] == {"min": "50.00", "max": "50.00", "currency": "BRL", "is_free": False}
    assert review["price"] is None
    assert jazz["venue"]["lat"] == pytest.approx(VITORIA[0], abs=1e-6)
    assert jazz["sources"] == [
        {"source": "sympla", "url": "https://sympla.example.org/e/1"},
        {"source": "zig", "url": "https://zig.example.org/e/1"},
    ]
    wider = _ids(c.get("/v1/events", params={**near, "radius_km": 11}))
    assert wider[-1] == ids["Longe"] and len(wider) == 5
    # Online events have no distance and come last; `only` is just them.
    included = c.get("/v1/events", params={**near, "online": "include"}).json()["data"]
    assert included[-1]["id"] == ids["Live"] and included[-1]["distance_m"] is None
    assert _ids(c.get("/v1/events", params={**near, "online": "only"})) == [ids["Live"]]


def test_city_search_orders_by_start_and_keeps_the_window(
    catalogue: tuple[TestClient, dict[str, int]],
) -> None:
    c, ids = catalogue
    assert _ids(c.get("/v1/events", params={"city": "vitoria"})) == [
        ids["Live"],
        ids["Visita 1"],
        ids["Jazz"],
        ids["Show Pago"],
        ids["Review"],
        ids["Longe"],
    ]
    assert (
        _ids(c.get("/v1/events", params={"city": "vitoria", "online": "exclude"}))[0]
        == ids["Visita 1"]
    )
    assert _ids(c.get("/v1/events", params={"city": "serra"})) == [ids["Feira"]]
    assert _ids(c.get("/v1/events", params={"city": "sao-paulo"})) == [ids["Paulista"]]
    later = {"city": "sao-paulo", "from": "2020-01-01", "to": "2020-06-01"}
    assert _ids(c.get("/v1/events", params=later)) == []
    horizon = (datetime.now(UTC) + timedelta(days=45)).date().isoformat()
    assert _ids(c.get("/v1/events", params={"city": "sao-paulo", "to": horizon})) == [
        ids["Paulista"],
        ids["Distante"],
    ]


def test_filters_by_category_free_and_title_words(
    catalogue: tuple[TestClient, dict[str, int]],
) -> None:
    c, ids = catalogue
    city = {"city": "vitoria"}
    assert _ids(c.get("/v1/events", params={**city, "category": "music"})) == [
        ids["Jazz"],
        ids["Show Pago"],
    ]
    assert _ids(c.get("/v1/events", params={**city, "category": ["music", "uncategorized"]})) == [
        ids["Live"],
        ids["Visita 1"],
        ids["Jazz"],
        ids["Show Pago"],
        ids["Review"],
        ids["Longe"],
    ]
    assert _ids(c.get("/v1/events", params={**city, "free": "true"})) == [ids["Jazz"]]
    not_free = _ids(c.get("/v1/events", params={**city, "free": "false"}))
    assert ids["Jazz"] not in not_free  # paid, and unknown prices, both stay
    assert ids["Show Pago"] in not_free and ids["Review"] in not_free
    assert _ids(c.get("/v1/events", params={**city, "q": "JÁZZ"})) == [ids["Jazz"]]
    assert _ids(c.get("/v1/events", params={**city, "q": "museu visita"})) == [ids["Visita 1"]]
    assert _ids(c.get("/v1/events", params={**city, "q": "nada"})) == []


def _walk(c: TestClient, params: dict[str, Any]) -> list[int]:
    seen: list[int] = []
    cursor: str | None = None
    for _ in range(20):
        response = c.get("/v1/events", params={**params, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(e["id"] for e in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("no end of pages")


def test_the_cursor_walks_every_row_once_in_both_sorts(
    catalogue: tuple[TestClient, dict[str, int]],
) -> None:
    c, _ = catalogue
    for params in (
        {"city": "vitoria"},
        {"lat": VITORIA[0], "lon": VITORIA[1], "radius_km": 11, "online": "include"},
        {"lat": VITORIA[0], "lon": VITORIA[1], "radius_km": 11, "sort": "starts_at"},
    ):
        whole = _ids(c.get("/v1/events", params={**params, "limit": 100}))
        assert len(whole) >= 5
        for limit in (1, 2, 4):
            assert _walk(c, {**params, "limit": limit}) == whole, (params, limit)


def test_detail_serves_every_session_with_lots_and_links_or_404_and_410(
    catalogue: tuple[TestClient, dict[str, int]],
) -> None:
    c, ids = catalogue
    response = c.get(f"/v1/events/{ids['Jazz']}")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=900"
    jazz = response.json()
    assert jazz["description"] == "Um festival."
    assert jazz["organizer"] == {"name": "Produtora", "url": "https://p.example.org"}
    assert jazz["price_lots"] == [
        {"name": "Gratuito", "amount": "0.00", "currency": "BRL"},
        {"name": "Pista", "amount": "80.00", "currency": "BRL"},
    ]
    assert jazz["price"]["is_free"] is True
    assert [s["source"] for s in jazz["sources"]] == ["sympla", "zig"]
    assert jazz["updated_at"].endswith("Z") and jazz["ends_at"].endswith("Z")
    assert "sessions" not in jazz and "distance_m" not in jazz
    assert c.get(f"/v1/events/{ids['Visita 2']}").json()["title"].startswith("11h")
    review = c.get(f"/v1/events/{ids['Review']}").json()
    assert review["organizer"] is None and review["price_lots"] == [] and review["price"] is None
    live = c.get(f"/v1/events/{ids['Live']}").json()
    assert live["venue"] is None and live["is_online"] is True

    assert c.get("/v1/events/999999").status_code == 404
    for title, status in [
        ("Cancelado", "cancelled"),
        ("Sumiu", "removed_from_source"),
        ("Passado", "ended"),
    ]:
        gone = c.get(f"/v1/events/{ids[title]}")
        assert gone.status_code == 410, title
        assert gone.json()["error"] == {
            "code": "gone",
            "message": "the event is no longer active",
            "status": status,
        }


def test_the_three_query_shapes_never_scan_events_or_venues(scratch_engine: Engine) -> None:
    """With sequential scans priced out, the plans reach every table by index.

    A seeded scratch database is too small for the planner to bother with
    indexes; forbidding seq scans proves the shapes can use them (the radius
    ones through ``ix_venues_location`` and ``ix_events_venue_id``).
    """
    _seed(scratch_engine)
    shapes = {
        "radius": build(lat=VITORIA[0], lon=VITORIA[1], now=datetime.now(UTC)),
        "city": build(city="vitoria", now=datetime.now(UTC)),
        "radius+category": build(
            lat=VITORIA[0], lon=VITORIA[1], categories=["music"], now=datetime.now(UTC)
        ),
    }
    with scratch_engine.connect() as conn:
        conn.execute(text("set local enable_seqscan = off"))
        for name, search in shapes.items():
            sql, params = list_sql(search)
            plan = "\n".join(conn.execute(text("explain " + sql), params).scalars())
            assert "Seq Scan on events" not in plan, (name, plan)
            assert "Seq Scan on venues" not in plan, (name, plan)
            if name != "city":
                assert "ix_venues_location" in plan, (name, plan)
