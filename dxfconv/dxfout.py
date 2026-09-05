"""Polylines -> DXF.

The file contains geometry entities and nothing else: no raster underlay, no
frame, no stray points. That is the whole reason Onshape's import behaves.
"""

from __future__ import annotations

import numpy as np
import ezdxf
from ezdxf import units


def write_dxf(paths_mm, out_path: str, cfg):
    doc = ezdxf.new("R2010", setup=True)
    doc.units = units.MM
    doc.header["$INSUNITS"] = 4       # millimetres
    doc.header["$MEASUREMENT"] = 1    # metric
    doc.header["$LUNITS"] = 2

    if cfg.layer not in doc.layers:
        doc.layers.add(cfg.layer, color=7)

    msp = doc.modelspace()
    attribs = {"layer": cfg.layer}

    for pts, closed in paths_mm:
        if len(pts) < 2:
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
