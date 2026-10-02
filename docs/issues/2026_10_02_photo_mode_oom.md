# Photo mode OOM-killed the pod (1.0.0)

**Date:** 2026-10-02 · **Fixed in:** 1.0.1

## Symptom
After 1.0.0 shipped photo mode, conversions in the web UI crashed the pod: `OOMKilled`, exit 137,
restarts climbing, readiness probe failing while it came back.

## Cause
The chart's memory limit (1Gi) dated from scan mode. Measured peak RSS of one photo-mode CLI run:
~780 MB on a 949×1024 photo (upsampled 3×), ~870 MB on a 12 MP one. The web server adds its own
share, and the editor fires overlapping `/api/convert` calls (eyedropper pick, Re-detect), which
stack.

Biggest contributors, from per-stage `tracemalloc`:
- `build_palette` filtered and Lab-converted the full image just to sample colours for k-means.
- `corner_preserving_smooth` built an `n_points × n_corners` distance matrix; on a long, rough
  silhouette contour that is tens of thousands × thousands.
- `assign` and `carve_rings` made full-size float64/int64 temporaries.

## Fix
Palette from a ≤1200 px copy; nearest corner by `searchsorted`; in-place float32 maths; a
`threading.Lock` so conversions run one at a time; limit 3Gi, request 512Mi.
Verified: ~470 MB on the 1 MP photo; two concurrent 12 MP conversions in a container capped at
1.5 GB both return 200 (queued, 3 s and 6 s), no OOM.

## Check next time
New pipeline stages that touch the upsampled image: measure with
`/usr/bin/time -f %M python3 -m dxfconv.cli <photo> --source photo` before raising limits.
