# Changelog

Each release is a git tag `vX.Y.Z` and a Docker image `dwilson2547/dxf-converter:X.Y.Z`
(`:latest` points at the newest release). The cluster runs the version pinned in
`helm/dxf-converter/values.yaml` — bump `image.tag` there to roll out a release.

## 2.0.0 — 2026-10-02

### Added
- **Accounts.** Log in / sign up from the top bar (sign-up switchable with `ALLOW_SIGNUP`);
  bearer-token auth in the `Authorization` header (no cookies), random tokens stored hashed,
  30-day sliding expiry; argon2id passwords. An expired login mid-edit re-prompts and retries
  without touching the editor. `python3 -m app.admin init-admin [--username NAME]` creates the
  first admin with a generated password; other admin commands for users and checks.
- **Saved scans** in Postgres + S3-compatible storage (AIStor): Save from the editor, a library
  on Home (thumbnails, filter, sort, rename, delete), reopen exactly where you left off.
- **Versions:** numbered, immutable, with labels, notes and lineage; load, relabel, delete.
- **Compare:** overlay another version (dashed, flip with B, opacity) with a live table of
  paths/points/circles/size differences and a warning when scales differ.
- **Account:** change password (signs out other sessions), delete your own account.
- **Admin panel:** users with their saved content; make/remove admin, reset password (shown
  once), delete content, delete user.
- **Ctrl+click** on a line adds a point on that segment (hold to drag it).

### Changed
- Download needs only the geometry in the browser (`POST /api/export`), so a server restart
  can't trap work.
- Re-detect asks before discarding hand edits and is undoable.
- One scale control: *Largest dimension* rescales in place (undoable, edits kept); the export
  "Scale factor" is gone.
- Errors show where they happened; loading overlay; empty states; missing Detection controls
  (Fixed threshold value, photo working resolution) and the single-layer export name.
- Static files are served with `Cache-Control: no-cache`.

### Fixed
- Clicking exactly on a line did nothing (the visible line sat over the click target).

### Deploy
- New secrets `dxf-converter-db` (`DATABASE_URL`) and `dxf-converter-bucket-credentials`;
  chart `accounts.*` values. Without them (`accounts.enabled: false`) it runs anonymously as
  before.

## 1.0.1 — 2026-10-02

### Fixed
- Pod OOM-killed during photo-mode conversions (1 GiB limit). Measured peaks: ~780 MB for a 1 MP
  photo, ~870 MB for a 12 MP one, before the web server's own share and before a second overlapping
  request.
  - Colour clustering now works on a copy at most 1200 px on its long side (it only needs a
    sample), not the full-resolution image.
  - Corner-preserving smoothing finds the nearest corner by sorted search instead of a
    points × corners matrix, which blew up on long, rough silhouettes.
  - Nearest-colour assignment and ring cutting no longer allocate full-size float64/int64
    temporaries.
  - The server runs one conversion at a time, so overlapping requests (eyedropper, Re-detect)
    queue instead of stacking memory.
  - Result: ~470 MB for the 1 MP photo; ~810 MB for 12 MP.

### Changed
- Helm: memory request 256Mi → 512Mi, limit 1Gi → 3Gi.

## 1.0.0 — 2026-10-01

### Added
- **Photo mode** (`--source photo`, auto-suggested in the web UI for colourful uploads): converts a
  colour photo of a printed badge/emblem into a layered DXF, one layer per ink colour plus the
  object's `OUTLINE`.
  - Colour separation by k-means in Lab; background is the colour owning the image border; small
    colours are ink, big areas are substrate.
  - Eyedropper in the UI / `--ink '#rrggbb'` to pick inks; picks add to what auto found.
  - Cleanup of specks, halo slivers along other colours' edges, and low-contrast shading.
  - Shapes are classified before cleanup: circles become true `CIRCLE` entities, rectilinear
    shapes (lettering) can be squared up with `--square`, freeform shapes get corner-preserving
    smoothing. Shapes touching a ring are cut free so the ring still fits as a circle.
- `--fit-mm` / "Largest dimension" in the UI: scale from one real measurement (works in scan mode
  too).
- Web UI: layer panel with visibility (hidden layers are left out of the export), per-layer colours,
  circles kept as `CIRCLE` through export unless a point is edited.
- `GET /api/version`.

### Changed
- DXF output carries layers with their true colours when the conversion produced them.
- Preview PNGs of small images render at 2x so thin detail stays legible.
- Image tags are now versioned; the Helm chart pins a release instead of tracking `:latest`.

## 0.1.0 — 2026-09-25

### Added
- Scanned pen tracing → to-scale DXF: outline and centreline modes, speck and edge-stripe
  rejection, real millimetres from scan DPI.
- Web UI with path editing (select, delete, move/insert points, undo/redo) and DXF export.
- Container image and Helm chart; deployed via ArgoCD at `dxf-converter.local`.
