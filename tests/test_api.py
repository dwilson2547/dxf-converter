"""Tests for the web layer.

Covers the round trip the editor depends on: upload, detect, edit, export —
including that edits made in the browser are what actually reach the DXF.
"""

import io
import os
import sys

import numpy as np
import cv2
import ezdxf
import pytest
from PIL import Image
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app          # noqa: E402


DPI = 300.0
PX_PER_MM = DPI / 25.4


@pytest.fixture
def client():
    return TestClient(app)


def scan_bytes(draw=None, size_mm=(120, 120)):
    def mm(v):
        return int(round(v * PX_PER_MM))

    img = np.full((mm(size_mm[1]), mm(size_mm[0])), 255, np.uint8)
    if draw is None:
        cv2.circle(img, (mm(60), mm(60)), mm(30), 0, mm(0.5))
    else:
        draw(img, mm)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG", dpi=(DPI, DPI))
    return buf.getvalue()


def upload(client, data=None, name="scan.png"):
    res = client.post("/api/upload",
                      files={"file": (name, data or scan_bytes(), "image/png")})
    assert res.status_code == 200, res.text
    return res.json()["id"]


# --- upload ---------------------------------------------------------------

def test_upload_rejects_unsupported_type(client):
    res = client.post("/api/upload",
                      files={"file": ("notes.txt", b"hello", "text/plain")})
    assert res.status_code == 400


def test_upload_rejects_empty_file(client):
    res = client.post("/api/upload",
                      files={"file": ("scan.png", b"", "image/png")})
    assert res.status_code == 400


def test_unknown_id_is_a_clean_404(client):
    assert client.get("/api/scan/deadbeef").status_code == 404
    assert client.post("/api/convert/deadbeef", json={}).status_code == 404


def test_malformed_id_is_rejected(client):
    assert client.get("/api/scan/../../etc").status_code in (400, 404)


# --- convert --------------------------------------------------------------

def test_convert_returns_geometry_and_scale(client):
    uid = upload(client)
    res = client.post(f"/api/convert/{uid}", json={"mode": "outline"})
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["page"]["dpi"] == 300.0
    assert body["page"]["dpi_source"] == "image metadata"
    assert body["page"]["width_mm"] == pytest.approx(120, abs=1)
    assert len(body["paths"]) == 2          # both edges of the stroke
    assert body["report"]["vertices"] > 0
    for path in body["paths"]:
        assert len(path["points"]) >= 3
        assert all(len(pt) == 2 for pt in path["points"])


def test_convert_reports_rejected_artifacts_with_positions(client):
    """The editor draws these boxes, so they have to be real page coordinates."""
    def draw(img, mm):
        cv2.circle(img, (mm(60), mm(60)), mm(30), 0, mm(0.5))
        cv2.rectangle(img, (0, mm(10)), (mm(0.5), mm(90)), 0, -1)

    uid = upload(client, scan_bytes(draw))
    body = client.post(f"/api/convert/{uid}", json={}).json()

    assert body["report"]["dropped_border"] >= 1
    borders = [r for r in body["rejects"] if r["reason"] == "border"]
    assert borders
    for r in borders:
        assert 0 <= r["x_mm"] <= body["page"]["width_mm"]
        assert 0 <= r["y_mm"] <= body["page"]["height_mm"]
        assert r["w_mm"] > 0 and r["h_mm"] > 0


def test_scan_is_served_back(client):
    uid = upload(client)
    res = client.get(f"/api/scan/{uid}")
    assert res.status_code == 200
    assert res.content[:8] == b"\x89PNG\r\n\x1a\n"


# --- export ---------------------------------------------------------------

def test_export_honours_edits(client, tmp_path):
    """Deleting a path in the editor must delete it from the DXF."""
    uid = upload(client)
    paths = client.post(f"/api/convert/{uid}", json={}).json()["paths"]
    assert len(paths) == 2

    res = client.post(f"/api/export/{uid}",
                      json={"paths": paths[:1], "origin": "bbox",
                            "entity": "lwpolyline", "scale": 1.0,
                            "filename": "edited"})
    assert res.status_code == 200
    assert "edited.dxf" in res.headers.get("content-disposition", "")

    out = tmp_path / "e.dxf"
    out.write_bytes(res.content)
    msp = ezdxf.readfile(str(out)).modelspace()
    assert len(msp.query("LWPOLYLINE")) == 1
    assert len(msp.query("POINT")) == 0


def test_export_applies_scale_and_origin(client, tmp_path):
    uid = upload(client)
    paths = client.post(f"/api/convert/{uid}", json={}).json()["paths"]

    def extents(scale):
        res = client.post(f"/api/export/{uid}",
                          json={"paths": paths, "origin": "bbox",
                                "entity": "lwpolyline", "scale": scale,
                                "filename": "s"})
        out = tmp_path / f"s{scale}.dxf"
        out.write_bytes(res.content)
        msp = ezdxf.readfile(str(out)).modelspace()
        pts = np.vstack([np.array(e.get_points("xy"))
                         for e in msp.query("LWPOLYLINE")])
        return np.ptp(pts[:, 0]), pts.min(axis=0)

    full_w, full_min = extents(1.0)
    half_w, half_min = extents(0.5)

    assert half_w == pytest.approx(full_w * 0.5, abs=0.05)
    assert full_min == pytest.approx([0, 0], abs=1e-6)   # bbox origin
    assert half_min == pytest.approx([0, 0], abs=1e-6)


def test_export_declares_millimetres(client, tmp_path):
    uid = upload(client)
    paths = client.post(f"/api/convert/{uid}", json={}).json()["paths"]
    res = client.post(f"/api/export/{uid}",
                      json={"paths": paths, "filename": "u"})
    out = tmp_path / "u.dxf"
    out.write_bytes(res.content)
    assert ezdxf.readfile(str(out)).header["$INSUNITS"] == 4


def test_export_with_nothing_to_write_is_rejected(client):
    uid = upload(client)
    res = client.post(f"/api/export/{uid}", json={"paths": []})
    assert res.status_code == 400


def test_export_filename_is_sanitised(client):
    uid = upload(client)
    paths = client.post(f"/api/convert/{uid}", json={}).json()["paths"]
    res = client.post(f"/api/export/{uid}",
                      json={"paths": paths, "filename": "../../etc/passwd"})
    assert res.status_code == 200
    disposition = res.headers.get("content-disposition", "")
    assert "/" not in disposition.split("filename=")[-1]


# --- lifecycle ------------------------------------------------------------

def test_discard_removes_the_upload(client):
    uid = upload(client)
    assert client.delete(f"/api/upload/{uid}").status_code == 200
    assert client.get(f"/api/scan/{uid}").status_code == 404
