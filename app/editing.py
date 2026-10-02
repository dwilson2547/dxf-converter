"""What the anonymous routes and the saved-scan routes share: the request
models, running a conversion, and writing an export. One implementation, so a
saved scan converts and exports exactly as an anonymous upload does.
"""

from __future__ import annotations

import os
import tempfile
import threading

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel

from dxfconv import Config, dxfout
from dxfconv.pipeline import apply_origin, extract

WORKDIR = os.path.join(tempfile.gettempdir(), "dxfconv-uploads")
ALLOWED = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
MAX_BYTES = 60 * 1024 * 1024

# One conversion at a time. A photo-mode run on a phone-sized image peaks near
# 1 GB; the editor can fire a second request (eyedropper, Re-detect) before the
# first finishes, and two at once is what got the pod OOM-killed.
CONVERT_LOCK = threading.Lock()


class Settings(BaseModel):
    source: str = "scan"
    fit_mm: float | None = None
    colors: int = 4
    inks: list[str] = []
    circles: bool = True
    square: bool = False
    upsample: int = 0
    dpi: float | None = None
    scale: float = 1.0
    flatten_mm: float = 3.0
    threshold: str = "otsu"
    threshold_value: int = 200
    close_gaps_mm: float = 0.0
    border_margin_mm: float = 3.0
    min_area_mm2: float = 0.30
    min_length_mm: float = 6.0
    mode: str = "outline"
    prune_spur_mm: float = 1.5
    smooth_mm: float = 0.6
    simplify_mm: float = 0.05

    def to_config(self) -> Config:
        return Config(**self.model_dump())


class Path(BaseModel):
    points: list[list[float]]
    closed: bool = False
    layer: str | None = None
    color: str | None = None
    kind: str = "poly"          # "circle" is written as a true CIRCLE


class ExportRequest(BaseModel):
    paths: list[Path]
    origin: str = "bbox"
    entity: str = "lwpolyline"
    layer: str = "PROFILE"
    scale: float = 1.0
    filename: str = "profile"


def upload_path(upload_id: str) -> str:
    """The local file of an anonymous upload."""
    if not upload_id or not upload_id.isalnum():
        raise HTTPException(400, "bad id")
    folder = os.path.join(WORKDIR, upload_id)
    if not os.path.isdir(folder):
        raise HTTPException(404, "upload not found — re-upload the scan")
    for name in os.listdir(folder):
        if name.startswith("scan"):
            return os.path.join(folder, name)
    raise HTTPException(404, "scan missing")


def convert_file(src: str, settings: Settings) -> dict:
    """Run the pipeline on a local image and shape the editor's response."""
    try:
        with CONVERT_LOCK:
            paths_mm, _, _, page, report = extract(src, settings.to_config())
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(422, f"conversion failed: {exc}") from exc

    rejects = [r for r in report["preprocess"].get("rejects", [])]
    px_per_mm = report["preprocess"]["px_per_mm"] / settings.scale

    pre, meta = report["preprocess"], report["meta"]
    return {
        "page": page,
        "paths": [{"points": np.asarray(p).round(4).tolist(), "closed": bool(c),
                   "layer": m["layer"], "color": m["color"], "kind": m["kind"]}
                  for (p, c), m in zip(paths_mm, meta)],
        "report": {
            "source": report["source"],
            "layers": pre.get("layers"),
            "palette": pre.get("palette"),
            "dropped_halo": pre.get("dropped_halo", 0),
            "dropped_contrast": pre.get("dropped_contrast", 0),
            "circles": report["vectorize"].get("circles", 0),
            "dpi": report["dpi"],
            "dpi_source": report["dpi_source"],
            "kept": report["preprocess"]["kept"],
            "dropped_border": report["preprocess"]["dropped_border"],
            "dropped_small": report["preprocess"]["dropped_small"],
            "dropped_short": report["vectorize"]["dropped_short"],
            "dropped_spur": report["vectorize"]["dropped_spur"],
            "paths": len(paths_mm),
            "vertices": report["vectorize"]["vertices"],
        },
        # Boxes for the artifacts the filters threw out, in page millimetres,
        # so the editor can show what it removed rather than just claiming it.
        "rejects": [{
            "reason": r["reason"],
            "x_mm": r["bbox"][0] / px_per_mm,
            "y_mm": page["height_mm"] - (r["bbox"][1] + r["bbox"][3]) / px_per_mm,
            "w_mm": r["bbox"][2] / px_per_mm,
            "h_mm": r["bbox"][3] / px_per_mm,
        } for r in rejects],
    }


def write_export(req: ExportRequest, out_dir: str) -> tuple[str, str]:
    """Write the DXF for an export request. Returns (path, download name)."""
    keep = [p for p in req.paths if len(p.points) >= 2]
    paths = [(np.asarray(p.points, dtype=float) * req.scale, p.closed) for p in keep]
    if not paths:
        raise HTTPException(400, "nothing to export")
    meta = [{"layer": p.layer or req.layer, "color": p.color, "kind": p.kind}
            for p in keep]

    paths = apply_origin(paths, req.origin)

    cfg = Config(entity=req.entity, layer=req.layer, origin=req.origin)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "out.dxf")
    dxfout.write_dxf(paths, out, cfg, meta)

    safe = "".join(c for c in req.filename if c.isalnum() or c in "-_") or "profile"
    return out, f"{safe}.dxf"
