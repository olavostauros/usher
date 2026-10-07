"""The Nominatim clip config (ops/host/nominatim/clip.py): one area per registry city."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from evcrawl.cities import slugs

CLIP_PY = Path(__file__).resolve().parents[1] / "ops" / "host" / "nominatim" / "clip.py"


def load_clip() -> ModuleType:
    spec = importlib.util.spec_from_file_location("clip", CLIP_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["clip"] = module
    spec.loader.exec_module(module)
    return module


clip = load_clip()


def test_every_registry_city_has_a_centre_and_nothing_else() -> None:
    assert set(clip.CENTRES) == set(slugs())


def test_centres_are_in_brazil() -> None:
    for lat, lon in clip.CENTRES.values():
        assert -34.0 < lat < 5.5 and -74.0 < lon < -34.0


def test_box_is_the_radius_in_both_directions() -> None:
    w, s, e, n = clip.box_around(-20.0, -40.0, 40.0)
    assert n - s == pytest.approx(80 / 111.32)
    assert e - w > n - s  # a degree of longitude is shorter away from the equator


def test_overlapping_boxes_merge_and_disjoint_ones_stay() -> None:
    merged = clip.merge([(0, 0, 2, 2), (1, 1, 3, 3), (10, 10, 11, 11), (2.5, 2.5, 4, 4)])
    assert merged == [(0, 0, 4, 4), (10, 10, 11, 11)]


def test_config_areas_cover_every_centre_and_do_not_overlap(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert clip.main(["--radius-km", "40", "--directory", "/x"]) == 0
    config = json.loads(capsys.readouterr().out)
    assert config["directory"] == "/x"
    (extract,) = config["extracts"]
    assert extract["output"] == "brazil-metros.osm.pbf"
    boxes = [
        (ring[0][0], ring[0][1], ring[2][0], ring[2][1]) for (ring,) in extract["multipolygon"]
    ]
    for i, a in enumerate(boxes):
        assert all(not clip.overlap(a, b) for b in boxes[i + 1 :])
    for lat, lon in clip.CENTRES.values():
        assert any(w <= lon <= e and s <= lat <= n for w, s, e, n in boxes)
    # Greater Vitória's six cities are one area.
    assert len(boxes) == len(clip.CENTRES) - 5
