"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import Config
from .pipeline import convert


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dxfconv",
        description="Convert a scanned pen tracing into a clean, to-scale DXF.")
    p.add_argument("image")
    p.add_argument("-o", "--out", help="output .dxf (default: alongside input)")
    p.add_argument("-p", "--preview", nargs="?", const="auto",
                   help="write an overlay PNG for eyeballing the trace")
    p.add_argument("--json", action="store_true", help="print the report as JSON")

    d = Config()
    g = p.add_argument_group("scale")
    g.add_argument("--dpi", type=float, help="override scan DPI")
    g.add_argument("--scale", type=float, default=d.scale,
                   help="correction factor for the finished geometry")

    g = p.add_argument_group("ink detection")
    g.add_argument("--flatten-mm", type=float, default=d.flatten_mm)
    g.add_argument("--threshold", choices=["otsu", "adaptive", "fixed"],
                   default=d.threshold)
    g.add_argument("--threshold-value", type=int, default=d.threshold_value)
    g.add_argument("--close-gaps-mm", type=float, default=d.close_gaps_mm)

    g = p.add_argument_group("artifact rejection")
    g.add_argument("--border-margin-mm", type=float, default=d.border_margin_mm)
    g.add_argument("--min-area-mm2", type=float, default=d.min_area_mm2)
    g.add_argument("--min-length-mm", type=float, default=d.min_length_mm)

    g = p.add_argument_group("vectorising")
    g.add_argument("--mode", choices=["centerline", "outline"], default=d.mode)
    g.add_argument("--prune-spur-mm", type=float, default=d.prune_spur_mm)
    g.add_argument("--smooth-mm", type=float, default=d.smooth_mm)
    g.add_argument("--simplify-mm", type=float, default=d.simplify_mm)

    g = p.add_argument_group("output")
    g.add_argument("--entity", choices=["lwpolyline", "spline"], default=d.entity)
    g.add_argument("--layer", default=d.layer)
    g.add_argument("--origin", choices=["bbox", "page"], default=d.origin)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    cfg = Config(
        dpi=args.dpi,
        scale=args.scale,
        flatten_mm=args.flatten_mm,
        threshold=args.threshold,
        threshold_value=args.threshold_value,
        close_gaps_mm=args.close_gaps_mm,
        border_margin_mm=args.border_margin_mm,
        min_area_mm2=args.min_area_mm2,
        min_length_mm=args.min_length_mm,
        mode=args.mode,
        prune_spur_mm=args.prune_spur_mm,
        smooth_mm=args.smooth_mm,
        simplify_mm=args.simplify_mm,
        entity=args.entity,
        layer=args.layer,
        origin=args.origin,
    )

    out = args.out or os.path.splitext(args.image)[0] + ".dxf"
    prev = None
    if args.preview:
        # Sit the preview next to the DXF, not next to the input.
        prev = (os.path.splitext(out)[0] + "_preview.png"
                if args.preview == "auto" else args.preview)

    report = convert(args.image, out, cfg, prev)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    pre, vec, ext = report["preprocess"], report["vectorize"], report["extents_mm"]
    print(f"  scan      {report['dpi']:g} DPI  ({report['dpi_source']})")
    print(f"  ink       {pre['kept']} regions kept, "
          f"{pre['dropped_border']} border artifacts, "
          f"{pre['dropped_small']} specks dropped")
    print(f"  traced    {vec['segments']} segments -> {vec['kept']} paths "
          f"({vec['closed']} closed)")
    print(f"            {vec['dropped_spur']} spurs pruned, "
          f"{vec['dropped_short']} below min length")
    print(f"  vertices  {report['vertices']}")
    if ext:
        print(f"  size      {ext['width_mm']:.2f} x {ext['height_mm']:.2f} mm")
    print(f"  wrote     {report['dxf']}")
    if prev:
        print(f"            {prev}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
