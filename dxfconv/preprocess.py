"""Scan -> clean binary ink mask.

The job here is to end up with a mask containing pen strokes and nothing else.
Everything downstream assumes the mask is already honest.
"""

from __future__ import annotations

import numpy as np
import cv2
from PIL import Image


DEFAULT_DPI = 300.0


def load_gray(path: str, dpi_override: float | None = None):
    """Return (grayscale uint8, dpi, dpi_source)."""
    with Image.open(path) as im:
        meta_dpi = im.info.get("dpi")
        im = im.convert("L")
        gray = np.array(im)

    if dpi_override:
        return gray, float(dpi_override), "override"
    if meta_dpi and meta_dpi[0] and float(meta_dpi[0]) > 1:
        # Scanners write e.g. 299.9994; round to a sane nominal value.
        d = float(meta_dpi[0])
        nominal = min((72, 96, 150, 200, 300, 400, 600, 1200),
                      key=lambda n: abs(n - d))
        if abs(nominal - d) / nominal < 0.02:
            d = float(nominal)
        return gray, d, "image metadata"
    return gray, DEFAULT_DPI, "default (no metadata)"


def _odd(n: int) -> int:
    n = max(1, int(round(n)))
    return n if n % 2 else n + 1


def flatten_background(gray: np.ndarray, kernel_px: int) -> np.ndarray:
    """Divide out the paper.

    Closing with a kernel wider than the stroke erases the ink, leaving an
    estimate of the page itself (shading, tone, the grey wash a scanner lid
    leaves behind). Dividing by it normalises the paper to white and makes a
    single global threshold safe.
    """
    k = _odd(kernel_px)
    if k < 3:
        return gray
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = np.maximum(bg, 1)
    flat = (gray.astype(np.float32) / bg.astype(np.float32)) * 255.0
    return np.clip(flat, 0, 255).astype(np.uint8)


def binarize(gray: np.ndarray, cfg, px_per_mm: float) -> np.ndarray:
    """Ink -> 255, paper -> 0."""
    if cfg.threshold == "fixed":
        _, bw = cv2.threshold(gray, cfg.threshold_value, 255, cv2.THRESH_BINARY_INV)
    elif cfg.threshold == "adaptive":
        block = _odd(px_per_mm * 6)
        bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, block, 10)
    else:
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, bw = cv2.threshold(blur, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def clean(bw: np.ndarray, cfg, px_per_mm: float):
    """Drop border artifacts and specks. Returns (mask, report)."""
    h, w = bw.shape
    report = {"kept": 0, "dropped_border": 0, "dropped_small": 0, "rejects": []}

    if cfg.close_gaps_mm > 0:
        k = _odd(cfg.close_gaps_mm * px_per_mm)
        bw = cv2.morphologyEx(
            bw, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))

    margin = int(round(cfg.border_margin_mm * px_per_mm))
    min_area = cfg.min_area_mm2 * px_per_mm ** 2

    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw, 8)
    keep = np.zeros(n, dtype=bool)

    for i in range(1, n):
        x, y, cw, ch, area = stats[i, :5]
        touches_border = (x < margin or y < margin or
                          x + cw > w - margin or y + ch > h - margin)
        if touches_border:
            report["dropped_border"] += 1
            report["rejects"].append(
                {"reason": "border", "bbox": [int(x), int(y), int(cw), int(ch)],
                 "area_px": int(area)})
            continue
        if area < min_area:
            report["dropped_small"] += 1
            report["rejects"].append(
                {"reason": "small", "bbox": [int(x), int(y), int(cw), int(ch)],
                 "area_px": int(area)})
            continue
        keep[i] = True
        report["kept"] += 1

    return np.where(keep[labels], 255, 0).astype(np.uint8), report


def build_mask(path: str, cfg):
    """Full front half of the pipeline."""
    gray, dpi, dpi_source = load_gray(path, cfg.dpi)
    px_per_mm = cfg.px_per_mm(dpi)

    flat = flatten_background(gray, cfg.flatten_mm * px_per_mm) \
        if cfg.flatten_mm > 0 else gray
    bw = binarize(flat, cfg, px_per_mm)

    ink_fraction = float((bw > 0).mean())
    mask, report = clean(bw, cfg, px_per_mm)
    report["ink_fraction"] = round(ink_fraction, 4)
    report["dpi"] = dpi
    report["dpi_source"] = dpi_source
    report["px_per_mm"] = px_per_mm

    return gray, mask, dpi, report
