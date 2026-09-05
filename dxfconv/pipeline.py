"""Orchestration: one call from scan to DXF."""

from __future__ import annotations

import os

import numpy as np

from .config import Config
from . import preprocess, vectorize, dxfout, preview


def extract(image_path: str, cfg: Config | None = None):
    """Scan -> geometry, without committing to a file.

    Paths come back in millimetres with the page origin kept, so they still
    line up with the scan image. That is what the editor needs; moving the
    geometry to its bounding box is deferred to export.
    """
    cfg = cfg or Config()

    gray, mask, dpi, pre_report = preprocess.build_mask(image_path, cfg)
    px_per_mm = cfg.px_per_mm(dpi)

    paths_px, vec_report = vectorize.build_paths(mask, cfg, px_per_mm)
    paths_mm = vectorize.to_mm(paths_px, gray.shape[0], px_per_mm,
                               origin="page", scale=cfg.scale)

    page = {
        "width_mm": gray.shape[1] / px_per_mm * cfg.scale,
        "height_mm": gray.shape[0] / px_per_mm * cfg.scale,
        "dpi": dpi,
        "dpi_source": pre_report["dpi_source"],
    }
    report = {
        "dpi": dpi,
        "dpi_source": pre_report["dpi_source"],
        "preprocess": pre_report,
        "vectorize": vec_report,
    }
    return paths_mm, paths_px, gray, page, report


def apply_origin(paths_mm, origin: str):
    """Move geometry to its bounding box corner, if asked."""
    if origin != "bbox" or not paths_mm:
        return paths_mm
    shift = np.vstack([np.asarray(p) for p, _ in paths_mm]).min(axis=0)
    return [(np.asarray(p) - shift, c) for p, c in paths_mm]


def convert(image_path: str, out_dxf: str, cfg: Config | None = None,
            preview_path: str | None = None) -> dict:
    cfg = cfg or Config()

    paths_mm, paths_px, gray, page, report = extract(image_path, cfg)
    paths_mm = apply_origin(paths_mm, cfg.origin)

    os.makedirs(os.path.dirname(os.path.abspath(out_dxf)), exist_ok=True)
    dxfout.write_dxf(paths_mm, out_dxf, cfg)

    result = dict(report)
    result.update({
        "input": image_path,
        "dxf": out_dxf,
        "page": page,
        "extents_mm": dxfout.extents(paths_mm),
        "paths": len(paths_mm),
        "vertices": report["vectorize"]["vertices"],
    })

    if preview_path:
        preview.render(gray, paths_px, report["preprocess"], preview_path)
        result["preview"] = preview_path

    return result
