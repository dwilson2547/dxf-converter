# Changelog

Each release is a git tag `vX.Y.Z` and a Docker image `dwilson2547/dxf-converter:X.Y.Z`
(`:latest` points at the newest release). The cluster runs the version pinned in
`helm/dxf-converter/values.yaml` — bump `image.tag` there to roll out a release.

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
