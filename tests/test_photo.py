"""Tests for photo mode.

The synthetic badge mimics what made photo mode necessary: a grey background,
a gold disc, a red ring with block lettering that runs into it, and a thin
brown cross on a cream field — then blurred and noised like a photo. Its
geometry is known in pixels, so circles and squared edges are checked against
ground truth.
"""

import os
import sys

import numpy as np
import cv2
import ezdxf
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dxfconv import Config, convert            # noqa: E402
from dxfconv import shapes                     # noqa: E402

N = 900
C = (450, 450)
R_DISC, R_RING_OUT, R_RING_IN = 420, 335, 320
GREY, GOLD, CREAM = (200, 200, 200), (60, 110, 150), (150, 200, 235)   # BGR
RED, BROWN = (20, 30, 170), (60, 105, 160)
RED_HEX, BROWN_HEX = "#aa1e14", "#a0693c"


def badge(tmp_path, name="badge.png", cross=True):
    img = np.full((N, N, 3), GREY, np.uint8)
    cv2.circle(img, C, R_DISC, GOLD, -1, cv2.LINE_AA)
    cv2.circle(img, C, R_RING_OUT, RED, -1, cv2.LINE_AA)
    cv2.circle(img, C, R_RING_IN, CREAM, -1, cv2.LINE_AA)
    # An "E": a block with two notches cut in from the right.
    cv2.rectangle(img, (300, 420), (360, 500), RED, -1)
    cv2.rectangle(img, (325, 440), (361, 450), CREAM, -1)
    cv2.rectangle(img, (325, 470), (361, 480), CREAM, -1)
    # A block with an enclosed slit.
    cv2.rectangle(img, (400, 420), (460, 500), RED, -1)
    cv2.rectangle(img, (415, 455), (445, 465), CREAM, -1)
    # A bar that runs into the ring, like the G on the Seeburg badge.
    cv2.rectangle(img, (500, 420), (C[0] + R_RING_IN + 5, 500), RED, -1)
    if cross:
        # A sparkle: arms that taper from 8 px at the centre to a point.
        star = np.array([[250, 250], [254, 296], [310, 300], [254, 304], [250, 380],
                         [246, 304], [190, 300], [246, 296]], np.int32)
        cv2.fillPoly(img, [star], BROWN, cv2.LINE_AA)
    img = cv2.GaussianBlur(img, (5, 5), 1.2)
    rng = np.random.default_rng(1)
    img = np.clip(img + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    path = str(tmp_path / name)
    cv2.imwrite(path, img)
    return path


def run(tmp_path, **kw):
    out = str(tmp_path / "out.dxf")
    r = convert(badge(tmp_path), out, Config(source="photo", **kw))
    return r, ezdxf.readfile(out)


def entities(doc, kind=None, layer=None):
    return [e for e in doc.modelspace()
            if (kind is None or e.dxftype() == kind)
            and (layer is None or e.dxf.layer == layer)]


# --- colour separation ----------------------------------------------------

def test_auto_finds_the_red_ink_and_skips_big_substrate_areas(tmp_path):
    r, doc = run(tmp_path)
    pal = r["preprocess"]["palette"]
    assert pal["auto"]
    assert len(pal["inks"]) == 1
    b, g, rr = (int(pal["inks"][0][i:i + 2], 16) for i in (5, 3, 1))
    assert rr > 120 and g < 80 and b < 80          # it is the red
    assert {e.dxf.layer for e in doc.modelspace()} == {"OUTLINE", "INK1"}


def test_picked_inks_add_a_layer_for_the_thin_detail(tmp_path):
    r, doc = run(tmp_path, inks=[RED_HEX, BROWN_HEX])
    cross = entities(doc, "LWPOLYLINE", "INK2")
    assert len(cross) == 1, "the cross is one shape, with no halo slivers"
    pts = np.array(cross[0].get_points("xy"))
    w, h = pts.max(axis=0) - pts.min(axis=0)
    px = r["preprocess"]["px_per_mm"]
    assert w * px == pytest.approx(120, abs=8)
    assert h * px == pytest.approx(130, abs=8)


def test_layers_carry_their_colour_into_the_dxf(tmp_path):
    _, doc = run(tmp_path)
    assert doc.layers.get("INK1").rgb[0] > 120


# --- circles --------------------------------------------------------------

def test_disc_and_ring_become_true_circles(tmp_path):
    r, doc = run(tmp_path)
    px = r["preprocess"]["px_per_mm"]
    radii = sorted(e.dxf.radius * px for e in entities(doc, "CIRCLE"))
    assert len(radii) == 3
    for got, want in zip(radii, (R_RING_IN, R_RING_OUT, R_DISC)):
        assert got == pytest.approx(want, abs=2.0)


def test_shape_touching_the_ring_is_cut_free(tmp_path):
    """The bar runs into the ring: the ring must still trace as two circles,
    and the bar must still be there as its own outline."""
    r, doc = run(tmp_path)
    assert len(entities(doc, "CIRCLE", "INK1")) == 2
    px = r["preprocess"]["px_per_mm"]
    bars = [np.array(e.get_points("xy")) * px for e in entities(doc, "LWPOLYLINE", "INK1")]
    widths = [p[:, 0].max() - p[:, 0].min() for p in bars]
    assert any(widths_ > 200 for widths_ in widths)


def test_no_circles_flag_keeps_polylines(tmp_path):
    _, doc = run(tmp_path, circles=False)
    assert not entities(doc, "CIRCLE")


# --- scale ----------------------------------------------------------------

def test_fit_mm_sets_the_largest_dimension(tmp_path):
    r, doc = run(tmp_path, fit_mm=76.0)
    assert r["extents_mm"]["width_mm"] == pytest.approx(76.0, abs=0.05)
    assert r["dpi_source"] == "fit to 76 mm"
    outer = max(entities(doc, "CIRCLE"), key=lambda e: e.dxf.radius)
    assert outer.dxf.radius * 2 == pytest.approx(76.0, abs=0.2)


# --- squaring up ----------------------------------------------------------

def _axis_aligned(p, tol=1e-6):
    d = np.diff(np.vstack([p, p[:1]]), axis=0)
    return np.all((np.abs(d[:, 0]) < tol) | (np.abs(d[:, 1]) < tol))


def test_square_makes_lettering_rectilinear_and_keeps_counters(tmp_path):
    r, doc = run(tmp_path, square=True)
    polys = [np.array(e.get_points("xy")) for e in entities(doc, "LWPOLYLINE", "INK1")]
    # E, block, block's slit, bar.
    assert len(polys) == 4
    bar = max(polys, key=lambda p: p[:, 0].max())
    for p in polys:
        if p is not bar:          # the bar's cut end follows the ring's curve
            assert _axis_aligned(p), p
    px = r["preprocess"]["px_per_mm"]
    slit = min(polys, key=lambda p: np.ptp(p[:, 0]) * np.ptp(p[:, 1]))
    assert np.ptp(slit[:, 0]) * px == pytest.approx(30, abs=3)
    assert np.ptp(slit[:, 1]) * px == pytest.approx(10, abs=3)
    # The E keeps both notches: 12 corners.
    e_shape = min(polys, key=lambda p: p[:, 0].min())
    assert len(e_shape) == 12


def test_square_shares_the_baseline_across_letters(tmp_path):
    _, doc = run(tmp_path, square=True)
    polys = [np.array(e.get_points("xy")) for e in entities(doc, "LWPOLYLINE", "INK1")]
    bottoms = sorted(p[:, 1].min() for p in polys if np.ptp(p[:, 1]) > 1)
    assert bottoms[0] == pytest.approx(bottoms[1], abs=1e-9)


def test_square_leaves_tapered_shapes_alone(tmp_path):
    """A tapered sparkle is not lettering: squaring would turn its taper into steps."""
    _, plain = run(tmp_path, inks=[RED_HEX, BROWN_HEX])
    _, sq = run(tmp_path, inks=[RED_HEX, BROWN_HEX], square=True)
    a = entities(plain, "LWPOLYLINE", "INK2")[0].get_points("xy")
    b = entities(sq, "LWPOLYLINE", "INK2")[0].get_points("xy")
    assert np.allclose(np.array(a), np.array(b))


# --- shape helpers --------------------------------------------------------

def test_robust_circle_ignores_attachments():
    pts = shapes.circle_points(100, 100, 50, 360)
    bump = np.column_stack([np.linspace(130, 150, 60), np.full(60, 80.0)])
    cx, cy, r, frac, rms = shapes.robust_circle(np.vstack([pts, bump]), 1.0)
    assert (cx, cy, r) == pytest.approx((100, 100, 50), abs=0.1)
    assert 0.8 < frac < 0.9


def test_square_corners_turns_a_bevel_into_a_right_angle():
    p = np.array([[0, 0], [50, 0], [50, 40], [46, 50], [0, 50]], float)
    q = shapes.square_corners(p, max_len=12)
    assert len(q) == 4
    assert _axis_aligned(q)
    assert [50, 50] in q.tolist()


def test_square_corners_flattens_a_pointed_slit_tip():
    # A slit whose right end comes to a point.
    p = np.array([[0, 0], [30, 0], [35, 5], [30, 10], [0, 10]], float)
    q = shapes.square_corners(p, max_len=12)
    assert _axis_aligned(q)
    assert q[:, 0].max() == pytest.approx(35)


def test_snap_leaves_a_long_gentle_slope_alone():
    """A swoosh edge 3 degrees off horizontal over 200 px is a curve, not a
    wobbly horizontal: snapping it would move its ends by 10 px."""
    p = np.array([[0, 0], [200, 10], [200, 30], [0, 30]], float)
    q = shapes.snap_guided(p, p, tol_deg=20, max_shift=1, near=1)
    assert [200, 10] in q.tolist()


def test_snap_guided_keeps_a_taper_chopped_into_short_pieces():
    """The fine outline of a tapered arm is many short, nearly flat pieces.
    Judged on the coarse outline it is one sloped edge, so nothing snaps."""
    x = np.linspace(0, 200, 21)
    top = np.column_stack([x, 10 - x / 40])          # tapers 5 px over 200
    fine = np.vstack([top, [[200, 20], [0, 20]]])
    coarse = np.array([[0, 10], [200, 5], [200, 20], [0, 20]], float)
    q = shapes.snap_guided(fine, coarse, tol_deg=20, max_shift=1, near=2)
    assert np.ptp(q[q[:, 1] < 15][:, 1]) == pytest.approx(5)
