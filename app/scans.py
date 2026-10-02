"""Saved scans and their versions.

A scan is an image a user saved; a version is an immutable save of the
editor's state for it (detection settings + edited paths). Saving again makes
the next version — nothing is overwritten.

Images live in object storage and reach the browser only through these
routes, after the ownership check. For converting, a saved image is cached in
the local work dir and re-fetched from the bucket after a pod restart.
Another user's scan is a 404, never a 403: its existence isn't disclosed.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .accounts import Store, current_user, get_store
from .editing import (WORKDIR, ExportRequest, Path, Settings, convert_file,
                      upload_path, write_export)
from .store import ObjectNotFound, ObjectStore
from .store.models import Scan, User, Version, utcnow

router = APIRouter(prefix="/api/scans", tags=["scans"])

MAX_PATHS = 20_000
MAX_POINTS = 500_000          # per version; the Seeburg badge is ~1,300
THUMB_PX = 400
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp",
                 ".webp": "image/webp"}


# --- request models -----------------------------------------------------------

class VersionIn(BaseModel):
    settings: Settings
    paths: list[Path]
    label: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=5000)
    parent_number: int | None = None
    view: dict = Field(default_factory=dict)
    """Editor state that isn't detection settings, e.g. hidden layers."""


class ScanIn(VersionIn):
    upload_id: str
    name: str = Field(..., min_length=1, max_length=200)


class ScanPatch(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class VersionPatch(BaseModel):
    label: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=5000)


class ConvertIn(BaseModel):
    settings: Settings


# --- helpers ------------------------------------------------------------------

def _objects(store: Store) -> ObjectStore:
    if store.objects is None:
        raise HTTPException(503, "image storage is not configured on this server")
    return store.objects


def _owned(s, scan_id: str, user: User, lock: bool = False) -> Scan:
    q = select(Scan).where(Scan.id == scan_id, Scan.user_id == user.id)
    if lock:
        q = q.with_for_update()
    scan = s.scalar(q)
    if scan is None:
        raise HTTPException(404, "no such scan")
    return scan


def _version(s, scan: Scan, number: int) -> Version:
    v = s.scalar(select(Version).where(Version.scan_id == scan.id, Version.number == number))
    if v is None:
        raise HTTPException(404, "no such version")
    return v


def compute_stats(paths: list[Path]) -> dict:
    pts = [q for p in paths for q in p.points]
    layers: dict[str, int] = {}
    for p in paths:
        layers[p.layer or "PROFILE"] = layers.get(p.layer or "PROFILE", 0) + 1
    ext = None
    if pts:
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        ext = {"width_mm": round(max(xs) - min(xs), 4), "height_mm": round(max(ys) - min(ys), 4)}
    return {"paths": len(paths), "vertices": len(pts),
            "circles": sum(p.kind == "circle" for p in paths),
            "extents": ext, "layers": layers}


def _check_size(paths: list[Path]) -> None:
    if len(paths) > MAX_PATHS:
        raise HTTPException(413, f"too many paths ({len(paths)} > {MAX_PATHS})")
    n = sum(len(p.points) for p in paths)
    if n > MAX_POINTS:
        raise HTTPException(413, f"too many points ({n} > {MAX_POINTS})")


def _new_version(s, scan: Scan, body: VersionIn) -> Version:
    """Issue the next number. Callers hold the scan row lock, so two saves
    racing on the same scan get consecutive numbers rather than a clash."""
    _check_size(body.paths)
    scan.last_version += 1
    scan.updated_at = utcnow()
    settings = body.settings.model_dump()
    if body.view:
        settings["_view"] = body.view
    v = Version(scan_id=scan.id, number=scan.last_version, label=body.label, note=body.note,
                parent_number=body.parent_number, settings=settings,
                paths=[p.model_dump() for p in body.paths], stats=compute_stats(body.paths))
    s.add(v)
    return v


def _version_summary(v: Version) -> dict:
    return {"number": v.number, "label": v.label, "note": v.note,
            "parent_number": v.parent_number, "stats": v.stats,
            "created_at": v.created_at.isoformat()}


def _scan_summary(scan: Scan, versions: int, latest: dict | None) -> dict:
    return {"id": scan.id, "name": scan.name, "width_px": scan.width_px,
            "height_px": scan.height_px, "content_type": scan.content_type,
            "created_at": scan.created_at.isoformat(),
            "updated_at": scan.updated_at.isoformat(),
            "versions": versions, "latest": latest,
            "image_url": f"/api/scans/{scan.id}/image",
            "thumb_url": f"/api/scans/{scan.id}/thumb" if scan.thumb_key else None}


def _thumbnail(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((THUMB_PX, THUMB_PX))
        buf = io.BytesIO()
        im.save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def cached_image(store: Store, scan: Scan) -> str:
    """Local copy of a saved scan's image, fetched from the bucket if the
    cache doesn't have it (first use, or after a pod restart)."""
    ext = os.path.splitext(scan.image_key)[1]
    folder = os.path.join(WORKDIR, "saved", scan.id)
    path = os.path.join(folder, "original" + ext)
    if os.path.exists(path):
        return path
    try:
        data = _objects(store).get(scan.image_key)
    except ObjectNotFound:
        raise HTTPException(410, "this scan's image is missing from storage") from None
    if hashlib.sha256(data).hexdigest() != scan.image_sha256:
        raise HTTPException(502, "stored image doesn't match its checksum")
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder)       # write-then-rename: no half files
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path


# --- scans --------------------------------------------------------------------

@router.post("")
def save_scan(body: ScanIn, user: User = Depends(current_user),
              store: Store = Depends(get_store)):
    """Save an anonymous upload as a new scan, with the editor state as v1."""
    objects = _objects(store)
    _check_size(body.paths)
    src = upload_path(body.upload_id)
    ext = os.path.splitext(src)[1].lower()
    with open(src, "rb") as fh:
        data = fh.read()
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        thumb = _thumbnail(data)
    except Exception as exc:                      # noqa: BLE001
        raise HTTPException(422, f"can't read the image: {exc}") from exc

    with store.db.session() as s:
        scan = Scan(user_id=user.id, name=body.name.strip(), image_key="",
                    image_sha256=hashlib.sha256(data).hexdigest(),
                    content_type=CONTENT_TYPES.get(ext, "application/octet-stream"),
                    width_px=width, height_px=height)
        s.add(scan)
        s.flush()                                 # assigns scan.id for the keys
        scan.image_key = ObjectStore.image_key(user.id, scan.id, ext)
        scan.thumb_key = ObjectStore.thumb_key(user.id, scan.id)
        # Objects before commit: if the upload fails the row is rolled back
        # and nothing points at a missing image.
        objects.put(scan.image_key, data, scan.content_type)
        objects.put(scan.thumb_key, thumb, "image/webp")
        v = _new_version(s, scan, body)
        s.flush()
        return {"scan": _scan_summary(scan, 1, _version_summary(v)),
                "version": _version_summary(v)}


@router.get("")
def list_scans(user: User = Depends(current_user), store: Store = Depends(get_store)):
    with store.db.session() as s:
        rows = s.execute(
            select(Scan, func.count(Version.id))
            .outerjoin(Version, Version.scan_id == Scan.id)
            .where(Scan.user_id == user.id)
            .group_by(Scan.id).order_by(Scan.updated_at.desc())).all()
        latest = {}
        if rows:
            newest = (select(Version.scan_id, func.max(Version.number).label("n"))
                      .where(Version.scan_id.in_([sc.id for sc, _ in rows]))
                      .group_by(Version.scan_id).subquery())
            # Columns only: the list shows stats, it doesn't need the geometry.
            for scan_id, number, label, stats, created in s.execute(
                    select(Version.scan_id, Version.number, Version.label, Version.stats,
                           Version.created_at)
                    .join(newest, (Version.scan_id == newest.c.scan_id)
                          & (Version.number == newest.c.n))):
                latest[scan_id] = {"number": number, "label": label, "stats": stats,
                                   "created_at": created.isoformat()}
        return {"scans": [_scan_summary(sc, n, latest.get(sc.id)) for sc, n in rows]}


@router.get("/{scan_id}")
def get_scan(scan_id: str, user: User = Depends(current_user),
             store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user)
        versions = s.scalars(select(Version).where(Version.scan_id == scan.id)
                             .order_by(Version.number)).all()
        latest = _version_summary(versions[-1]) if versions else None
        out = _scan_summary(scan, len(versions), latest)
        out["version_list"] = [_version_summary(v) for v in versions]
        return out


@router.patch("/{scan_id}")
def rename_scan(scan_id: str, body: ScanPatch, user: User = Depends(current_user),
                store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user)
        scan.name = body.name.strip()
        scan.updated_at = utcnow()
        return {"id": scan.id, "name": scan.name}


@router.delete("/{scan_id}")
def delete_scan(scan_id: str, user: User = Depends(current_user),
                store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user)
        if store.objects:                         # objects first, then the row
            store.objects.delete_prefix(ObjectStore.scan_prefix(user.id, scan.id))
        s.delete(scan)
    shutil.rmtree(os.path.join(WORKDIR, "saved", scan_id), ignore_errors=True)
    return {"ok": True}


def _stream(store: Store, key: str | None, fallback_type: str):
    if not key:
        raise HTTPException(404, "no such image")
    try:
        chunks, ctype, length = _objects(store).stream(key)
    except ObjectNotFound:
        raise HTTPException(410, "this image is missing from storage") from None
    headers = {"Cache-Control": "private, max-age=86400"}   # a scan's image never changes
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(chunks, media_type=ctype or fallback_type, headers=headers)


@router.get("/{scan_id}/image")
def scan_image(scan_id: str, user: User = Depends(current_user),
               store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user)
        key, ctype = scan.image_key, scan.content_type
    return _stream(store, key, ctype)


@router.get("/{scan_id}/thumb")
def scan_thumb(scan_id: str, user: User = Depends(current_user),
               store: Store = Depends(get_store)):
    with store.db.session() as s:
        key = _owned(s, scan_id, user).thumb_key
    return _stream(store, key, "image/webp")


@router.post("/{scan_id}/convert")
def convert_saved(scan_id: str, body: ConvertIn, user: User = Depends(current_user),
                  store: Store = Depends(get_store)):
    """Re-detect on a saved scan's image, the same as /api/convert does for
    an anonymous upload. Nothing is saved until a version is posted."""
    with store.db.session() as s:
        scan = _owned(s, scan_id, user)
        s.expunge(scan)
    return convert_file(cached_image(store, scan), body.settings)


@router.post("/{scan_id}/export")
def export_saved(scan_id: str, req: ExportRequest, user: User = Depends(current_user),
                 store: Store = Depends(get_store)):
    with store.db.session() as s:
        _owned(s, scan_id, user)
    out_dir = tempfile.mkdtemp(prefix="export-", dir=_ensure(WORKDIR))
    out, name = write_export(req, out_dir)
    return FileResponse(out, media_type="application/dxf", filename=name)


def _ensure(d: str) -> str:
    os.makedirs(d, exist_ok=True)
    return d


# --- versions -----------------------------------------------------------------

@router.post("/{scan_id}/versions")
def save_version(scan_id: str, body: VersionIn, user: User = Depends(current_user),
                 store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user, lock=True)
        if body.parent_number is not None and body.parent_number > scan.last_version:
            raise HTTPException(422, "parent_number is not a version of this scan")
        v = _new_version(s, scan, body)
        s.flush()
        return _version_summary(v)


@router.get("/{scan_id}/versions/{number}")
def get_version(scan_id: str, number: int, user: User = Depends(current_user),
                store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user)
        v = _version(s, scan, number)
        settings = dict(v.settings)
        view = settings.pop("_view", {})
        out = _version_summary(v)
        out.update({"settings": settings, "view": view, "paths": v.paths})
        return out


@router.patch("/{scan_id}/versions/{number}")
def relabel_version(scan_id: str, number: int, body: VersionPatch,
                    user: User = Depends(current_user), store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user)
        v = _version(s, scan, number)
        for field in body.model_fields_set:
            setattr(v, field, getattr(body, field))
        return _version_summary(v)


@router.delete("/{scan_id}/versions/{number}")
def delete_version(scan_id: str, number: int, user: User = Depends(current_user),
                   store: Store = Depends(get_store)):
    with store.db.session() as s:
        scan = _owned(s, scan_id, user, lock=True)
        v = _version(s, scan, number)
        count = s.scalar(select(func.count(Version.id)).where(Version.scan_id == scan.id))
        if count <= 1:
            err = "that's the only version — delete the scan instead"
        else:
            err = None
            s.delete(v)
            scan.updated_at = utcnow()
    if err:
        raise HTTPException(409, err)
    return {"ok": True}
