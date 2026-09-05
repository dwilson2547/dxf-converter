"""Web front end for the scan -> DXF converter.

Upload a scan, look at what came out, fix it by hand, download the DXF. The
hand-fixing is the point: no set of thresholds gets every scan right, and
deleting a stray contour takes a second when you can see it.

State lives in a temp directory keyed by upload id. Nothing persists across a
restart, which is fine for a PoC.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dxfconv import Config                                   # noqa: E402
from dxfconv.pipeline import extract, apply_origin           # noqa: E402
from dxfconv import dxfout                                   # noqa: E402


STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WORKDIR = os.path.join(tempfile.gettempdir(), "dxfconv-uploads")
ALLOWED = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
MAX_BYTES = 60 * 1024 * 1024

app = FastAPI(title="dxf-converter")


class Settings(BaseModel):
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


class ExportRequest(BaseModel):
    paths: list[Path]
    origin: str = "bbox"
    entity: str = "lwpolyline"
    layer: str = "PROFILE"
    scale: float = 1.0
    filename: str = "profile"


def _scan_path(upload_id: str) -> str:
    if not upload_id or not upload_id.isalnum():
        raise HTTPException(400, "bad id")
    folder = os.path.join(WORKDIR, upload_id)
    if not os.path.isdir(folder):
        raise HTTPException(404, "upload not found — re-upload the scan")
    for name in os.listdir(folder):
        if name.startswith("scan"):
            return os.path.join(folder, name)
    raise HTTPException(404, "scan missing")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"unsupported file type {ext or '?'}")

    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "file too large (60 MB max)")

    upload_id = uuid.uuid4().hex
    folder = os.path.join(WORKDIR, upload_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "scan" + ext), "wb") as fh:
        fh.write(data)

    return {"id": upload_id, "name": file.filename}


@app.get("/api/scan/{upload_id}")
def scan(upload_id: str):
    return FileResponse(_scan_path(upload_id))


@app.post("/api/convert/{upload_id}")
def convert_upload(upload_id: str, settings: Settings):
    src = _scan_path(upload_id)
    try:
        paths_mm, _, _, page, report = extract(src, settings.to_config())
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(422, f"conversion failed: {exc}") from exc

    rejects = [r for r in report["preprocess"].get("rejects", [])]
    px_per_mm = report["preprocess"]["px_per_mm"] / settings.scale

    return JSONResponse({
        "page": page,
        "paths": [{"points": np.asarray(p).round(4).tolist(), "closed": bool(c)}
                  for p, c in paths_mm],
        "report": {
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
    })


@app.post("/api/export/{upload_id}")
def export(upload_id: str, req: ExportRequest):
    _scan_path(upload_id)          # validates the id

    paths = [(np.asarray(p.points, dtype=float) * req.scale, p.closed)
             for p in req.paths if len(p.points) >= 2]
    if not paths:
        raise HTTPException(400, "nothing to export")

    paths = apply_origin(paths, req.origin)

    cfg = Config(entity=req.entity, layer=req.layer, origin=req.origin)
    out = os.path.join(WORKDIR, upload_id, "out.dxf")
    dxfout.write_dxf(paths, out, cfg)

    safe = "".join(c for c in req.filename if c.isalnum() or c in "-_") or "profile"
    return FileResponse(out, media_type="application/dxf",
                        filename=f"{safe}.dxf")


@app.delete("/api/upload/{upload_id}")
def discard(upload_id: str):
    folder = os.path.dirname(_scan_path(upload_id))
    shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
