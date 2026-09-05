"""Tests for the scan -> DXF pipeline.

The synthetic cases matter most: they are drawn at a known size in millimetres,
so scale accuracy is measured against ground truth rather than asserted.
"""

import os
import sys

import numpy as np
import cv2
import ezdxf
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dxfconv import Config, convert          # noqa: E402
from dxfconv import vectorize                # noqa: E402


DPI = 300.0
PX_PER_MM = DPI / 25.4
SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples")


def mm(v):
    return int(round(v * PX_PER_MM))


def write_scan(tmp_path, draw, name="scan.png", size_mm=(120, 120)):
    """Render a white page, let `draw` put ink on it, save at 300 DPI."""
    h, w = mm(size_mm[1]), mm(size_mm[0])
    img = np.full((h, w), 255, np.uint8)
    draw(img)
    path = str(tmp_path / name)
    Image.fromarray(img).save(path, dpi=(DPI, DPI))
    return path


# --- scale fidelity -------------------------------------------------------

def test_square_comes_back_at_its_real_size(tmp_path):
    """A 50 mm square traced with a 0.5 mm pen must measure 50 mm."""
    side = 50.0

    def draw(img):
        cv2.rectangle(img, (mm(20), mm(20)), (mm(20 + side), mm(20 + side)),
                      0, mm(0.5))

    src = write_scan(tmp_path, draw)
    out = str(tmp_path / "square.dxf")
    r = convert(src, out, Config(mode="centerline"))

    ext = r["extents_mm"]
    # Centreline of a stroke that straddles the nominal edge -> nominal size.
    assert ext["width_mm"] == pytest.approx(side, abs=0.3)
    assert ext["height_mm"] == pytest.approx(side, abs=0.3)


def test_outline_brackets_the_true_edge(tmp_path):
    """Why outline is the default.

    Outline returns both sides of the stroke, so the true edge is one of the
    two contours rather than something half a stroke-width away from the only
    contour you got. Tracing rides the pen against the object, so the inner
    contour is the part you measure against.
    """
    side, pen = 50.0, 0.6

    def draw(img):
        cv2.rectangle(img, (mm(20), mm(20)), (mm(20 + side), mm(20 + side)),
                      0, mm(pen))

    src = write_scan(tmp_path, draw)
    r = convert(src, str(tmp_path / "o.dxf"), Config(mode="outline"))

    assert r["paths"] == 2, "outer and inner side of the stroke"
    outer = r["extents_mm"]["width_mm"]
    assert outer == pytest.approx(side + pen, abs=0.3)

    doc = ezdxf.readfile(str(tmp_path / "o.dxf"))
    widths = []
    for e in doc.modelspace().query("LWPOLYLINE"):
        xs = [p[0] for p in e.get_points("xy")]
        widths.append(max(xs) - min(xs))
    inner = min(widths)
    assert inner == pytest.approx(side - pen, abs=0.3)


def test_scale_is_independent_of_scan_dpi(tmp_path):
    """The same drawing scanned at 600 DPI must yield the same millimetres."""
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(30), 0, mm(0.5))

    src300 = write_scan(tmp_path, draw, "c300.png")

    img = np.array(Image.open(src300))
    big = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    src600 = str(tmp_path / "c600.png")
    Image.fromarray(big).save(src600, dpi=(600, 600))

    a = convert(src300, str(tmp_path / "a.dxf"), Config())["extents_mm"]
    b = convert(src600, str(tmp_path / "b.dxf"), Config())["extents_mm"]

    assert b["width_mm"] == pytest.approx(a["width_mm"], abs=0.3)
    assert b["height_mm"] == pytest.approx(a["height_mm"], abs=0.3)
    assert a["width_mm"] == pytest.approx(60.0, abs=0.5)


def test_scale_factor_is_applied(tmp_path):
    def draw(img):
        cv2.rectangle(img, (mm(20), mm(20)), (mm(70), mm(70)), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    full = convert(src, str(tmp_path / "a.dxf"), Config())["extents_mm"]
    half = convert(src, str(tmp_path / "b.dxf"), Config(scale=0.5))["extents_mm"]
    assert half["width_mm"] == pytest.approx(full["width_mm"] * 0.5, abs=0.05)


# --- artifact rejection ---------------------------------------------------

def test_specks_and_edge_stripes_are_rejected(tmp_path):
    """The two artifact classes a real scanner produces, together."""
    rng = np.random.default_rng(0)

    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(30), 0, mm(0.5))
        # Scanner edge stripe: long and thin, so size filters alone miss it.
        cv2.rectangle(img, (0, mm(10)), (mm(0.5), mm(90)), 0, -1)
        # Dust.
        for _ in range(200):
            y = rng.integers(0, img.shape[0])
            x = rng.integers(0, img.shape[1])
            cv2.circle(img, (int(x), int(y)), rng.integers(1, 3), 0, -1)

    src = write_scan(tmp_path, draw)
    out = str(tmp_path / "noisy.dxf")
    r = convert(src, out, Config(mode="centerline"))

    assert r["paths"] == 1, "only the circle should survive"
    assert r["preprocess"]["dropped_border"] >= 1
    assert r["extents_mm"]["width_mm"] == pytest.approx(60.0, abs=0.5)


def test_speck_only_page_produces_empty_dxf(tmp_path):
    """No geometry is better than junk geometry."""
    rng = np.random.default_rng(1)

    def draw(img):
        for _ in range(300):
            y = rng.integers(0, img.shape[0])
            x = rng.integers(0, img.shape[1])
            cv2.circle(img, (int(x), int(y)), 1, 0, -1)

    src = write_scan(tmp_path, draw)
    out = str(tmp_path / "empty.dxf")
    r = convert(src, out, Config())
    assert r["paths"] == 0
    assert len(ezdxf.readfile(out).modelspace()) == 0


# --- topology -------------------------------------------------------------

def test_closed_shape_stays_closed(tmp_path):
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(30), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    r = convert(src, str(tmp_path / "c.dxf"), Config(mode="centerline"))
    assert r["vectorize"]["closed"] == 1


def test_crossing_strokes_stitch_into_continuous_paths(tmp_path):
    """Two lines through one crossing -> two paths, not four fragments."""
    def draw(img):
        cv2.line(img, (mm(20), mm(60)), (mm(100), mm(60)), 0, mm(0.5))
        cv2.line(img, (mm(60), mm(20)), (mm(60), mm(100)), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    r = convert(src, str(tmp_path / "x.dxf"), Config(mode="centerline"))

    assert r["vectorize"]["segments"] >= 4, "skeleton should split at the crossing"
    assert r["paths"] == 2, "tangent pairing should rejoin them"


def test_loop_crossed_by_a_line_still_closes(tmp_path):
    """The inner-triangle case from the real scan, in miniature.

    The chord cuts the circle into two arcs at two junctions; stitching has to
    put them back together as one closed loop.
    """
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(20), 0, mm(0.5))
        cv2.line(img, (mm(20), mm(50)), (mm(100), mm(50)), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    r = convert(src, str(tmp_path / "t.dxf"), Config(mode="centerline"))
    assert r["vectorize"]["closed"] >= 1, "the circle should survive as a loop"
    assert r["paths"] == 2, "circle plus chord"


# --- DXF hygiene: the actual Onshape requirement --------------------------

def test_dxf_contains_only_geometry_on_one_layer(tmp_path):
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(30), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    out = str(tmp_path / "clean.dxf")
    convert(src, out, Config())

    msp = ezdxf.readfile(out).modelspace()
    assert len(msp.query("POINT")) == 0, "stray points are what break the import"
    assert len(msp.query("INSERT")) == 0
    assert {e.dxftype() for e in msp} == {"LWPOLYLINE"}
    assert {e.dxf.layer for e in msp} == {"PROFILE"}


def test_dxf_declares_millimetres(tmp_path):
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(20), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    out = str(tmp_path / "u.dxf")
    convert(src, out, Config())
    doc = ezdxf.readfile(out)
    assert doc.header["$INSUNITS"] == 4
    assert doc.header["$MEASUREMENT"] == 1


def test_spline_output_is_readable(tmp_path):
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(30), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    out = str(tmp_path / "s.dxf")
    convert(src, out, Config(entity="spline"))
    assert len(ezdxf.readfile(out).modelspace().query("SPLINE")) >= 1


# --- helpers --------------------------------------------------------------

def test_resample_and_smooth_preserve_endpoints():
    pts = np.array([[0.0, 0.0], [0.0, 10.0], [0.0, 20.0]])
    out = vectorize.smooth(vectorize.resample(pts, 1.0), 5, closed=False)
    assert out[0] == pytest.approx(pts[0])
    assert out[-1] == pytest.approx(pts[-1])


def test_dpi_read_from_metadata(tmp_path):
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(20), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    r = convert(src, str(tmp_path / "d.dxf"), Config())
    assert r["dpi"] == 300.0
    assert r["dpi_source"] == "image metadata"


def test_dpi_override_wins(tmp_path):
    def draw(img):
        cv2.circle(img, (mm(60), mm(60)), mm(20), 0, mm(0.5))

    src = write_scan(tmp_path, draw)
    r = convert(src, str(tmp_path / "d.dxf"), Config(dpi=150))
    assert r["dpi"] == 150.0
    # Half the DPI, so the same pixels describe twice the millimetres.
    assert r["extents_mm"]["width_mm"] == pytest.approx(80.0, abs=1.0)


# --- the real scan --------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(os.path.join(SAMPLES, "cl_35_profile.png")),
                    reason="sample scan not present")
def test_real_scan_matches_hand_cleaned_version(tmp_path):
    """The dirty original must yield the same geometry as the Paint cleanup."""
    cfg = Config(mode="centerline")
    raw = convert(os.path.join(SAMPLES, "cl_35_profile.png"),
                  str(tmp_path / "raw.dxf"), cfg)
    cleaned = convert(os.path.join(SAMPLES, "cl_35_profile_cleaned.png"),
                      str(tmp_path / "cleaned.dxf"), cfg)

    assert raw["paths"] == cleaned["paths"] == 4
    assert raw["vectorize"]["closed"] == 1
    assert raw["preprocess"]["dropped_border"] >= 1

    for key in ("width_mm", "height_mm"):
        assert raw["extents_mm"][key] == pytest.approx(
            cleaned["extents_mm"][key], abs=0.5)
