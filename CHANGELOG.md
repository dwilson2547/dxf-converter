# Changelog

Each release is a git tag `vX.Y.Z` and a Docker image `dwilson2547/dxf-converter:X.Y.Z`
(`:latest` points at the newest release). The cluster runs the version pinned in
`helm/dxf-converter/values.yaml` — bump `image.tag` there to roll out a release.

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
