"""Shape regularisation for photo mode: circles and squared-up lettering.

A photo of a printed emblem is soft at the pixel level — JPEG blocking, lens
blur, paint that bled a little. Tracing that faithfully gives wobbly outlines
of shapes that were drawn with a compass and a T-square. These helpers put the
compass and T-square back: contours that are circles become true circles, and
edges that are almost horizontal or vertical become exactly so.

Everything here works on (x, y) point arrays in pixels. Tolerances are pixels
of the *source* image, because the defects being removed are pixel-scale.
"""

from __future__ import annotations

import numpy as np


# --- circles ----------------------------------------------------------------

def fit_circle(pts: np.ndarray):
    """Algebraic least-squares circle (Kasa). Returns (cx, cy, r)."""
    x, y = pts[:, 0].astype(float), pts[:, 1].astype(float)
    a = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    cx, cy, c = np.linalg.lstsq(a, x * x + y * y, rcond=None)[0]
    return float(cx), float(cy), float(np.sqrt(max(c + cx * cx + cy * cy, 0.0)))


def robust_circle(pts: np.ndarray, tol: float, iters: int = 6):
    """Circle fit that ignores whatever is attached to the circle.

    A letter running into a ring drags a plain fit off-centre, and when the
    attachment is a big share of the contour, even iterative trimming starting
    from that fit converges on the wrong circle. So the fit is seeded from
    several quarter-arcs of the (ordered) contour, each refined on the points
    that agree with it, and the candidate explaining the most points wins.
    Returns (cx, cy, r, inlier_fraction, rms_of_inliers).
    """
    pts = np.asarray(pts, float)
    n = len(pts)

    def refine(seed):
        keep = seed
        for _ in range(iters):
            if len(keep) < 8:
                break
            cx, cy, r = fit_circle(keep)
            res = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
            nxt = pts[res < tol * 1.5]
            if len(nxt) < 8 or len(nxt) == len(keep):
                break
            keep = nxt
        cx, cy, r = fit_circle(keep)
        res = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
        inl = res < tol
        rms = float(np.sqrt(np.mean(res[inl] ** 2))) if inl.any() else float("inf")
        return cx, cy, r, float(inl.mean()), rms

    seeds = [pts]
    if n >= 32:
        q = n // 4
        seeds += [np.roll(pts, -k * n // 12, axis=0)[:q] for k in range(12)]
    best = None
    for seed in seeds:
        cand = refine(seed)
        if best is None or cand[3] > best[3]:
            best = cand
    return best


def circle_points(cx: float, cy: float, r: float, n: int = 0) -> np.ndarray:
    n = n or int(np.clip(r / 2, 48, 360))
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)])


# --- squaring up ------------------------------------------------------------

def _exact_kinds(p: np.ndarray, eps: float = 1e-9) -> list:
    """'H'/'V' only for edges that are already exactly axis-aligned."""
    n = len(p)
    out = []
    for i in range(n):
        a, b = p[i], p[(i + 1) % n]
        out.append("H" if abs(b[1] - a[1]) <= eps else
                   "V" if abs(b[0] - a[0]) <= eps else None)
    return out


def _kinds(p: np.ndarray, tol_deg: float, max_shift: float) -> list:
    """Classify each edge i -> i+1 as 'H', 'V' or None.

    The shift limit is what keeps a long, gently sloping curve edge (a swash,
    a swoosh) from being flattened: snapping it would move its ends by far
    more than a pixel or two, so it is left alone, while a wobbly letter stem
    a couple of pixels off vertical gets straightened.
    """
    n = len(p)
    out = []
    for i in range(n):
        a, b = p[i], p[(i + 1) % n]
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        ang = np.degrees(np.arctan2(dy, dx))
        if ang < tol_deg and dy <= 2 * max_shift:
            out.append("H")
        elif ang > 90 - tol_deg and dx <= 2 * max_shift:
            out.append("V")
        else:
            out.append(None)
    return out


def _seg_dist(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((pts - a) @ ab) / max(float(ab @ ab), 1e-12), 0, 1)
    return np.linalg.norm(pts - (a + t[:, None] * ab), axis=1)


def snap_guided(fine: np.ndarray, coarse: np.ndarray, tol_deg: float,
                max_shift: float, near: float) -> np.ndarray:
    """Snap the fine outline's vertices onto the coarse outline's axis edges.

    The decision is made on the coarse outline, where a tapered stroke is one
    long edge that drifts too far to pass as horizontal. Made on the fine
    outline instead, the same taper is many short pieces that each pass, and
    snapping them all turns the taper into stairs. The fine outline is what
    gets moved, so curves elsewhere keep their detail.
    """
    fine = np.asarray(fine, float).copy()
    coarse = np.asarray(coarse, float)
    k = _kinds(coarse, tol_deg, max_shift)
    n = len(coarse)
    for j in range(n):
        if k[j] is None:
            continue
        a, b = coarse[j], coarse[(j + 1) % n]
        on = _seg_dist(fine, a, b) <= near
        if k[j] == "H":
            fine[on, 1] = (a[1] + b[1]) / 2
        else:
            fine[on, 0] = (a[0] + b[0]) / 2
    return drop_collinear(fine)


def square_corners(p: np.ndarray, max_len: float) -> np.ndarray:
    """Turn the short bevels that blur leaves at corners into real corners.

    Works on an outline whose straight edges are already exactly axis-aligned
    (see snap_guided); only those count as H or V here, so a slanted taper is
    never mistaken for a step. A run of short non-axis edges (each at most
    max_len, together at most three times that) is replaced according to what bounds it:
      H ... V   -> a right-angle corner
      H ... H   -> a vertical end at the run's extreme (a pointed slit tip,
                   or a sloped step); V ... V likewise
    """
    pts = np.asarray(p, float)
    changed = True
    while changed and len(pts) > 4:
        changed = False
        n = len(pts)
        k = _exact_kinds(pts)
        if all(x is None for x in k) or all(x is not None for x in k):
            break
        for start in range(n):
            # A run starts at an edge that is None and follows an axis edge.
            if k[start] is not None or k[start - 1] is None:
                continue
            end = start
            while k[end % n] is None:
                end += 1
            run = [(start + j) % n for j in range(end - start)]
            lengths = [np.hypot(*(pts[(j + 1) % n] - pts[j])) for j in run]
            if max(lengths) > max_len or sum(lengths) > 3 * max_len:
                continue
            before, after = k[start - 1], k[end % n]
            first, last = start % n, end % n           # vertices bounding the run
            inner = [(start + j) % n for j in range(1, end - start)]
            if {before, after} == {"H", "V"}:
                h_pt, v_pt = (pts[first], pts[last]) if before == "H" else (pts[last], pts[first])
                corner = np.array([v_pt[0], h_pt[1]])
                pts[first] = corner
                pts[last] = corner
            elif before == after == "H":
                seg = pts[[first, last] + inner]
                d = pts[(first - 1) % n][0] - pts[first][0]   # which way the run points
                x = seg[:, 0].min() if d > 0 else seg[:, 0].max()
                pts[first][0] = x
                pts[last][0] = x
            else:                                           # V ... V
                seg = pts[[first, last] + inner]
                d = pts[(first - 1) % n][1] - pts[first][1]
                y = seg[:, 1].min() if d > 0 else seg[:, 1].max()
                pts[first][1] = y
                pts[last][1] = y
            pts = np.delete(pts, inner, axis=0) if inner else pts
            pts = drop_collinear(pts)
            changed = True
            break
    return drop_collinear(pts)


def drop_collinear(p: np.ndarray, tol: float = 1e-6) -> np.ndarray:
    """Remove vertices sitting in the middle of a straight edge."""
    p = np.asarray(p, float)
    changed = True
    while changed and len(p) > 3:
        changed = False
        for i in range(len(p)):
            a, b, c = p[i - 1], p[i], p[(i + 1) % len(p)]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cross) <= tol * max(1.0, np.hypot(*(c - a))) or np.allclose(a, b):
                p = np.delete(p, i, axis=0)
                changed = True
                break
    return p


def is_boxy(p: np.ndarray, fill: float = 0.8) -> bool:
    """True when a contour nearly fills its bounding box."""
    import cv2
    x, y, w, h = cv2.boundingRect(np.asarray(p, np.float32))
    if w * h == 0:
        return False
    return cv2.contourArea(np.asarray(p, np.float32)) / (w * h) >= fill


def box(p: np.ndarray) -> np.ndarray:
    x0, y0 = np.min(p, axis=0)
    x1, y1 = np.max(p, axis=0)
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], float)


def _cluster(vals: np.ndarray, tol: float) -> np.ndarray:
    """Pull nearly-equal values onto their shared mean. A group never spans
    more than tol, so dense values cannot chain into one giant group."""
    if not len(vals):
        return vals
    order = np.argsort(vals)
    out = vals.copy()
    grp = [order[0]]
    for b in order[1:]:
        if vals[b] - vals[grp[0]] <= tol:
            grp.append(b)
        else:
            out[grp] = vals[grp].mean()
            grp = [b]
    out[grp] = vals[grp].mean()
    return out


def share_lines(polys: list, tol: float) -> list:
    """Line up edges across shapes: one cap line, one baseline, one slit line.

    Only vertices on an exactly axis-aligned edge are moved, and only along
    that axis, so slanted and curved outlines are untouched.
    """
    polys = [np.asarray(p, float).copy() for p in polys]
    for axis, kind in ((1, "H"), (0, "V")):
        refs = []
        for pi, p in enumerate(polys):
            k = _exact_kinds(p)
            n = len(p)
            for j in range(n):
                if k[j - 1] == kind or k[j] == kind:
                    refs.append((pi, j))
        if not refs:
            continue
        vals = np.array([polys[pi][j, axis] for pi, j in refs])
        new = _cluster(vals, tol)
        for (pi, j), v in zip(refs, new):
            polys[pi][j, axis] = v
    return [drop_collinear(p) for p in polys]
