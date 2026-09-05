"""Overlay render so you can check the trace before opening CAD.

Faded scan underneath, extracted vectors in green on top, rejected artifacts
boxed in red. If something got thrown away that you wanted, this is where you
see it.
"""

from __future__ import annotations

import numpy as np
import cv2


def render(gray: np.ndarray, paths_px, report, out_path: str, max_dim: int = 1600):
    canvas = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    canvas = (canvas * 0.25 + 255 * 0.75).astype(np.uint8)

    for bad in report.get("rejects", []):
        x, y, w, h = bad["bbox"]
        pad = 6
        cv2.rectangle(canvas, (x - pad, y - pad), (x + w + pad, y + h + pad),
                      (0, 0, 235), 3)

    for pts, closed in paths_px:
        poly = np.round(pts[:, ::-1]).astype(np.int32)   # (row,col) -> (x,y)
        cv2.polylines(canvas, [poly], bool(closed), (0, 160, 0), 3,
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
