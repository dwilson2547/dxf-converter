"""Overlay render so you can check the trace before opening CAD.

Faded scan underneath, extracted vectors in green on top, rejected artifacts
boxed in red. If something got thrown away that you wanted, this is where you
see it.
"""

from __future__ import annotations

import numpy as np
import cv2


def _bgr(hex_color: str | None, fallback=(0, 160, 0)):
    if not hex_color:
        return fallback
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    # Darken so a pale ink still shows against the faded photo.
    return (int(b * 0.7), int(g * 0.7), int(r * 0.7))


def render(gray: np.ndarray, paths_px, report, out_path: str, max_dim: int = 1600,
           meta=None, color_path: str | None = None):
    """meta gives each path's layer colour (photo mode); color_path puts the
    colour photo underneath instead of the grayscale scan."""
    if color_path:
        from .photo import load_color
        canvas = load_color(color_path)
    else:
        canvas = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    canvas = (canvas * 0.25 + 255 * 0.75).astype(np.uint8)
    # Paths are drawn on a 2x canvas so thin detail stays legible.
    up = 2 if max(canvas.shape[:2]) * 2 <= max_dim * 1.5 else 1
    if up > 1:
        canvas = cv2.resize(canvas, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)

    for bad in report.get("rejects", []):
        x, y, w, h = (v * up for v in bad["bbox"])
        pad = 6
        cv2.rectangle(canvas, (x - pad, y - pad), (x + w + pad, y + h + pad),
                      (0, 0, 235), 3)

    for n, (pts, closed) in enumerate(paths_px):
        poly = np.round(pts[:, ::-1] * up).astype(np.int32)   # (row,col) -> (x,y)
        color = _bgr(meta[n].get("color")) if meta else (0, 160, 0)
        cv2.polylines(canvas, [poly], bool(closed), color, 2 if up > 1 else 3,
                      lineType=cv2.LINE_AA)
        if not closed and len(poly):
            for end in (poly[0], poly[-1]):
                cv2.circle(canvas, tuple(end), 7, (200, 90, 0), -1)

    h, w = canvas.shape[:2]
    scale = min(1.0, max_dim / max(h, w))
    if scale < 1.0:
        canvas = cv2.resize(canvas, (int(w * scale), int(h * scale)),
                            interpolation=cv2.INTER_AREA)

    cv2.imwrite(out_path, canvas)
    return out_path
