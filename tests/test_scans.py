"""Saved scans and versions: save, list, load, stream, version, delete.

Runs against a real Postgres and an S3 server (see conftest.py).
"""

import hashlib
import io
import os
import shutil
import threading

import ezdxf
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.editing import WORKDIR
from app.main import app
from app.store import ObjectNotFound, ObjectStore, auth
from app.store.models import Scan, Version


@pytest.fixture
def client(store):
    return TestClient(app)


def _user(store, client, name="dan"):
    with store.db.session() as s:
        auth.create_user(s, name, "correct horse")
    tok = client.post("/api/auth/login",
                      json={"username": name, "password": "correct horse"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def dan(store, client):
    return _user(store, client, "dan")


@pytest.fixture
def eve(store, client):
    return _user(store, client, "eve")


def png_bytes(w=600, h=500):
    img = np.full((h, w, 3), 255, np.uint8)
    img[100:400, 100:500] = (30, 40, 200)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def upload(client, data=None):
    res = client.post("/api/upload", files={"file": ("badge.png", data or png_bytes(), "image/png")})
    assert res.status_code == 200, res.text
    return res.json()["id"]


PATHS = [{"points": [[0, 0], [10, 0], [10, 5]], "closed": True, "layer": "INK1",
          "color": "#aa0000", "kind": "poly"},
         {"points": [[20, 20], [25, 20], [25, 25]], "closed": True, "layer": "OUTLINE",
          "color": "#888888", "kind": "circle"}]


def save(client, headers, name="Seeburg", **extra):
    body = {"upload_id": upload(client), "name": name, "settings": {"source": "photo", "fit_mm": 76},
            "paths": PATHS, "label": "first try", "view": {"hidden_layers": ["OUTLINE"]}}
    body.update(extra)
    res = client.post("/api/scans", json=body, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def new_version(client, headers, scan_id, **extra):
    body = {"settings": {"source": "photo", "square": True}, "paths": PATHS[:1]}
    body.update(extra)
    return client.post(f"/api/scans/{scan_id}/versions", json=body, headers=headers)


# --- saving -------------------------------------------------------------------

def test_saving_an_upload_stores_the_image_a_thumbnail_and_v1(client, store, dan):
    data = png_bytes()
    out = client.post("/api/scans", headers=dan, json={
        "upload_id": upload(client, data), "name": "Seeburg",
        "settings": {"source": "photo"}, "paths": PATHS}).json()
    scan, v = out["scan"], out["version"]
    assert v["number"] == 1 and scan["versions"] == 1
    assert scan["width_px"] == 600 and scan["height_px"] == 500
    assert v["stats"]["paths"] == 2 and v["stats"]["vertices"] == 6
    assert v["stats"]["circles"] == 1
    assert v["stats"]["layers"] == {"INK1": 1, "OUTLINE": 1}
    with store.db.session() as s:
        row = s.get(Scan, scan["id"])
        key, thumb_key, sha = row.image_key, row.thumb_key, row.image_sha256
    assert key.startswith("users/") and key.endswith("/original.png")
    assert store.objects.get(key) == data
    assert sha == hashlib.sha256(data).hexdigest()
    with Image.open(io.BytesIO(store.objects.get(thumb_key))) as t:
        assert t.format == "WEBP" and max(t.size) <= 400


def test_saving_needs_a_login(client, store):
    res = client.post("/api/scans", json={"upload_id": upload(client), "name": "x",
                                          "settings": {}, "paths": []})
    assert res.status_code == 401


def test_saving_needs_image_storage(client, store_cfg):
    from dataclasses import replace
    from app import accounts
    from app.store import StoreConfig
    accounts.configure(replace(store_cfg, s3_endpoint=None))
    try:
        c = TestClient(app)
        with accounts.current_store().db.session() as s:
            auth.create_user(s, "dan", "correct horse")
        tok = c.post("/api/auth/login", json={"username": "dan", "password": "correct horse"}).json()["token"]
        res = c.post("/api/scans", headers={"Authorization": f"Bearer {tok}"},
                     json={"upload_id": upload(c), "name": "x", "settings": {}, "paths": []})
        assert res.status_code == 503
    finally:
        accounts.configure(StoreConfig.from_env({}))


def test_unknown_upload_is_404(client, store, dan):
    res = client.post("/api/scans", headers=dan, json={
        "upload_id": "deadbeef", "name": "x", "settings": {}, "paths": []})
    assert res.status_code == 404


def test_oversized_geometry_is_refused(client, store, dan, monkeypatch):
    from app import scans
    monkeypatch.setattr(scans, "MAX_POINTS", 5)
    res = client.post("/api/scans", headers=dan, json={
        "upload_id": upload(client), "name": "x", "settings": {}, "paths": PATHS})
    assert res.status_code == 413


# --- listing and loading ------------------------------------------------------

def test_list_shows_latest_version_newest_scan_first(client, store, dan):
    a = save(client, dan, "first")["scan"]["id"]
    b = save(client, dan, "second")["scan"]["id"]
    new_version(client, dan, a, label="squared")
    scans = client.get("/api/scans", headers=dan).json()["scans"]
    assert [s["id"] for s in scans] == [a, b]        # a was updated last
    assert scans[0]["versions"] == 2
    assert scans[0]["latest"]["number"] == 2 and scans[0]["latest"]["label"] == "squared"
    assert scans[0]["thumb_url"] == f"/api/scans/{a}/thumb"


def test_load_a_version_round_trips_settings_paths_and_view(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    v = client.get(f"/api/scans/{sid}/versions/1", headers=dan).json()
    assert v["paths"] == PATHS
    assert v["settings"]["source"] == "photo" and v["settings"]["fit_mm"] == 76
    assert v["settings"]["square"] is False             # defaults filled in
    assert v["view"] == {"hidden_layers": ["OUTLINE"]}
    assert "_view" not in v["settings"]
    assert v["label"] == "first try"


def test_scan_detail_lists_versions_without_geometry(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    new_version(client, dan, sid, parent_number=1)
    d = client.get(f"/api/scans/{sid}", headers=dan).json()
    assert [v["number"] for v in d["version_list"]] == [1, 2]
    assert d["version_list"][1]["parent_number"] == 1
    assert all("paths" not in v for v in d["version_list"])


def test_image_and_thumb_stream_through_the_api(client, store, dan):
    data = png_bytes(320, 200)
    sid = client.post("/api/scans", headers=dan, json={
        "upload_id": upload(client, data), "name": "x", "settings": {},
        "paths": PATHS}).json()["scan"]["id"]
    res = client.get(f"/api/scans/{sid}/image", headers=dan)
    assert res.status_code == 200 and res.content == data
    assert res.headers["content-type"] == "image/png"
    assert res.headers["cache-control"].startswith("private")
    th = client.get(f"/api/scans/{sid}/thumb", headers=dan)
    assert th.status_code == 200 and th.headers["content-type"] == "image/webp"


def test_image_needs_the_bearer_header(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    assert client.get(f"/api/scans/{sid}/image").status_code == 401


# --- versions -----------------------------------------------------------------

def test_versions_number_up_and_numbers_are_never_reused(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    assert new_version(client, dan, sid).json()["number"] == 2
    assert new_version(client, dan, sid).json()["number"] == 3
    assert client.delete(f"/api/scans/{sid}/versions/3", headers=dan).status_code == 200
    assert new_version(client, dan, sid).json()["number"] == 4


def test_the_only_version_cant_be_deleted(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    assert client.delete(f"/api/scans/{sid}/versions/1", headers=dan).status_code == 409


def test_parent_must_be_a_version_of_this_scan(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    assert new_version(client, dan, sid, parent_number=9).status_code == 422


def test_relabel_a_version_and_rename_a_scan(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    r = client.patch(f"/api/scans/{sid}/versions/1", headers=dan, json={"label": "keeper"})
    assert r.json()["label"] == "keeper" and r.json()["note"] is None
    assert client.patch(f"/api/scans/{sid}", headers=dan,
                        json={"name": "Seeburg emblem"}).json()["name"] == "Seeburg emblem"


def test_concurrent_saves_get_distinct_numbers(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    results = []

    def go():
        results.append(new_version(TestClient(app), dan, sid).json()["number"])

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [2, 3, 4, 5, 6, 7]


# --- converting and exporting a saved scan ------------------------------------

def test_convert_works_after_the_local_cache_is_gone(client, store, dan):
    """A pod restart empties the work dir; the image comes back from the bucket."""
    sid = save(client, dan)["scan"]["id"]
    shutil.rmtree(os.path.join(WORKDIR, "saved", sid), ignore_errors=True)
    res = client.post(f"/api/scans/{sid}/convert", headers=dan,
                      json={"settings": {"source": "scan", "min_length_mm": 1}})
    assert res.status_code == 200, res.text
    assert res.json()["report"]["paths"] >= 1
    assert os.path.exists(os.path.join(WORKDIR, "saved", sid, "original.png"))


def test_a_tampered_image_is_refused(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    with store.db.session() as s:
        key = s.get(Scan, sid).image_key
    store.objects.put(key, png_bytes(10, 10), "image/png")
    shutil.rmtree(os.path.join(WORKDIR, "saved", sid), ignore_errors=True)
    res = client.post(f"/api/scans/{sid}/convert", headers=dan, json={"settings": {}})
    assert res.status_code == 502


def test_a_missing_image_is_410(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    with store.db.session() as s:
        key = s.get(Scan, sid).image_key
    store.objects.client.delete_object(Bucket=store.objects.bucket, Key=key)
    shutil.rmtree(os.path.join(WORKDIR, "saved", sid), ignore_errors=True)
    assert client.post(f"/api/scans/{sid}/convert", headers=dan,
                       json={"settings": {}}).status_code == 410
    assert client.get(f"/api/scans/{sid}/image", headers=dan).status_code == 410


def test_export_a_saved_scan(client, store, dan, tmp_path):
    sid = save(client, dan)["scan"]["id"]
    v = client.get(f"/api/scans/{sid}/versions/1", headers=dan).json()
    res = client.post(f"/api/scans/{sid}/export", headers=dan,
                      json={"paths": v["paths"], "filename": "seeburg"})
    assert res.status_code == 200
    assert "seeburg.dxf" in res.headers["content-disposition"]
    out = tmp_path / "s.dxf"
    out.write_bytes(res.content)
    msp = ezdxf.readfile(str(out)).modelspace()
    assert len(msp.query("CIRCLE")) == 1 and len(msp.query("LWPOLYLINE")) == 1


# --- deleting -----------------------------------------------------------------

def test_delete_scan_removes_rows_objects_and_cache(client, store, dan):
    sid = save(client, dan)["scan"]["id"]
    client.post(f"/api/scans/{sid}/convert", headers=dan, json={"settings": {}})
    with store.db.session() as s:
        row = s.get(Scan, sid)
        keys = [row.image_key, row.thumb_key]
    assert client.delete(f"/api/scans/{sid}", headers=dan).status_code == 200
    with store.db.session() as s:
        assert s.get(Scan, sid) is None
        assert s.query(Version).filter_by(scan_id=sid).count() == 0
    for k in keys:
        with pytest.raises(ObjectNotFound):
            store.objects.get(k)
    assert not os.path.exists(os.path.join(WORKDIR, "saved", sid))


# --- other people's scans -----------------------------------------------------

def test_another_users_scan_is_404_everywhere(client, store, dan, eve):
    sid = save(client, dan)["scan"]["id"]
    for method, url, body in [
        ("get", f"/api/scans/{sid}", None),
        ("get", f"/api/scans/{sid}/image", None),
        ("get", f"/api/scans/{sid}/thumb", None),
        ("get", f"/api/scans/{sid}/versions/1", None),
        ("patch", f"/api/scans/{sid}", {"name": "mine now"}),
        ("patch", f"/api/scans/{sid}/versions/1", {"label": "x"}),
        ("post", f"/api/scans/{sid}/versions", {"settings": {}, "paths": []}),
        ("post", f"/api/scans/{sid}/convert", {"settings": {}}),
        ("post", f"/api/scans/{sid}/export", {"paths": PATHS}),
        ("delete", f"/api/scans/{sid}/versions/1", None),
        ("delete", f"/api/scans/{sid}", None),
    ]:
        res = getattr(client, method)(url, headers=eve, **({"json": body} if body else {}))
        assert res.status_code == 404, (method, url, res.status_code)
    assert client.get("/api/scans", headers=eve).json()["scans"] == []
    assert client.get(f"/api/scans/{sid}", headers=dan).status_code == 200
