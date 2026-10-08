# services/generateTicket/_measure.py
"""Measure the template's own pixels, so the layout constants in
image_functions.py are read off the artwork rather than eyeballed.

Run this after replacing assets/knak_event_pass.jpeg with a new template, and
copy the numbers it prints into image_functions.py:

    python services/generateTicket/_measure.py

It reports:
  * the white QR card's exact bounds      -> _QR_CARD_MEASURED
  * the baked-in text blocks' bounds      -> the text boxes' left edges and the
                                             empty bands between them
  * the baked-in text's actual colors     -> COLOR_HEADLINE / COLOR_DETAIL
  * the Telegraf size matching each
    baked-in cap height                   -> SIZE_* start sizes

Nothing here runs in production - it is a calibration tool, not part of the
request path.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
TEMPLATE = HERE / "assets" / "knak_event_pass.jpeg"
FONTS = HERE / "assets" / "fonts"


def _bbox(mn, mx, y0, y1, x0, x1, pred, min_pixels_per_line=3):
    """Bounding box of pixels matching `pred` inside the given window.

    Rows and columns need more than a few matching pixels to count, so JPEG
    ringing and stray background speckle don't stretch the box.
    """
    m = pred(mn[y0:y1, x0:x1], mx[y0:y1, x0:x1])
    rows = np.nonzero(m.sum(axis=1) > min_pixels_per_line)[0]
    cols = np.nonzero(m.sum(axis=0) > min_pixels_per_line)[0]
    if not len(rows) or not len(cols):
        return None
    return (int(x0 + cols.min()), int(y0 + rows.min()),
            int(x0 + cols.max()), int(y0 + rows.max()))


def _mean_color(a, box, threshold):
    """Average color of the bright core of a text block, ignoring its glow."""
    x0, y0, x1, y1 = box
    sub = a[y0:y1 + 1, x0:x1 + 1]
    core = sub[sub.min(axis=2) > threshold]
    return tuple(int(c) for c in core.mean(axis=0).round()) if len(core) else None


def _telegraf_size_for_cap_height(font_file, target):
    """The Telegraf point size whose cap height matches `target` pixels."""
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    best = None
    for size in range(20, 400):
        font = ImageFont.truetype(str(FONTS / font_file), size)
        _, top, _, bottom = draw.textbbox((0, 0), "HOLDER", font=font)
        cap = bottom - top
        if best is None or abs(cap - target) < abs(best[1] - target):
            best = (size, cap)
    return best


def main() -> int:
    if not TEMPLATE.exists():
        print(f"Template not found: {TEMPLATE}")
        return 1

    im = Image.open(TEMPLATE).convert("RGB")
    a = np.asarray(im).astype(int)
    H, W, _ = a.shape
    mn, mx = a.min(axis=2), a.max(axis=2)
    print(f"template: {W}x{H}\n")

    # ---- the white QR card ------------------------------------------------
    # Solidly white AND near-neutral, so the pale green glow around the card
    # and the green nebula behind it are both excluded. A column only counts
    # if hundreds of its pixels match, which is true of the card and of
    # nothing else on the pass.
    white = (mn > 215) & ((mx - mn) < 25)
    window = np.zeros_like(white)
    window[int(H * 0.35):, :int(W * 0.50)] = True
    card = white & window
    cols = np.nonzero(card.sum(axis=0) > 300)[0]
    rows = np.nonzero(card.sum(axis=1) > 300)[0]
    if len(cols) and len(rows):
        box = (int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max()))
        print(f"_QR_CARD_MEASURED = {box}"
              f"   ({box[2] - box[0] + 1} x {box[3] - box[1] + 1})")

    # ---- the baked-in text blocks -----------------------------------------
    blocks = {
        "KNAK wordmark": ((60, 280, 300, 1000), lambda p, q: (p > 230) & ((q - p) < 20), 235),
        "EVENT PASS headline": ((300, 620, 100, 1900), lambda p, _: p > 200, 215),
        "HOLDER: label": ((930, 1120, 820, 1500), lambda p, q: (p > 150) & ((q - p) < 45), 170),
    }
    print()
    for label, (win, pred, core_threshold) in blocks.items():
        box = _bbox(mn, mx, *win, pred)
        if box is None:
            print(f"{label}: not found")
            continue
        cap = box[3] - box[1] + 1
        print(f"{label}: x {box[0]}..{box[2]}  y {box[1]}..{box[3]}  "
              f"(cap height {cap}px, color {_mean_color(a, box, core_threshold)})")

    # ---- Telegraf sizes matching those cap heights ------------------------
    holder = _bbox(mn, mx, 930, 1120, 820, 1500,
                   lambda p, q: (p > 150) & ((q - p) < 45))
    if holder:
        target = holder[3] - holder[1] + 1
        size, cap = _telegraf_size_for_cap_height("Telegraf-Regular.otf", target)
        print(f"\nSIZE_NAME = {size}   (Telegraf Regular, cap height {cap}px, "
              f"matching the HOLDER: label's {target}px)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
