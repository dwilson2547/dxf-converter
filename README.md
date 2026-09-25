# dxf-converter

Turns a scanned pen tracing into a clean, to-scale DXF that Onshape imports without complaint.

Traced an object onto paper, scanned it, and now the online converter hands you a file full of
speckle that jams the import. This does the conversion locally, keeps the real-world scale, and
emits geometry entities and nothing else.

## Web UI

```
./run.sh          # http://127.0.0.1:8077
```

Drop a scan in and you get the detected geometry over the faded scan. From there:

- **Click** a path to select it, **Del** to remove it. The sidebar lists every path sorted
  shortest-first, so leftover dirt sorts to the top where you can delete it in one click.
- **Shift+drag** boxes-selects paths (a path is only caught when it fits entirely inside the box,
  so brushing past a long curve doesn't sweep it up with the dirt).
- **Drag a point** to move it, **Alt+click** a path to insert one, **Del** with a point selected
  removes just that point.
- **Ctrl+Z** / **Ctrl+Shift+Z** undo and redo.
- The **Removed** toggle boxes what the filters threw out, so you can see what it discarded
  instead of taking its word for it.

Adjust the detection settings and hit **Re-detect** to run the pipeline again — that discards
hand edits, and the panel warns you when you have some.

## CLI

```
python3 -m dxfconv.cli samples/cl_35_profile.png -o out/profile.dxf --preview
```

```
  scan      300 DPI  (image metadata)
  ink       4 regions kept, 4 border artifacts, 4 specks dropped
  traced    6 segments -> 6 paths (6 closed)
            0 spurs pruned, 0 below min length
  vertices  354
  size      67.84 x 102.49 mm
```

Always look at the preview PNG before opening CAD: the scan sits faded underneath, extracted
vectors are green, and anything thrown away is boxed in red. If it discarded something you
wanted, that is where you see it.

## What it does differently

**Real millimetres.** Scan DPI comes from the image metadata, so a 300 DPI scan converts at
25.4/300 mm per pixel and the part lands in Onshape at roughly its true size. Every setting below
is specified in millimetres, so the same values work whether you scan at 300 or 600.

**Both stroke edges, by default.** A traced line is a pen stroke about 0.5 mm wide. Tracing rides
the pen tip against the object, so the *inner* edge of that stroke is the true boundary — which is
why `--mode outline` is the default and measures accurately against calipers.

`--mode centerline` instead takes the line down the middle of each stroke. It is tidier — one curve
per stroke rather than two, crossings rejoined into continuous paths, loops closed — but that
centreline sits about half a stroke-width outside the true edge, a consistent oversize in one
direction. Use it when you want clean topology and will correct the scale, or when the drawing is
lines rather than a traced outline.

**Two kinds of scanner dirt, two rules.** Dust specks are small, so an area threshold catches them.
Edge stripes — the dark band down the margin where the lid doesn't seal — are *long and thin*, so
they sail straight through any size filter. Those need `--border-margin-mm`. Anything that survives
both still has to clear `--min-length-mm` of actual traced length.

**Crossings stay continuous** (centreline mode). Skeletonising cuts every stroke where another
stroke touches it. The pieces are rejoined by pairing branches whose tangents run straight through,
so a curve crossing another comes out as one path and a loop that touches something still closes.
On the sample scan that turns 15 fragments back into the 4 pen strokes actually drawn.

One subtlety worth knowing if you touch that code: skeletonising an X-crossing does *not* produce a
4-way junction. It produces two Y-junctions joined by a connector a few pixels long, which has to be
collapsed first or nothing pairs up.

## Scale

On the sample, the raw scan and a hand-cleaned version of the same page agree to within 0.26 mm
over 102 mm — about 0.25%. Outline output has been checked in Onshape against caliper measurements
and held up.

Any remaining error comes from the tracing, not the conversion — how squarely the pen was held
against the edge. Measure the real part, divide by the reported size, and pass the ratio to
`--scale` (or the scale box in the web UI).

## Options

| Flag | Default | What it's for |
|---|---|---|
| `--dpi` | from metadata | Override when the scanner writes nothing useful |
| `--scale` | `1.0` | Correction factor for the finished geometry |
| `--flatten-mm` | `3.0` | Divides out paper tone and scanner shading. Must exceed stroke width |
| `--threshold` | `otsu` | `otsu`, `adaptive` (uneven lighting), or `fixed` |
| `--border-margin-mm` | `3.0` | Rejects edge stripes. Raise if the scan has a wide dark margin |
| `--min-area-mm2` | `0.30` | Speck pre-filter |
| `--min-length-mm` | `6.0` | The real "lines only" rule. Raise to be more aggressive |
| `--mode` | `outline` | `outline` keeps the true edge; `centerline` gives cleaner topology |
| `--prune-spur-mm` | `1.5` | Trims skeleton whiskers (centreline mode only) |
| `--smooth-mm` | `0.6` | Removes pixel staircase and paper wobble |
| `--simplify-mm` | `0.05` | Vertex budget. Raise for a lighter sketch |
| `--entity` | `lwpolyline` | `lwpolyline` or `spline` |
| `--origin` | `bbox` | `bbox` starts geometry at (0,0); `page` keeps its place on the sheet |

## If a scan gives you trouble

- **Strokes broken into pieces** — faint pencil or a drying pen. Try `--close-gaps-mm 0.3`, or
  `--threshold adaptive` if one part of the page scanned lighter than the rest.
- **Dirt survived** — raise `--min-length-mm`. If it's near the page edge, raise
  `--border-margin-mm`.
- **Real geometry vanished** — it was probably shorter than `--min-length-mm`; lower it. The red
  boxes in the preview tell you which rule fired.
- **Sketch too heavy in Onshape** — raise `--simplify-mm` to 0.1–0.2.

## Tests

```
python3 -m pytest tests/ -q
```

The synthetic cases are drawn at known sizes in millimetres, so scale accuracy is measured against
ground truth rather than asserted. They cover both artifact classes, crossing topology, the two
modes' differing relationship to the true edge, the DXF hygiene Onshape actually cares about (no
stray `POINT` entities, mm units declared), and the web round trip including that editor deletions
reach the exported file.

## Cluster deploy

Runs on the home cluster at http://dxf-converter.local via ArgoCD
(`infra/cluster-config/argocd/dxf-converter.yaml`, chart `helm/dxf-converter/`, namespace
`dxf-converter`). The image is `dwilson2547/dxf-converter:latest` with `pullPolicy: Always`:

```
docker build -t dwilson2547/dxf-converter:latest . && docker push dwilson2547/dxf-converter:latest
kubectl -n dxf-converter rollout restart deploy/dxf-converter
```

## Status

CLI and web UI both working, 29 tests passing, deployed to the cluster.

Nothing persists across a restart — uploads live in a temp directory (an `emptyDir` in the
cluster) keyed by id. That pins the deployment to one replica; a restart just means re-uploading
the scan.
