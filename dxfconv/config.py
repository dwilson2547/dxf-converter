"""Tunable parameters for the scan -> DXF pipeline.

Every length is expressed in real-world millimetres, never pixels. The pipeline
converts to pixels once, using the scan DPI, so the same settings behave the
same way whether you scan at 300 or 600 DPI.
"""

from dataclasses import dataclass, asdict, field


@dataclass
class Config:
    # --- input -------------------------------------------------------------
    source: str = "scan"
    """scan  -> dark pen lines on paper, traced in grayscale.
    photo -> a colour photo of a printed object (badge, emblem, sign); colours
    are separated into one layer each, circles and lettering regularised."""

    # --- scale -------------------------------------------------------------
    dpi: float | None = None
    """Scan resolution. None means read it from the image metadata."""

    scale: float = 1.0
    """Correction factor applied to the finished geometry. Tracing sits the
    pen slightly outside the object, so parts come out a few tenths large;
    measure the real part, divide by the reported size, put it here."""

    fit_mm: float | None = None
    """Scale so the geometry's largest dimension is this many millimetres.
    A photo has no meaningful DPI, so measure one thing on the real object
    (e.g. the outer diameter of a badge) and put it here. Overrides DPI."""

    # --- photo mode --------------------------------------------------------
    colors: int = 4
    """How many colours to separate the photo into, background included."""

    inks: list[str] = field(default_factory=list)
    """Ink colours to trace, as #rrggbb. Empty means auto: every colour that
    covers less than a third of the object. Pick them when auto lumps a thin
    detail in with a big area of similar colour."""

    circles: bool = True
    """Replace contours that are circles with true CIRCLE entities."""

    square: bool = False
    """Square up lettering: straight edges, square corners, shared baselines.
    Leaves long gentle curves alone, so a swash or swoosh survives."""

    upsample: int = 0
    """Photo working resolution multiplier. 0 = auto (~3000 px long side)."""

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
