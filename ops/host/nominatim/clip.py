#!/usr/bin/env python3
"""Write the osmium extract config that clips Brazil to the registry's metro areas.

The full Brazil import is tens of GB even frozen; the pipeline only geocodes
venues of the cities in ``src/evcrawl/cities.py``. This draws a square of
``--radius-km`` around each city's centre, merges squares that overlap (Greater
Vitória is six cities, one area) and writes a config for::

    osmium extract --strategy smart -c clip.json brazil-latest.osm.pbf

``smart`` keeps every way and relation that touches an area whole, so an
avenue or a neighbourhood boundary on the edge is not cut. A venue outside
every area is a local miss and goes to ``EVCRAWL_GEOCODE_FALLBACK_URL``
(``ops/host/README.md`` "Clipped"). Standard library only; the host runs it.
"""

import argparse
import json
import math
import sys
from pathlib import Path

DEFAULT_RADIUS_KM = 40.0
OUTPUT = "brazil-metros.osm.pbf"
KM_PER_DEGREE_LAT = 111.32

# (lat, lon) of each registry slug's centre; tests/test_clip.py holds the keys
# equal to the registry, so a new city cannot be added without its area.
CENTRES: dict[str, tuple[float, float]] = {
    "vitoria": (-20.3155, -40.3128),
    "vila-velha": (-20.3297, -40.2925),
    "serra": (-20.1286, -40.3075),
    "cariacica": (-20.2632, -40.4165),
    "viana": (-20.3900, -40.4964),
    "guarapari": (-20.6716, -40.4997),
    "rio-branco": (-9.9747, -67.8100),
    "maceio": (-9.6658, -35.7353),
    "manaus": (-3.1190, -60.0217),
    "macapa": (0.0349, -51.0694),
    "salvador": (-12.9777, -38.5016),
    "fortaleza": (-3.7319, -38.5267),
    "brasilia": (-15.7939, -47.8828),
    "goiania": (-16.6869, -49.2648),
    "sao-luis": (-2.5307, -44.3068),
    "belo-horizonte": (-19.9167, -43.9345),
    "campo-grande": (-20.4697, -54.6201),
    "cuiaba": (-15.6014, -56.0979),
    "belem": (-1.4558, -48.4902),
    "joao-pessoa": (-7.1195, -34.8450),
    "recife": (-8.0476, -34.8770),
    "teresina": (-5.0920, -42.8038),
    "curitiba": (-25.4284, -49.2733),
    "rio-de-janeiro": (-22.9068, -43.1729),
    "natal": (-5.7945, -35.2110),
    "porto-velho": (-8.7612, -63.9004),
    "boa-vista": (2.8235, -60.6758),
    "porto-alegre": (-30.0346, -51.2177),
    "florianopolis": (-27.5954, -48.5480),
    "aracaju": (-10.9472, -37.0731),
    "sao-paulo": (-23.5505, -46.6333),
    "palmas": (-10.1840, -48.3336),
}

Box = tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat


def box_around(lat: float, lon: float, radius_km: float) -> Box:
    """The square of half-side *radius_km* around a point, in degrees."""
    dlat = radius_km / KM_PER_DEGREE_LAT
    dlon = radius_km / (KM_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def overlap(a: Box, b: Box) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def merge(boxes: list[Box]) -> list[Box]:
    """Replace overlapping boxes by their bounding box until none overlap.

    osmium reads a multipolygon by ring crossings, so two overlapping squares
    would cancel out where they meet; disjoint boxes are always safe.
    """
    merged = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                if overlap(merged[i], merged[j]):
                    a, b = merged[i], merged.pop(j)
                    merged[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    changed = True
                    break
            if changed:
                break
    return sorted(merged)


def ring(box: Box) -> list[list[float]]:
    w, s, e, n = (round(v, 5) for v in box)
    return [[w, s], [e, s], [e, n], [w, n], [w, s]]


def config(radius_km: float, directory: str) -> dict[str, object]:
    boxes = merge([box_around(lat, lon, radius_km) for lat, lon in CENTRES.values()])
    return {
        "directory": directory,
        "extracts": [{"output": OUTPUT, "multipolygon": [[ring(b)] for b in boxes]}],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
    parser.add_argument("--directory", default=str(Path(__file__).resolve().parent / "pbf"))
    args = parser.parse_args(argv)
    if args.radius_km <= 0:
        parser.error("--radius-km must be positive")
    json.dump(config(args.radius_km, args.directory), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
