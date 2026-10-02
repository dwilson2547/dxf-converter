# v2 plan: saved scans, versions, compare overlay

Status: **planned** (2026-10-02). Nothing here is built yet.

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
| `users` | `id`, `username` (unique), `password_hash` (argon2id), `created_at` |
| `sessions` | `id` (random 256-bit token, stored hashed), `user_id`, `created_at`, `expires_at` |
| `scans` | `id`, `user_id`, `name`, `image_key`, `image_sha256`, `width_px`, `height_px`, `content_type`, `created_at`, `updated_at` |
| `versions` | `id`, `scan_id`, `number` (1, 2, … per scan), `label`, `note`, `settings` jsonb, `paths` jsonb, `stats` jsonb, `parent_number`, `created_at` |

- `settings` is the full `Settings` model (source, inks, fit_mm, square, …), so a version can be
  re-detected exactly as it was.
- `paths` is the editor's array as-is: `{points, closed, layer, color, kind}`. Hidden layers are
  stored as layer visibility in `settings`, not by dropping paths.
- `stats` is computed on save (paths, vertices, circles, extents) so the library and the compare
  panel don't have to load full geometry.
- `parent_number` records which version an edit started from: a short lineage, no branching UI.

Object keys in the bucket: `users/<user_id>/scans/<scan_id>/original.<ext>` and
`.../thumb.webp` (≤400 px, for the library list). Deleting a scan deletes both objects and its rows.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/auth/login`, `/api/auth/logout` | session cookie: `HttpOnly`, `SameSite=Lax` |
| GET | `/api/auth/me` | who is logged in, or 401 |
| GET | `/api/scans` | the user's scans with thumbnail URL, latest version's stats |
| POST | `/api/scans` | save the current upload as a new scan (image → AIStor) + version 1 |
| GET | `/api/scans/{id}` | scan + version list (stats only) |
| GET | `/api/scans/{id}/image`, `/thumb` | streamed from AIStor through the API |
| POST | `/api/scans/{id}/versions` | save editor state as the next version |
| GET | `/api/scans/{id}/versions/{n}` | settings + paths, to load into the editor or overlay |
| PATCH / DELETE | `/api/scans/{id}`, `.../versions/{n}` | rename / relabel / delete |

Every scan route checks ownership; another user's id is a 404, not a 403. Upload, convert and
export stay usable without logging in.

**Accounts are created by an admin command, not self-registration**: `python3 -m app.admin
add-user <name>` run in the pod. It's a home tool behind the LAN, and an open sign-up form is
attack surface for no benefit.

## UI

- **Header:** "Log in" / user name and "Log out".
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

1. **Storage + accounts backend.** DB schema, argon2 passwords, sessions, S3 client, admin CLI.
   Tests run against a throwaway MinIO container and a scratch Postgres, not mocks — the storage
   path is what is being added.
2. **Scans + versions API.** Save, list, load, stream image/thumb, version CRUD, ownership checks.
   Image cache in the temp dir, re-fetched from AIStor after a pod restart.
3. **UI: login, library, save/load, version strip.** Driven end-to-end in a browser with Playwright.
4. **UI: compare overlay + stats diff.**
5. **Deploy.** Create the bucket and a service account limited to it, create the database and
   role, apply the Kubernetes Secret (DB URL, S3 endpoint and keys, session secret — template in
   `infra/cluster-config/example-secrets/dxf-converter/`), chart env wiring, release 2.0.0. With
   state out of the pod, the one-replica rule in `values.yaml` no longer applies to saved scans
   (anonymous uploads still live in a single pod's temp dir).

## Decisions to confirm

| Decision | Recommendation | Why |
|---|---|---|
| Where metadata lives | Cluster Postgres, new database `dxfconv` | Already running; keeps the pod stateless. SQLite on a PVC is the alternative. |
| Schema changes | Alembic from the first migration | This will hold real data from day one. |
| Accounts | Admin-created, username + password | No identity provider exists on the cluster today; this can move to OIDC later without changing the data model. |
| Anonymous use | Keep it | Quick one-off conversions shouldn't need a login. |
| DXF files | Not stored; regenerated on export from the saved version | Paths + settings fully determine the DXF. |

## Unknowns

- **AIStor endpoint, port and TLS** — `⚠ unverified`. The workspace records only that AIStor runs
  as a TrueNAS app (`infra/cluster-config/docs/issues/Aistor truenas tls fix.md`); its address is
  not written down anywhere. Needed before phase 1's real-storage tests against it, and for phase 5.
- **Whether the cluster can reach the TrueNAS box** on that port — to be checked from a pod in
  phase 5, not assumed.
- **Bucket and service-account creation** need an AIStor admin login. Done by hand or with `mc`
  using admin credentials supplied at the time, never committed.
