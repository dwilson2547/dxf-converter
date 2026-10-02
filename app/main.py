"""Web front end for the scan -> DXF converter.

Upload a scan, look at what came out, fix it by hand, download the DXF. The
hand-fixing is the point: no set of thresholds gets every scan right, and
deleting a stray contour takes a second when you can see it.

Anonymous uploads live in a temp directory keyed by upload id and don't
survive a restart. Logged-in users can save scans (accounts.py, store/):
records in Postgres, images in object storage.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dxfconv import __version__                              # noqa: E402
from app import accounts, admin_api, scans                   # noqa: E402
from app.editing import (ALLOWED, MAX_BYTES, WORKDIR, ExportRequest,  # noqa: E402
                         Settings, convert_file, upload_path as _scan_path,
                         write_export)


STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="dxf-converter", version=__version__)
app.include_router(accounts.router)
app.include_router(admin_api.router)
app.include_router(scans.router)


@app.get("/api/version")
def version():
    return {"version": __version__}


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

    return {"id": upload_id, "name": file.filename,
            "suggested_source": _suggest_source(data)}


def _suggest_source(data: bytes) -> str:
    """A pen scan is near-grey; a photo of a printed badge is not."""
    import cv2
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        try:                                   # webp/tiff cv2 can't read
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                img = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
        except Exception:                      # noqa: BLE001
            return "scan"
    h, w = img.shape[:2]
    k = 400 / max(h, w)
    if k < 1:
        img = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
    sat = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 1]
    return "photo" if (sat > 60).mean() > 0.15 else "scan"


@app.get("/api/scan/{upload_id}")
def scan(upload_id: str):
    return FileResponse(_scan_path(upload_id))


@app.post("/api/convert/{upload_id}")
def convert_upload(upload_id: str, settings: Settings):
    return JSONResponse(convert_file(_scan_path(upload_id), settings))


@app.post("/api/export/{upload_id}")
def export(upload_id: str, req: ExportRequest):
    _scan_path(upload_id)          # validates the id
    out, name = write_export(req, os.path.join(WORKDIR, upload_id))
    return FileResponse(out, media_type="application/dxf", filename=name)


@app.delete("/api/upload/{upload_id}")
def discard(upload_id: str):
    folder = os.path.dirname(_scan_path(upload_id))
    shutil.rmtree(folder, ignore_errors=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
