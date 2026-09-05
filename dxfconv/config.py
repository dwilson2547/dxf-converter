"""Tunable parameters for the scan -> DXF pipeline.

Every length is expressed in real-world millimetres, never pixels. The pipeline
converts to pixels once, using the scan DPI, so the same settings behave the
same way whether you scan at 300 or 600 DPI.
"""

from dataclasses import dataclass, asdict


@dataclass
class Config:
    # --- scale -------------------------------------------------------------
    dpi: float | None = None
    """Scan resolution. None means read it from the image metadata."""

    scale: float = 1.0
    """Correction factor applied to the finished geometry. Tracing sits the
    pen slightly outside the object, so parts come out a few tenths large;
    measure the real part, divide by the reported size, put it here."""

    # --- ink detection -----------------------------------------------------
    flatten_mm: float = 3.0
    """Background-flattening kernel. Must be wider than the pen stroke; kills
    scanner shading, paper tone and soft smudges. 0 disables."""

    threshold: str = "otsu"
    """otsu | adaptive | fixed"""

    threshold_value: int = 200
    """Only used when threshold == 'fixed' (0-255, ink is darker than this)."""

    close_gaps_mm: float = 0.0
    """Dilate-then-erode to bridge breaks in a stroke. Keep small."""

    # --- artifact rejection ------------------------------------------------
    border_margin_mm: float = 3.0
    """Drop anything touching this band around the page edge. This is what
    removes scanner edge stripes, which are long and thin and therefore
    survive every size-based filter."""

    min_area_mm2: float = 0.30
    """Cheap pre-filter for dust specks."""

    min_length_mm: float = 6.0
    """The real 'lines only' rule, applied to traced centreline length after
    vectorising. A speck has no length; a pen stroke does."""

    # --- vectorising -------------------------------------------------------
    mode: str = "outline"
    """outline    -> both edges of every stroke. Tracing rides the pen against
    the object, so the inner edge IS the true boundary; measured against
    calipers this is the accurate one, which is why it is the default.
    centerline -> one curve per stroke. Half a stroke-width outside the true
    edge, but gives a single tidy curve per stroke instead of two."""

    prune_spur_mm: float = 1.5
    """Skeletonisation grows short whiskers at stroke ends and crossings."""

    smooth_mm: float = 0.6
    """Moving-average window along the curve. Removes pixel staircase and
    paper fibre wobble without eating real curvature."""

    simplify_mm: float = 0.05
    """Douglas-Peucker tolerance. The vertex budget knob."""

    # --- output ------------------------------------------------------------
    entity: str = "lwpolyline"
    """lwpolyline | spline"""

    layer: str = "PROFILE"

    origin: str = "bbox"
    """bbox -> geometry starts at (0,0). page -> keep position on the page."""

    def px_per_mm(self, dpi: float) -> float:
        return dpi / 25.4

    def to_dict(self) -> dict:
        return asdict(self)
