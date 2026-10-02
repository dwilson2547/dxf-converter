"""Colour photo -> layered geometry.

Scan mode assumes dark ink on white paper. A photo of a printed emblem breaks
that: the "ink" is several colours, the substrate is shiny and shaded, and in
grayscale red paint and a gold rim are the same mid-tone. So photo mode works
in colour:

  1. Cluster the photo's colours (k-means in Lab). The cluster that owns the
     image border is the background; everything else is the object.
  2. Decide which colours are ink. Picked colours (eyedropper / --ink) win.
     Otherwise every non-background colour covering less than a third of the
     object is ink; the big areas (a painted field, a metal rim) are substrate.
  3. Assign every pixel of an upsampled copy to its nearest palette colour.
     Upsampling first gives smooth contours from a low-resolution photo.
  4. Clean each ink mask: drop specks; drop "halo" slivers — the blended
     fringe along the edge of another colour, which nearest-colour assignment
     inevitably hands to some third colour; and drop low-contrast blobs, where
     shading on a metal rim merely drifted close to an ink colour. Real ink
     stands out from what is right around it; a highlight does not.
  5. Where a shape touches a ring (a letter running into a border), cut it free
     so the ring stays a clean circle.
  6. Trace, then regularise: circles become true circles, and with `square`
     on, lettering gets straight edges, square corners and shared baselines.

The object's silhouette is always emitted too, as layer OUTLINE.
Tolerances here are in source pixels: photo defects (blur, JPEG blocks) are
pixel-scale whatever size the object is.
"""

from __future__ import annotations

import numpy as np
import cv2
from PIL import Image

from . import shapes
from .preprocess import load_gray

TARGET_PX = 3000          # upsample until the long side is about this
PALETTE_PX = 1200         # colour clustering works on a copy this size
SUBSTRATE_FRACTION = 0.30
HALO_PX = 2.0
HALO_FRACTION = 0.5
MIN_AREA_PX = 30.0        # specks, in source px^2
CARVE_PX = 1.5            # gap cut between a ring and what touches it
CIRCLE_TOL_PX = 0.9
SMOOTH_PX = 2.5           # half-window of corner-preserving smoothing
SIMPLIFY_PX = 0.5
CORNER_SPAN_PX = 3.0
CORNER_SPAN_LETTER_PX = 1.5
SQUARE_TOL_DEG = 20.0
SQUARE_SHIFT_PX = 1.0
RECTILINEAR_FRACTION = 0.7  # outline share on H/V edges to count as lettering
INK_BIAS = 1.2           # see assign()
MIN_CONTRAST = 18.0       # Lab distance an ink blob must stand out from its surroundings
SQUARE_CORNER_PX = 7.0
SQUARE_SIMPLIFY_PX = 1.5
SHARE_LINE_PX = 1.2


# --- colour -------------------------------------------------------------------

def _lab(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)


def hex_to_lab(h: str) -> np.ndarray:
    h = h.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad colour {h!r}; use #rrggbb")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return _lab(np.array([[[b, g, r]]], np.uint8))[0, 0]


def lab_to_hex(lab: np.ndarray) -> str:
    px = np.clip(np.round(lab), 0, 255).astype(np.uint8).reshape(1, 1, 3)
    b, g, r = cv2.cvtColor(px, cv2.COLOR_LAB2BGR)[0, 0]
    return f"#{r:02x}{g:02x}{b:02x}"


def load_color(path: str) -> np.ndarray:
    with Image.open(path) as im:
        return cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)


def build_palette(bgr: np.ndarray, k: int, inks: list[str]):
    """Returns (palette_lab[N,3], ink_ids, bg_id, info)."""
    # The palette only needs a representative sample of colours, so work on a
    # copy no bigger than PALETTE_PX: a full-size 12 MP photo's filtered Lab
    # copy alone is a few hundred MB.
    h, w = bgr.shape[:2]
    k_small = min(1.0, PALETTE_PX / max(h, w))
    if k_small < 1.0:
        bgr = cv2.resize(bgr, (int(w * k_small), int(h * k_small)),
                         interpolation=cv2.INTER_AREA)
    lab = _lab(cv2.bilateralFilter(bgr, 9, 30, 9))
    flat = lab.reshape(-1, 3)
    rng = np.random.default_rng(0)
    sample = flat[rng.choice(len(flat), min(len(flat), 60000), replace=False)]
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.5)
    cv2.setRNGSeed(0)
    _, lbl, centres = cv2.kmeans(sample, int(k), None, crit, 4, cv2.KMEANS_PP_CENTERS)
    lbl = lbl.ravel()

    def nearest(px, pal):
        return np.argmin(((px[:, None, :] - pal[None, :, :]) ** 2).sum(-1), axis=1)

    border = np.vstack([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    bg_c = int(np.bincount(nearest(border, centres), minlength=len(centres)).argmax())

    if inks:
        picks = np.array([hex_to_lab(c) for c in inks], np.float32)
        # A cluster sitting on a picked colour would split that ink in two.
        others = [c for i, c in enumerate(centres)
                  if i == bg_c or np.min(np.linalg.norm(picks - c, axis=1)) > 20]
        bg_i = next(i for i, c in enumerate(others) if np.allclose(c, centres[bg_c]))
        palette = np.vstack([picks, np.array(others, np.float32)])
        ink_ids = list(range(len(picks)))
        bg_id = len(picks) + bg_i
        auto = False
    else:
        counts = np.bincount(lbl, minlength=len(centres)).astype(float)
        obj = counts.sum() - counts[bg_c]
        ink_ids = [i for i in range(len(centres))
                   if i != bg_c and obj and counts[i] / obj < SUBSTRATE_FRACTION]
        if not ink_ids:   # everything is big: take the smallest as ink
            ink_ids = [int(min((i for i in range(len(centres)) if i != bg_c),
                               key=lambda i: counts[i]))]
        palette, bg_id, auto = centres.astype(np.float32), bg_c, True

    info = {
        "auto": auto,
        "colors": [lab_to_hex(c) for c in palette],
        "background": lab_to_hex(palette[bg_id]),
        "inks": [lab_to_hex(palette[i]) for i in ink_ids],
    }
    return palette, ink_ids, bg_id, info


def assign(lab: np.ndarray, palette: np.ndarray, ink_ids=(), bias: float = 1.0) -> np.ndarray:
    """Nearest palette colour per pixel. Ink distances are divided by `bias`,
    so a pixel that is only nearly as close to an ink still goes to it: the
    darker overlap where two strokes cross shouldn't fall to a substrate
    colour. The halo and contrast filters mop up what this over-claims."""
    best = np.full(lab.shape[:2], np.inf, np.float32)
    out = np.zeros(lab.shape[:2], np.uint8)
    d = np.empty(lab.shape[:2], np.float32)
    t = np.empty_like(d)
    for i, c in enumerate(palette):
        # Channel by channel, in place: (lab - c) ** 2 on the whole image
        # makes several full-size float temporaries per colour.
        d.fill(0)
        for ch in range(3):
            np.subtract(lab[..., ch], c[ch], out=t)
            np.multiply(t, t, out=t)
            d += t
        np.sqrt(d, out=d)
        if i in ink_ids:
            d /= bias
        better = d < best
        out[better] = i
        best[better] = d[better]
    return out


# --- masks --------------------------------------------------------------------

def silhouette(labels: np.ndarray, bg_id: int, u: int) -> np.ndarray:
    obj = (labels != bg_id).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4 * u + 1, 4 * u + 1))
    obj = cv2.morphologyEx(obj, cv2.MORPH_OPEN, k)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(obj, 8)
    if n <= 1:
        return np.zeros_like(obj)
    big = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    sil = (lbl == big).astype(np.uint8) * 255
    cs, _ = cv2.findContours(sil, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = np.zeros_like(sil)
    cv2.drawContours(out, cs, -1, 255, -1)        # fill holes
    return out


def clean_inks(masks: list, sil: np.ndarray, lab: np.ndarray, u: int, rejects: list):
    """Specks, halo slivers and low-contrast blobs out.

    Halo = most of the blob hugs another ink. Contrast = Lab distance between
    the blob's median colour and the median of a thin band just outside it.
    Halos are counted but not boxed in `rejects`: a sliver following a ring
    has a bounding box the size of the ring, which would mislead more than
    it shows.
    """
    ring = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4 * u + 1, 4 * u + 1))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    masks = [cv2.morphologyEx(m & sil, cv2.MORPH_OPEN, k) for m in masks]
    hr = int(round(HALO_PX * u))
    near = [cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                    (2 * hr + 1, 2 * hr + 1)))
            for m in masks]
    # The object's own edge counts as "another colour": a sliver of ink
    # hugging the silhouette is glare along a rim, not artwork.
    edge = cv2.dilate(255 - sil, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                           (2 * hr + 1, 2 * hr + 1)))
    out = []
    for i, m in enumerate(masks):
        others = edge.copy()
        for j, nm in enumerate(near):
            if j != i:
                others |= nm
        n, lbl, st, _ = cv2.connectedComponentsWithStats(m, 8)
        keep = np.zeros(n, bool)
        for c in range(1, n):
            x, y, w, h, area = st[c]
            box = [int(x / u), int(y / u), max(1, int(w / u)), max(1, int(h / u))]
            if area < MIN_AREA_PX * u * u:
                # Only box specks big enough to matter; a photo has hundreds.
                rejects.append({"reason": "small",
                                "bbox": box if area >= MIN_AREA_PX * u * u / 3 else None,
                                "area_px": int(area / u / u)})
                continue
            comp = lbl[y:y + h, x:x + w] == c
            hug = (others[y:y + h, x:x + w][comp] > 0).mean()
            if hug > HALO_FRACTION:
                rejects.append({"reason": "halo", "bbox": None, "area_px": int(area / u / u)})
                continue
            pad = 3 * u
            y0, y1 = max(0, y - pad), min(m.shape[0], y + h + pad)
            x0, x1 = max(0, x - pad), min(m.shape[1], x + w + pad)
            inside = (lbl[y0:y1, x0:x1] == c).astype(np.uint8)
            band = (cv2.dilate(inside, ring) > 0) & (m[y0:y1, x0:x1] == 0)
            if band.any():
                sub = lab[y0:y1, x0:x1]
                contrast = np.linalg.norm(np.median(sub[inside > 0], axis=0)
                                          - np.median(sub[band], axis=0))
                if contrast < MIN_CONTRAST:
                    rejects.append({"reason": "low contrast", "bbox": box,
                                    "area_px": int(area / u / u)})
                    continue
            keep[c] = True
        out.append(np.where(keep[lbl], 255, 0).astype(np.uint8))
    return out


def fill_foreign_holes(m: np.ndarray, lab: np.ndarray, u: int) -> np.ndarray:
    """Fill holes that look more like the ink than like the shape's surroundings.

    A letter's counter shows the substrate around the letter (blurred toward
    the ink if it is narrow), so it stays a hole. Where two strokes cross, the
    darker overlap can fall to some other colour and punch a hole the artwork
    never had; it still looks like ink, so it gets filled. Only holes up to a
    quarter of their shape qualify: a ring's middle differs from its outside
    too, and that is the design.
    """
    cs, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return m
    m = m.copy()
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * u + 1, 2 * u + 1))
    for i, c in enumerate(cs):
        parent = hier[0][i][3]
        if parent < 0 or cv2.contourArea(c) > 0.25 * cv2.contourArea(cs[parent]):
            continue
        x, y, w, h = cv2.boundingRect(cs[parent])
        pad = 3 * u
        y0, y1 = max(0, y - pad), min(m.shape[0], y + h + pad)
        x0, x1 = max(0, x - pad), min(m.shape[1], x + w + pad)
        shape = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.drawContours(shape, [cs[parent]], -1, 1, -1, offset=(-x0, -y0))
        around = (cv2.dilate(shape, k) > 0) & (shape == 0)
        hole = np.zeros_like(shape)
        cv2.drawContours(hole, [c], -1, 1, -1, offset=(-x0, -y0))
        ink = (m[y0:y1, x0:x1] > 0) & (shape > 0)
        if not around.any() or not hole.any() or not ink.any():
            continue
        sub = lab[y0:y1, x0:x1]
        h_col = np.median(sub[hole > 0], axis=0)
        to_ink = np.linalg.norm(h_col - np.median(sub[ink], axis=0))
        to_out = np.linalg.norm(h_col - np.median(sub[around], axis=0))
        if to_ink < to_out:
            cv2.drawContours(m, [c], -1, 255, -1)
    return m


def _coverage(pts, cx, cy, inl) -> float:
    ang = np.arctan2(pts[inl, 1] - cy, pts[inl, 0] - cx)
    return np.unique(((ang + np.pi) / (2 * np.pi) * 36).astype(int) % 36).size / 36


def carve_rings(m: np.ndarray, u: int) -> np.ndarray:
    """Cut shapes free of a ring they touch, so the ring traces as a circle."""
    cs, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return m
    m = m.copy()
    yy = xx = None      # float32 open grids: broadcasting, not two full int64 images
    for i, c in enumerate(cs):
        pts = c[:, 0, :].astype(float)
        if len(pts) < 60:
            continue
        cx, cy, r, frac, _ = shapes.robust_circle(pts, CIRCLE_TOL_PX * u)
        if r < 10 * u or not (0.5 <= frac < 0.97):
            continue
        res = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r) < CIRCLE_TOL_PX * u
        if _coverage(pts, cx, cy, res) < 0.75:
            continue
        if yy is None:
            yy = np.arange(m.shape[0], dtype=np.float32)[:, None]
            xx = np.arange(m.shape[1], dtype=np.float32)[None, :]
        rr = np.hypot(xx - np.float32(cx), yy - np.float32(cy))
        gap = CARVE_PX * u
        if hier[0][i][3] >= 0:      # a hole: attachments sit inside it
            m[(rr > r - gap) & (rr < r - 0.5 * u)] = 0
        else:                       # an outer edge: attachments sit outside
            m[(rr > r + 0.5 * u) & (rr < r + gap)] = 0
    return m


# --- tracing ------------------------------------------------------------------

def corner_preserving_smooth(p: np.ndarray, h_max: int, s: int) -> np.ndarray:
    """Moving average whose window never reaches across a corner."""
    n = len(p)
    if n < 4 * s + 3 or h_max < 1:
        return p
    a = p - np.roll(p, s, axis=0)
    b = np.roll(p, -s, axis=0) - p
    cos = (a * b).sum(1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1) + 1e-9)
    corner = cos < np.cos(np.radians(40))
    idx = np.arange(n)
    if corner.any():
        # Distance (in contour steps, wrapping) to the nearest corner, via a
        # sorted search rather than an n x corners matrix.
        cidx = idx[corner]
        ext = np.concatenate([cidx - n, cidx, cidx + n])
        pos = np.searchsorted(ext, idx)
        d = np.minimum(np.abs(ext[pos] - idx), np.abs(idx - ext[pos - 1]))
    else:
        d = np.full(n, n)
    h = np.clip(d - 1, 0, h_max).astype(int)
    ext = np.vstack([p, p, p])
    csum = np.vstack([np.zeros((1, 2)), np.cumsum(ext, axis=0)])
    i = idx + n
    return (csum[i + h + 1] - csum[i - h]) / (2 * h + 1)[:, None]


def _outline(pts: np.ndarray, u: int, span_px: float):
    """Smooth + simplify a raw pixel contour (fine outline)."""
    sm = corner_preserving_smooth(pts, int(round(SMOOTH_PX * u)),
                                  max(2, int(round(span_px * u))))
    return sm, cv2.approxPolyDP(sm.astype(np.float32).reshape(-1, 1, 2),
                                SIMPLIFY_PX * u, True)[:, 0, :].astype(float)


def axis_fraction(coarse: np.ndarray, u: int) -> float:
    """Share of a coarse outline's perimeter on near-horizontal/vertical edges."""
    n = len(coarse)
    if n < 3:
        return 0.0
    kinds = shapes._kinds(coarse, SQUARE_TOL_DEG, SQUARE_SHIFT_PX * u)
    lengths = np.hypot(*(np.roll(coarse, -1, axis=0) - coarse).T)
    total = lengths.sum()
    return float(lengths[[k is not None for k in kinds]].sum() / total) if total else 0.0


def classify(c, u: int, cfg, rough: bool):
    """Decide what a shape is before deciding how to clean it up.

    circle      -> a true circle
    rectilinear -> lettering-style: mostly horizontal/vertical edges
    freeform    -> everything else (a swoosh, a tapered star, a silhouette)
    """
    pts = c[:, 0, :].astype(float)
    if cfg.circles and len(pts) >= 60:
        # A silhouette edge is rougher (glare, shadow, a bevelled rim) than
        # printed ink, so it qualifies as a circle on looser terms.
        tol, need, max_rms = (2.0, 0.85, 1.0) if rough else (CIRCLE_TOL_PX, 0.97, 0.6)
        cx, cy, r, frac, rms = shapes.robust_circle(pts, tol * u)
        if r >= 8 * u and frac >= need and rms <= max_rms * u:
            return "circle", (cx, cy, r)
    if cfg.square and not rough:
        sm, _ = _outline(pts, u, CORNER_SPAN_LETTER_PX)
        coarse = cv2.approxPolyDP(sm.astype(np.float32).reshape(-1, 1, 2),
                                  SQUARE_SIMPLIFY_PX * u, True)[:, 0, :].astype(float)
        if axis_fraction(coarse, u) >= RECTILINEAR_FRACTION:
            return "rectilinear", coarse
    return "freeform", None


def trace_layer(m: np.ndarray, u: int, cfg, rough: bool = False) -> list:
    """Mask -> [(points_xy_up, kind)] where kind is 'circle' or 'poly'.

    Each shape is classified first (see classify) and cleaned up by the
    rules for its class, rather than one set of rules for the whole layer.
    """
    cs, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return []
    classes = {}

    def cls(i):
        if i not in classes:
            classes[i] = classify(cs[i], u, cfg, rough)
        return classes[i]

    out, squared = [], []
    for i, c in enumerate(cs):
        if cv2.contourArea(c) < MIN_AREA_PX * u * u * 0.5:
            continue
        pts = c[:, 0, :].astype(float)
        parent = hier[0][i][3]
        kind, info = cls(i)

        if kind == "circle":
            out.append((shapes.circle_points(*info), "circle"))
            continue

        # A counter inside a letter is part of the letter: it follows the
        # letter's class, whatever its own outline looks like.
        letter = kind == "rectilinear" or (parent >= 0 and cls(parent)[0] == "rectilinear")
        if letter and parent >= 0 and shapes.is_boxy(pts, 0.7):
            out.append((shapes.box(pts), "poly"))
            squared.append(len(out) - 1)
            continue

        # Corners are judged over a span: short for lettering, whose corners
        # are close together and must stay crisp; long for everything else,
        # so a one-pixel stair-step on a thin line isn't mistaken for one.
        sm, ap = _outline(pts, u, CORNER_SPAN_LETTER_PX if letter else CORNER_SPAN_PX)
        if len(ap) < 3:
            continue
        if letter:
            coarse = info if kind == "rectilinear" else cv2.approxPolyDP(
                sm.astype(np.float32).reshape(-1, 1, 2),
                SQUARE_SIMPLIFY_PX * u, True)[:, 0, :].astype(float)
            ap = shapes.snap_guided(ap, coarse, SQUARE_TOL_DEG, SQUARE_SHIFT_PX * u,
                                    (SQUARE_SIMPLIFY_PX + SIMPLIFY_PX) * u)
            ap = shapes.square_corners(ap, SQUARE_CORNER_PX * u)
            squared.append(len(out))
        out.append((ap, "poly"))

    if squared:
        lined = shapes.share_lines([out[i][0] for i in squared], SHARE_LINE_PX * u)
        for i, p in zip(squared, lined):
            out[i] = (p, "poly")
    return out


# --- entry point --------------------------------------------------------------

def extract(path: str, cfg):
    """Returns (gray, paths_px[(row,col) arrays, closed], meta, dpi, report)."""
    gray, dpi, dpi_source = load_gray(path, cfg.dpi)
    bgr = load_color(path)
    h, w = bgr.shape[:2]
    u = cfg.upsample or int(np.clip(round(TARGET_PX / max(h, w)), 1, 4))

    palette, ink_ids, bg_id, info = build_palette(bgr, cfg.colors, cfg.inks)
    up = cv2.resize(bgr, None, fx=u, fy=u, interpolation=cv2.INTER_CUBIC) if u > 1 else bgr
    lab = _lab(cv2.GaussianBlur(up, (5, 5), 0))
    labels = assign(lab, palette, ink_ids, 1.0 if info['auto'] else INK_BIAS)
    sil = silhouette(labels, bg_id, u)

    rejects: list = []
    masks = clean_inks([((labels == i) * 255).astype(np.uint8) for i in ink_ids],
                       sil, lab, u, rejects)
    masks = [fill_foreign_holes(m, lab, u) for m in masks]
    if cfg.circles:
        masks = [carve_rings(m, u) for m in masks]

    layers = [("OUTLINE", info["background"], sil, False)]
    layers += [(f"INK{n + 1}", info["inks"][n], m, True) for n, m in enumerate(masks)]

    paths_px, meta = [], []
    layer_report = []
    for name, color, m, is_ink in layers:
        if not is_ink:
            # The silhouette's outer edge only; its inside is the inks' business.
            cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            m = np.zeros_like(m)
            if cs:
                cv2.drawContours(m, [max(cs, key=cv2.contourArea)], -1, 255, -1)
            color = "#8a8f98"
        traced = trace_layer(m, u, cfg, rough=not is_ink)
        for pts, kind in traced:
            rc = np.column_stack([pts[:, 1], pts[:, 0]]) / u
            paths_px.append((rc, True))
            meta.append({"layer": name, "color": color, "kind": kind})
        layer_report.append({"name": name, "color": color, "paths": len(traced)})

    n_circles = sum(1 for m_ in meta if m_["kind"] == "circle")
    pre = {
        "kept": len(paths_px),
        "dropped_border": 0,
        "dropped_small": sum(1 for r in rejects if r["reason"] == "small"),
        "dropped_halo": sum(1 for r in rejects if r["reason"] == "halo"),
        "dropped_contrast": sum(1 for r in rejects if r["reason"] == "low contrast"),
        "rejects": [r for r in rejects if r["bbox"]],
        "ink_fraction": float(sum((m > 0).mean() for m in masks)),
        "dpi": dpi,
        "dpi_source": dpi_source,
        "px_per_mm": cfg.px_per_mm(dpi),
        "upsample": u,
        "palette": info,
        "layers": layer_report,
    }
    vec = {
        "segments": len(paths_px), "stitched": len(paths_px),
        "collapsed_junctions": 0, "dropped_short": 0, "dropped_spur": 0,
        "kept": len(paths_px), "closed": len(paths_px), "circles": n_circles,
        "vertices": int(sum(len(p) if m_["kind"] != "circle" else 1
                            for (p, _), m_ in zip(paths_px, meta))),
    }
    return gray, paths_px, meta, dpi, {"preprocess": pre, "vectorize": vec}
