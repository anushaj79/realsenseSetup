#!/usr/bin/env python3
"""Pick the sharpest frames from a recording for Gaussian splatting / COLMAP.

A 2-minute take at 30 fps is ~3600 near-duplicate frames -- far too many for
COLMAP and most splat tools. This keeps the sharpest frame (least motion blur)
out of every N consecutive frames and copies them into <recording>/images/.

    .venv/bin/python pick_frames.py recordings/20261115_143000            # ~3 per second
    .venv/bin/python pick_frames.py recordings/20261115_143000 --every 5   # more frames
    .venv/bin/python pick_frames.py recordings/20261115_143000 --target 250

Then point your splat tool (Postshot, Nerfstudio `ns-process-data images`,
gsplat/COLMAP, Polycam/Luma upload, ...) at the images/ folder.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import cv2


def sharpness(path: Path) -> float:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return -1.0
    img = cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1])))
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", type=Path)
    ap.add_argument("--every", type=int, default=10, help="keep 1 of every N frames (default 10 = 3/s at 30 fps)")
    ap.add_argument("--target", type=int, help="aim for about this many images instead of --every")
    ap.add_argument("--out", type=Path, help="default: <recording>/images")
    args = ap.parse_args()

    frames = sorted((args.recording / "color").glob("*.*"))
    if not frames:
        sys.exit(f"No frames in {args.recording / 'color'}")
    every = max(1, len(frames) // args.target) if args.target else max(1, args.every)
    out = args.out or args.recording / "images"
    out.mkdir(parents=True, exist_ok=True)

    kept = 0
    for start in range(0, len(frames), every):
        window = frames[start:start + every]
        best = max(window, key=sharpness)
        shutil.copy2(best, out / best.name)
        kept += 1
        print(f"\r  {start + len(window)}/{len(frames)} scanned, {kept} kept", end="", flush=True)
    print(f"\nWrote {kept} images to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
