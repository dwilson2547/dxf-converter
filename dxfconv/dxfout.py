"""Polylines -> DXF.

The file contains geometry entities and nothing else: no raster underlay, no
frame, no stray points. That is the whole reason Onshape's import behaves.
"""

from __future__ import annotations

import numpy as np
import ezdxf
from ezdxf import units


def _layer(doc, name: str, color: str | None):
    if name not in doc.layers:
        layer = doc.layers.add(name, color=7)
        if color:
            h = color.lstrip("#")
            layer.rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return name


def write_dxf(paths_mm, out_path: str, cfg, meta=None):
    """meta, when given, is one {layer, color, kind} per path. kind "circle"
    is written as a true CIRCLE fitted to the path's points, so it survives
    the origin shift and scale that were applied to those points."""
    doc = ezdxf.new("R2010", setup=True)
    doc.units = units.MM
    doc.header["$INSUNITS"] = 4       # millimetres
    doc.header["$MEASUREMENT"] = 1    # metric
    doc.header["$LUNITS"] = 2

    msp = doc.modelspace()

    for n, (pts, closed) in enumerate(paths_mm):
        m = meta[n] if meta else {}
        attribs = {"layer": _layer(doc, m.get("layer") or cfg.layer, m.get("color"))}
        pts = np.asarray(pts, dtype=float)
        if len(pts) < 2:
            continue
        if m.get("kind") == "circle" and len(pts) >= 3:
            c = pts.mean(axis=0)
            r = float(np.linalg.norm(pts - c, axis=1).mean())
            msp.add_circle((float(c[0]), float(c[1])), r, dxfattribs=attribs)
            continue
        if cfg.entity == "spline" and len(pts) >= 4:
            msp.add_spline(fit_points=[(float(x), float(y)) for x, y in pts],
                           dxfattribs=attribs)
        else:
            msp.add_lwpolyline([(float(x), float(y)) for x, y in pts],
                               format="xy",
                               close=bool(closed),
                               dxfattribs=attribs)

    if paths_mm:
        allpts = np.vstack([p for p, _ in paths_mm])
        lo, hi = allpts.min(axis=0), allpts.max(axis=0)
        doc.header["$EXTMIN"] = (float(lo[0]), float(lo[1]), 0.0)
        doc.header["$EXTMAX"] = (float(hi[0]), float(hi[1]), 0.0)

    doc.saveas(out_path)
    return out_path


def extents(paths_mm):
    if not paths_mm:
        return None
    allpts = np.vstack([p for p, _ in paths_mm])
    lo, hi = allpts.min(axis=0), allpts.max(axis=0)
    return {"min": lo.tolist(), "max": hi.tolist(),
            "width_mm": float(hi[0] - lo[0]),
            "height_mm": float(hi[1] - lo[1])}
