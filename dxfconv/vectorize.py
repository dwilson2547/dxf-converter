"""Binary mask -> ordered polylines.

Centreline mode is the important one. A traced outline is a pen stroke roughly
0.5 mm wide, but the geometry you actually want is the line *down the middle*
of it. Contour-based converters (potrace and friends) return the boundary of
the black region instead, so every stroke comes back as two parallel curves
that you then have to reconcile in CAD. Skeletonising first avoids that.
"""

from __future__ import annotations

import numpy as np
import cv2
from skimage.morphology import skeletonize
from shapely.geometry import LineString

# 8-connected neighbourhood
_NB = [(-1, -1), (-1, 0), (-1, 1),
       (0, -1),           (0, 1),
       (1, -1),  (1, 0),  (1, 1)]


def _degree_map(skel: np.ndarray) -> np.ndarray:
    """Neighbour count for every skeleton pixel."""
    k = np.ones((3, 3), np.uint8)
    counts = cv2.filter2D(skel.astype(np.uint8), -1, k, borderType=cv2.BORDER_CONSTANT)
    return (counts - skel.astype(np.uint8)) * skel


def _trace_component(pixels: set, junction_lookup: dict) -> list:
    """Walk one junction-free pixel run into an ordered segment.

    Returns a list of dicts {pts, nodes, closed} — normally one, but a pixel
    run can contain more than one strand if it happens to be disconnected
    after the junctions were carved out. `nodes` holds the junction id each
    end attaches to, or None for a free end.
    """
    remaining = set(pixels)
    out = []

    def nbrs(p, pool):
        r, c = p
        return [(r + dr, c + dc) for dr, dc in _NB if (r + dr, c + dc) in pool]

    while remaining:
        # Prefer starting from a free end so the walk is a straight shot.
        start = None
        for p in remaining:
            if len(nbrs(p, remaining)) <= 1:
                start = p
                break
        closed = start is None
        if closed:
            start = next(iter(remaining))

        chain = [start]
        remaining.discard(start)
        cur = start
        while True:
            nxt = nbrs(cur, remaining)
            if not nxt:
                break
            # Prefer orthogonal steps; keeps the chain from cutting corners.
            nxt.sort(key=lambda q: abs(q[0] - cur[0]) + abs(q[1] - cur[1]))
            cur = nxt[0]
            chain.append(cur)
            remaining.discard(cur)

        if len(chain) < 2:
            continue

        # Re-attach to junctions so crossings stay connected in the output.
        pts = [(float(r), float(c)) for r, c in chain]
        head = junction_lookup.get(chain[0])
        tail = junction_lookup.get(chain[-1])
        n0 = n1 = None
        if head is not None:
            n0, pt = head
            pts.insert(0, pt)
        if tail is not None:
            n1, pt = tail
            pts.append(pt)

        out.append({"pts": np.array(pts, dtype=float),
                    "nodes": [n0, n1],
                    "closed": closed})

    return out


def skeleton_polylines(mask: np.ndarray) -> list:
    """Centreline trace. Returns segment dicts {pts, nodes, closed}."""
    skel = skeletonize(mask > 0)
    deg = _degree_map(skel)

    junction = skel & (deg >= 3)
    paths_mask = skel & ~junction

    # Every junction blob collapses to a single point that all incoming
    # strands get stitched to.
    junction_lookup = {}
    if junction.any():
        _, jlabels, _, jcent = cv2.connectedComponentsWithStats(
            junction.astype(np.uint8), 8)
        jr, jc = np.nonzero(junction)
        jpix = set(zip(jr.tolist(), jc.tolist()))
        for r, c in zip(jr, jc):
            lab = int(jlabels[r, c])
            cx, cy = jcent[lab]          # centroid is (x, y) = (col, row)
            for dr, dc in _NB:
                q = (r + dr, c + dc)
                if q not in jpix:
                    junction_lookup[q] = (lab, (float(cy), float(cx)))

    n, labels = cv2.connectedComponents(paths_mask.astype(np.uint8), 8)
    result = []
    for lab in range(1, n):
        rs, cs = np.nonzero(labels == lab)
        result.extend(_trace_component(set(zip(rs.tolist(), cs.tolist())),
                                       junction_lookup))
    return result


# --- junction stitching ---------------------------------------------------

def _end_direction(pts: np.ndarray, at_start: bool, lookahead: int = 12):
    """Unit vector pointing away from the given end, along the segment."""
    if at_start:
        a, b = pts[0], pts[min(lookahead, len(pts) - 1)]
    else:
        a, b = pts[-1], pts[max(-lookahead - 1, -len(pts))]
    v = np.asarray(b, float) - np.asarray(a, float)
    norm = np.linalg.norm(v)
    return v / norm if norm else np.zeros(2)


def collapse_junction_clusters(segments: list, max_link: float) -> tuple:
    """Fuse junctions that sit a hair apart into one node.

    Skeletonising an X-crossing does not give a single 4-way junction. It
    gives two Y-junctions joined by a connector a few pixels long. Left alone
    that presents the stitcher with two 3-branch nodes, where the true
    opposite branch simply isn't present to pair with, so nothing joins up.
    Collapsing the pair into one 4-branch node is what makes crossings work.
    """
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Each junction's position, taken from any segment end that touches it.
    pos = {}
    for seg in segments:
        for side, idx in ((0, 0), (1, -1)):
            node = seg["nodes"][side]
            if node is not None:
                pos[node] = seg["pts"][idx]

    dropped = set()
    for i, seg in enumerate(segments):
        n0, n1 = seg["nodes"]
        if (n0 is not None and n1 is not None and not seg["closed"]
                and _polyline_length(seg["pts"]) < max_link):
            union(n0, n1)
            dropped.add(i)

    members: dict = {}
    for node in pos:
        members.setdefault(find(node), []).append(node)
    centroid = {root: np.mean([pos[n] for n in ns], axis=0)
                for root, ns in members.items()}

    out = []
    for i, seg in enumerate(segments):
        if i in dropped:
            continue
        pts = seg["pts"].copy()
        nodes = list(seg["nodes"])
        for side, idx in ((0, 0), (1, -1)):
            node = nodes[side]
            if node is not None:
                root = find(node)
                nodes[side] = root
                pts[idx] = centroid[root]      # snap the seam shut
        if (nodes[0] is not None and nodes[0] == nodes[1]
                and _polyline_length(pts) < max_link):
            continue                            # degenerate self-loop
        out.append({"pts": pts, "nodes": nodes, "closed": seg["closed"]})

    return out, len(dropped)


def stitch_segments(segments: list, straightness: float = 0.35) -> list:
    """Rejoin segments that meet at a junction and continue in line.

    Skeletonising cuts every stroke at each crossing. A traced outline that
    happens to touch another stroke therefore comes back in pieces. At each
    junction we pair up the branches whose tangents are most nearly collinear,
    so a curve that visually runs straight through a crossing comes out as one
    continuous path — and a loop that touches something still closes.

    `straightness` is how anti-parallel two branches must be to count as the
    same stroke; 0 would pair anything, 1 only a perfectly straight run.
    """
    # ends[node] -> list of (segment index, at_start)
    ends: dict = {}
    for i, seg in enumerate(segments):
        if seg["closed"]:
            continue
        for side, at_start in ((0, True), (1, False)):
            node = seg["nodes"][side]
            if node is not None:
                ends.setdefault(node, []).append((i, at_start))

    # partner[(seg, at_start)] -> (seg, at_start)
    partner: dict = {}
    for node, branches in ends.items():
        if len(branches) < 2:
            continue
        dirs = {b: _end_direction(segments[b[0]]["pts"], b[1]) for b in branches}

        scored = []
        for a in range(len(branches)):
            for b in range(a + 1, len(branches)):
                ba, bb = branches[a], branches[b]
                if ba[0] == bb[0]:
                    continue        # both ends of one segment: a loop, handle below
                # -1 means the two branches run straight through each other.
                scored.append((float(np.dot(dirs[ba], dirs[bb])), ba, bb))
        scored.sort(key=lambda s: s[0])

        used = set()
        for score, ba, bb in scored:
            if score > -straightness:
                break
            if ba in used or bb in used:
                continue
            partner[ba], partner[bb] = bb, ba
            used.update((ba, bb))

    # Follow the partner links into chains.
    merged, consumed = [], set()

    def oriented(idx, entry_at_start):
        """Segment points running away from the end we entered by."""
        pts = segments[idx]["pts"]
        return pts if entry_at_start else pts[::-1]

    def walk(idx, entry_at_start):
        """Consume a chain starting at this segment end. Returns (pts, closed)."""
        chain, cur, side = [], idx, entry_at_start
        closed = False
        while True:
            consumed.add(cur)
            chain.append(oriented(cur, side))
            nxt = partner.get((cur, not side))
            if nxt is None:
                break
            if nxt[0] in consumed:
                closed = nxt[0] == idx
                break
            cur, side = nxt[0], nxt[1]

        pts = np.vstack(chain)
        # Drop the duplicated junction points at the seams.
        keep = np.ones(len(pts), bool)
        keep[1:] = np.linalg.norm(np.diff(pts, axis=0), axis=1) > 1e-9
        pts = pts[keep]
        if closed and len(pts) > 2 and np.linalg.norm(pts[0] - pts[-1]) < 1e-6:
            pts = pts[:-1]
        return pts, closed

    # Segments that were already whole loops.
    for i, seg in enumerate(segments):
        if seg["closed"]:
            merged.append((seg["pts"], True))
            consumed.add(i)

    # Open chains: start from an end that has no partner.
    for i, seg in enumerate(segments):
        if i in consumed:
            continue
        for at_start in (True, False):
            if partner.get((i, at_start)) is None:
                merged.append(walk(i, at_start))
                break

    # Anything left is a ring of stitched segments; enter it anywhere.
    for i, seg in enumerate(segments):
        if i not in consumed:
            merged.append(walk(i, True))

    return merged


def contour_polylines(mask: np.ndarray) -> list:
    """Outline trace, for scans of filled/solid shapes rather than pen lines."""
    contours, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        pts = c.reshape(-1, 2)[:, ::-1].astype(float)   # (x,y) -> (row,col)
        if len(pts) >= 3:
            out.append((pts, True))
    return out


def prune_spurs(segments: list, max_len: float) -> tuple:
    """Drop short whiskers hanging off a junction.

    Skeletonising sprouts these wherever a stroke ends in a blob or two
    strokes cross at a shallow angle. They are short, and dangle from a
    junction at one end with nothing at the other. Removing them before
    stitching matters: a whisker at a crossing would otherwise compete for
    the tangent pairing and split a curve that should run straight through.
    """
    kept, dropped = [], 0
    for seg in segments:
        dangling = sum(n is not None for n in seg["nodes"]) == 1
        if (dangling and not seg["closed"]
                and _polyline_length(seg["pts"]) < max_len):
            dropped += 1
            continue
        kept.append(seg)
    return kept, dropped


# --- curve conditioning ---------------------------------------------------

def _polyline_length(pts: np.ndarray) -> float:
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


def resample(pts: np.ndarray, step: float) -> np.ndarray:
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    dist = np.concatenate([[0.0], np.cumsum(seg)])
    total = dist[-1]
    if total <= 0:
        return pts
    n = max(2, int(round(total / step)) + 1)
    t = np.linspace(0, total, n)
    return np.column_stack([np.interp(t, dist, pts[:, 0]),
                            np.interp(t, dist, pts[:, 1])])


def smooth(pts: np.ndarray, window: int, closed: bool) -> np.ndarray:
    if window < 3 or len(pts) < window:
        return pts
    if window % 2 == 0:
        window += 1
    kern = np.ones(window) / window
    pad = window // 2

    if closed:
        ext = np.vstack([pts[-pad:], pts, pts[:pad]])
    else:
        ext = np.vstack([np.repeat(pts[:1], pad, axis=0),
                         pts,
                         np.repeat(pts[-1:], pad, axis=0)])

    sm = np.column_stack([np.convolve(ext[:, 0], kern, mode="valid"),
                          np.convolve(ext[:, 1], kern, mode="valid")])
    if not closed:
        # Endpoints carry real information — a stroke ends where it ends.
        sm[0], sm[-1] = pts[0], pts[-1]
    return sm


def simplify(pts: np.ndarray, tol: float, closed: bool) -> np.ndarray:
    if tol <= 0 or len(pts) < 3:
        return pts
    line = LineString(pts)
    simplified = np.array(line.simplify(tol, preserve_topology=False).coords)
    if len(simplified) < 2:
        return pts
    return simplified


def build_paths(mask: np.ndarray, cfg, px_per_mm: float):
    """Mask -> conditioned polylines in pixel space, plus a report."""
    min_len_px = cfg.min_length_mm * px_per_mm
    spur_px = cfg.prune_spur_mm * px_per_mm

    report = {"dropped_short": 0, "dropped_spur": 0}

    if cfg.mode == "centerline":
        segments = skeleton_polylines(mask)
        report["segments"] = len(segments)

        segments, spurs = prune_spurs(segments, spur_px)
        segments, report["collapsed_junctions"] = collapse_junction_clusters(
            segments, spur_px)
        # Collapsing can leave a whisker newly dangling.
        segments, more_spurs = prune_spurs(segments, spur_px)
        report["dropped_spur"] = spurs + more_spurs

        raw = stitch_segments(segments)
        report["stitched"] = len(raw)
    else:
        raw = contour_polylines(mask)
        report["segments"] = report["stitched"] = len(raw)
        report["collapsed_junctions"] = 0

    paths = []
    for pts, closed in raw:
        # The real "lines only" rule: a speck has no length, a stroke does.
        if _polyline_length(pts) < min_len_px:
            report["dropped_short"] += 1
            continue

        pts = resample(pts, max(0.8, px_per_mm * 0.05))
        pts = smooth(pts, int(round(cfg.smooth_mm * px_per_mm)), closed)
        pts = simplify(pts, cfg.simplify_mm * px_per_mm, closed)
        paths.append((pts, closed))

    report["kept"] = len(paths)
    report["closed"] = int(sum(1 for _, c in paths if c))
    report["vertices"] = int(sum(len(p) for p, _ in paths))
    return paths, report


def to_mm(paths, height_px: int, px_per_mm: float, origin: str,
          scale: float = 1.0):
    """Pixel (row, col) -> CAD millimetres (x, y), Y flipped to CAD's up."""
    out = []
    for pts, closed in paths:
        xy = np.column_stack([pts[:, 1] / px_per_mm,
                              (height_px - 1 - pts[:, 0]) / px_per_mm])
        out.append((xy * scale, closed))

    if origin == "bbox" and out:
        allpts = np.vstack([p for p, _ in out])
        shift = allpts.min(axis=0)
        out = [(p - shift, c) for p, c in out]

    return out
