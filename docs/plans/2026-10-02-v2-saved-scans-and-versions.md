# v2 plan: saved scans, versions, compare overlay

Status: **phases 1-2 done** (2026-10-02): accounts/storage backend and the scans API. Phases 3-5 planned.

## Goal

Today everything is temporary: an upload lives in the pod's temp dir until the pod restarts, and
edits live only in the browser tab. Coming back to a part a week later means re-uploading and
redoing every edit.

v2 adds:

1. **Accounts.** Log in to save; using the tool without logging in keeps working exactly as now.
2. **Saved scans.** The original image plus its geometry, stored per user, reloadable later.
3. **Versions.** Each save of a scan is a numbered, immutable version (settings + edited paths).
   Loading a version puts it in the editor; saving again makes a new version, never overwrites.
4. **Compare.** Overlay one version on another (ghosted, with an A/B toggle and opacity) plus a
   side-by-side of the numbers (paths, points, circles, size), to see which is better.

## Shape of the system

```
browser ──► dxf-converter API (cluster) ──► Postgres (cluster)   users, scans, versions, paths
                     │
                     └──────────────────► AIStor (TrueNAS)       original images, thumbnails
```

- **AIStor is storage only, never hosting.** The bucket is private; the browser never talks to it
  and gets no presigned URLs. The API checks the session, reads the object and streams it back —
  the same `/api/scan/...` path the editor already loads its underlay and eyedropper pixels from.
  Light traffic is expected, so proxying costs nothing worth optimising.
- **Postgres holds everything small and relational**: users, scans, versions, and each version's
  paths as `jsonb`. Paths are small (the Seeburg is ~1,300 points), are written together with
  their version row, and must stay consistent with it — so they belong in the database, not as
  loose objects.
- **The pipeline still needs a local file.** On load/convert, the API fetches the image into the
  existing temp dir, keyed by scan id, and reuses it while it's there. The temp dir becomes a cache,
  not the only copy.

## Data model

| Table | Columns |
|---|---|
| `users` | `id`, `username` (unique, lower-case), `password_hash` (argon2id), `is_admin`, `created_at` |
| `auth_tokens` | `token_hash` (SHA-256 of a random 256-bit token), `user_id`, `created_at`, `expires_at` |
| `scans` | `id`, `user_id`, `name`, `image_key`, `thumb_key`, `image_sha256`, `width_px`, `height_px`, `content_type`, `last_version`, `created_at`, `updated_at` |
| `versions` | `id`, `scan_id`, `number` (1, 2, … per scan), `label`, `note`, `settings` jsonb, `paths` jsonb, `stats` jsonb, `parent_number`, `created_at` |

- `settings` is the full `Settings` model (source, inks, fit_mm, square, …), so a version can be
  re-detected exactly as it was.
- `paths` is the editor's array as-is: `{points, closed, layer, color, kind}`. Hidden layers are
  stored as layer visibility in `settings`, not by dropping paths.
- `stats` is computed on save (paths, vertices, circles, extents) so the library and the compare
  panel don't have to load full geometry.
- `parent_number` records which version an edit started from: a short lineage, no branching UI.
- `last_version` is a counter: new versions take `last_version + 1` under a row lock on the scan,
  so concurrent saves get consecutive numbers and a deleted version's number is never reused.
- Editor-only state (hidden layers etc.) is posted as `view` and kept inside `settings` under
  `_view`; loading a version returns it separately as `view`.

Object keys in the bucket: `users/<user_id>/scans/<scan_id>/original.<ext>` and
`.../thumb.webp` (≤400 px, for the library list). Deleting a scan deletes both objects and its rows.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/auth/status` | whether accounts, storage and sign-up are on (the UI hides what isn't) |
| POST | `/api/auth/signup` | `{username, password}` → a plain account, logged in; 403 when `ALLOW_SIGNUP=false` |
| POST | `/api/auth/login` | `{username, password}` → `{token, expires_at, user}` |
| POST | `/api/auth/logout` | revokes the presented token |
| GET | `/api/auth/me` | who is logged in, or 401 |

**Auth is a bearer token in the `Authorization` header, never a cookie** (decided 2026-10-02:
cookie expiry is a recurring browser hassle). The token is random, not a JWT, so logout and
password changes end it immediately; only its SHA-256 is stored. Expiry slides: 30 days from last
use, pushed forward at most once a day. Failed logins are capped at 10 per client+username per
10 minutes. The UI keeps the token in `localStorage` and sends the header on every API call.
Because `<img>`/SVG `<image>` can't send headers, the editor fetches a saved scan's image with
the header and displays it from a blob URL — the eyedropper reads pixels from the same blob.
| GET | `/api/scans` | the user's scans with thumbnail URL, latest version's stats |
| POST | `/api/scans` | save the current upload as a new scan (image → AIStor) + version 1 |
| GET | `/api/scans/{id}` | scan + version list (stats only) |
| GET | `/api/scans/{id}/image`, `/thumb` | streamed from AIStor through the API |
| POST | `/api/scans/{id}/versions` | save editor state as the next version |
| GET | `/api/scans/{id}/versions/{n}` | settings + view + paths, to load into the editor or overlay |
| PATCH / DELETE | `/api/scans/{id}`, `.../versions/{n}` | rename / relabel / delete (the only version can't be deleted — delete the scan) |
| POST | `/api/scans/{id}/convert` | re-detect on the saved image (same pipeline as `/api/convert`); saves nothing |
| POST | `/api/scans/{id}/export` | DXF from posted paths, same as `/api/export` |

Saved images reach the browser only through these routes; they are never copied to the anonymous
`/api/scan/{upload_id}` path. Conversion reads a local cache (`<workdir>/saved/<scan_id>/`),
re-fetched from the bucket and checked against `image_sha256` when missing — after a pod restart,
for example. Limits: 20,000 paths and 500,000 points per version.

Every scan route checks ownership; another user's id is a 404, not a 403. Upload, convert and
export stay usable without logging in.

**Admin panel API** (admin token required; users addressed by id):

| Method | Path | |
|---|---|---|
| GET | `/api/admin/users` | every user with scan and version counts |
| DELETE | `/api/admin/users/{id}/content` | delete all their scans (images, then rows); the account stays |
| DELETE | `/api/admin/users/{id}` | delete the account and everything it owns |

An admin can't delete their own account there, and the last admin can't be deleted at all.
Deletion removes objects before rows, so a failed bucket call leaves nothing half-deleted.

**Accounts** (changed 2026-10-02 on request): **self sign-up is on**, with a "Sign up"
button next to "Log in"; sign-ups create plain users and are capped at 5 per client per hour.
`ALLOW_SIGNUP=false` turns it off, back to admin-created accounts only. The first admin is made
with `python3 -m app.admin init-admin`, which prints a generated 24-character password once —
run it with `kubectl exec` after the first deploy rather than fishing it out of startup logs.
`--reset` gives that admin a new generated password. `add-user --admin` makes more admins.

## UI

- **Header:** "Log in" and "Sign up" (when sign-up is on) / user name and "Log out"; "Admin" for
  admins.
- **Admin panel** (admins): the user list with scan/version counts, and per user "Delete content"
  and "Delete user", each behind a confirm that names the user and what will go.
- **Library** (logged in): the user's scans as cards — thumbnail, name, version count, last saved.
  Open one to get its version list.
- **Editor:** "Save" (first save names the scan; after that it adds a version, with an optional
  label like "squared, picked star ink"). A version strip shows `v1 v2 v3 …`; clicking one loads
  it, and there's a warning if you have unsaved edits.
- **Compare:** pick a version to compare against. It renders ghosted (one colour, dashed) under
  the current geometry, with an opacity slider and an A/B flip key. A small table shows both
  versions' paths, points, circles and size, with the differences.

## Phases

Each phase ends tested and committed; release once at the end as **2.0.0**.

1. **Storage + accounts backend — done 2026-10-02.** `app/store/` (config, models, db, auth,
   objects), `app/accounts.py` (routes + `current_user` dependency), `app/admin.py`
   (`add-user`, `set-password`, `list-users`, `delete-user`, `check`). Tests run against a
   throwaway `postgres:17` container and moto's S3 server (MinIO images don't pull, see below);
   an opt-in test hits the real bucket under a random `_tests/` prefix.
   Follow-up the same day: `is_admin`, `init-admin`, sign-up (+ `ALLOW_SIGNUP`), case-insensitive
   usernames, admin panel API (`app/admin_api.py`, `app/store/content.py`).
2. **Scans + versions API — done 2026-10-02.** `app/scans.py`; shared convert/export code moved
   to `app/editing.py`. 21 tests (`tests/test_scans.py`) incl. ownership on every route,
   concurrent version saves, cache loss, tampered and missing objects. End-to-end over HTTP in
   the built image with Postgres + an S3 server on the Seeburg photo.
3. **UI: login + sign-up, admin panel, library, save/load, version strip.** Driven end-to-end in
   a browser with Playwright. Scope widened by `docs/workflow-analysis-2026-10-02.md`: stateless
   export (`POST /api/export`) so a restart can't trap work; confirm + undo on Re-detect; inline
   errors; one scale control; 401 mid-edit re-prompts login and retries without touching the
   editor; save-while-anonymous keeps the editor state; library search/sort; compare warns when
   versions were scaled differently; self-service password change and account deletion; admin
   password reset and promote/demote (API + UI).
4. **UI: compare overlay + stats diff.**
5. **Deploy.** Create the bucket and a service account limited to it, create the database and
   role, apply the Kubernetes Secret (DB URL, S3 endpoint and keys, session secret — template in
   `infra/cluster-config/example-secrets/dxf-converter/`), chart env wiring, release 2.0.0. With
   state out of the pod, the one-replica rule in `values.yaml` no longer applies to saved scans
   (anonymous uploads still live in a single pod's temp dir). After the rollout, create the admin:
   `kubectl -n dxf-converter exec deploy/dxf-converter -- python3 -m app.admin init-admin`.

## Decisions to confirm

| Decision | Recommendation | Why |
|---|---|---|
| Where metadata lives | Cluster Postgres, new database `dxfconv` | Already running; keeps the pod stateless. SQLite on a PVC is the alternative. |
| Schema changes | **Decided:** bootstrap with `create_all` now; Alembic with a baseline at the first schema change after real scans exist | No data to preserve yet; the schema is still settling across phases 1-4. |
| Accounts | Admin-created, username + password | No identity provider exists on the cluster today; this can move to OIDC later without changing the data model. |
| Anonymous use | Keep it | Quick one-off conversions shouldn't need a login. |
| DXF files | Not stored; regenerated on export from the saved version | Paths + settings fully determine the DXF. |

## Storage facts

- **AIStor S3 API: `http://192.168.0.10:30320`, plain HTTP** — verified 2026-10-02:
  `/minio/health/live` returned 200 from the workstation and from a pod in the `dxf-converter`
  namespace. `:30321` is the web console, not the API. The race logger (robo-services) uses the
  same endpoint.
- **Bucket: `dxf-converter`** — created by hand 2026-10-02.
- **Credentials:** Secret `dxf-converter-bucket-credentials` (namespace `dxf-converter`, keys
  `S3_ACCESS_KEY` / `S3_SECRET_KEY`), template at
  `infra/cluster-config/example-secrets/dxf-converter/secret.yml`, applied by hand. Endpoint and
  bucket go in `values.yaml`, not the secret.
- **Credentials verified 2026-10-02** from a pod reading that Secret: list, put, get and delete in
  `dxf-converter` all succeed.
- **`minio/mc` and `quay.io/minio/mc` images would not pull** (Docker Hub pull failed; quay
  returned 401) on 2026-10-02. Phase 1 planned tests against a throwaway MinIO container — confirm
  a pullable server image first, or fall back to a scratch bucket prefix on AIStor.

## Unknowns

- The database URL and session secret: added to the cluster in phase 5, alongside creating the
  `dxfconv` database and role.
