# services/generateTicket/image_functions.py
"""Ticket image generation for the Generate Ticket flow step.

Composites four things onto the Knak event pass template: a QR code carrying the
person's Marketo lead id, the event name, the holder's name, and the event date
and location.

Two behaviors worth knowing about:

- The QR encodes `knak-marketo-id=<lead_id>` - the format the Knak event
  check-in app's camera scanner expects - so a badge printed from this pass can
  be scanned at the door directly.
- Every text field auto-shrinks and word-wraps to fit its own region rather than
  assuming a fixed length, so an unusually long event name or location still
  renders cleanly instead of overflowing into the artwork or getting clipped.

All coordinates below were measured off the template's own pixels (see
`_measure.py` in this folder for the script that produced them), not eyeballed.
"""

import io
import logging
import os
from pathlib import Path
from typing import List, NamedTuple, Tuple

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_H

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
ASSETS = HERE / "assets"
TEMPLATE_PATH = ASSETS / "knak_event_pass.jpeg"

# The pass owns its own copy of the mark, separate from the repo-root
# knak_logo.png that Marketo shows in the flow-step bar. Those are two different
# jobs: the icon is decoration at 32x32, this is the centre of a QR code a
# scanner has to read at a door. Sharing one file means swapping the admin icon
# silently changes every badge - and the coverage guard in generate_qr checks
# area, not shape, so a wordmark would sail through it.
LOGO_PATH = ASSETS / "knak_logo.png"

# Where the fonts live. Point FONT_DIR elsewhere for a shared volume or a
# system font directory.
FONT_DIR = Path(os.getenv("FONT_DIR") or (ASSETS / "fonts"))


class FontSet(NamedTuple):
    """A typeface the pass can be set in, with its own calibrated start sizes.

    The sizes are not interchangeable between faces. SIZE_NAME is whichever
    point size renders an 87px cap height in THAT face, because 87px is the
    measured cap height of the "HOLDER:" label baked into the template - that is
    what makes the holder's name line up with its own label. A different face
    has different metrics at the same point size, so each entry carries numbers
    derived for it by _measure.py.
    """
    label: str
    bold: Path
    regular: Path
    event_name: int
    name: int
    detail: int


# Telegraf is Knak's brand typeface, licensed from Pangram Pangram Foundry.
# That license does not permit redistributing the binaries, so the files are not
# in this repo - drop them into FONT_DIR and the pass upgrades to them on its
# own, no configuration. See assets/fonts/README.md.
#
# Chivo (SIL Open Font License) is the bundled fallback so the service works the
# moment it is cloned. It was picked by measuring candidates against Telegraf
# rather than by eye: at a matched cap height its regular sets 98% of Telegraf's
# width and its bold 93%, the closest of the open grotesques tried, so the
# layout boxes calibrated for Telegraf still hold.
_FONT_SETS = (
    FontSet("Telegraf", FONT_DIR / "Telegraf-Bold.otf", FONT_DIR / "Telegraf-Regular.otf",
            event_name=130, name=124, detail=110),
    FontSet("Chivo", FONT_DIR / "Chivo-Bold.ttf", FONT_DIR / "Chivo-Regular.ttf",
            event_name=128, name=122, detail=108),
)


def _active_font_set() -> FontSet:
    """The first font set whose files are actually present.

    Resolved per call rather than cached at import, so dropping Telegraf into
    FONT_DIR takes effect on the next pass instead of needing a restart.
    """
    for fonts in _FONT_SETS:
        if fonts.bold.exists() and fonts.regular.exists():
            return fonts
    # Nothing available. Return the fallback anyway so the caller fails on a
    # missing file with a path in the message, rather than on None here.
    return _FONT_SETS[-1]

# The prefix the Knak check-in app's scanner matches on. A bare number would be
# a collision risk - any unrelated QR code that happens to decode to a number
# could match a real lead id and check the wrong person in.
QR_PREFIX = "knak-marketo-id="

# Logo width/height as a fraction of the QR code size.
LOGO_SIZE_RATIO = 0.25
# White backing tile drawn behind the logo, as a multiple of the logo's own box.
# Just enough to clear the modules immediately around the mark so it reads as a
# deliberate tile rather than a smudge, matching how the template's own artwork
# places the k. mark straight onto the white.
LOGO_PLATE_MARGIN = 1.12

# Coverage thresholds against the code's own module area (excluding the
# quiet-zone border, since only real modules carry data). Real decoders stay
# reliable up to ~15.5% coverage and start failing around 16-17%, well below
# level H's theoretical ~30% recovery budget - which also has to cover print
# defects and camera angle, not just a logo.
COVERAGE_WARN = 0.16
COVERAGE_MAX = 0.17

# Sampled from the template's own rendered text pixels, so generated lines sit
# in the same palette as the baked-in ones rather than next to them.
COLOR_HEADLINE = (230, 254, 231)  # pale green-white, matching "EVENT PASS"
COLOR_DETAIL = (216, 221, 225)    # light cool gray, matching "HOLDER:"

# (x0, y0, x1, y1) boxes in the template's own 2524x1664 pixel space.
#
# The QR card's white area measures exactly (161, 949) - (773, 1583). QR_BOX
# grows that by a few px on every side: the card has rounded corners, and a
# sharp-cornered paste sized to fit inside that curve leaves a thin rounded
# sliver of the original card exposed at each corner, where the template's own
# baked-in placeholder QR (including its corner finder squares) shows through.
# Overpainting a few px onto the surrounding glow is imperceptible; a fragment
# of the old placeholder QR peeking through is not.
_QR_CARD_MEASURED = (161, 949, 773, 1583)
_QR_CARD_GROW = 6

# White left around the code inside that card, matching the template's own
# baked-in QR, whose modules sit 27-29px in from every edge of the card. That is
# roughly 1.6 modules rather than the 4 the QR spec asks for - see _qr_card for
# why that holds up here, and test_local.py for the decode checks that prove it.
QUIET_ZONE_PX = 28
# The floor that rounding the module size up is never allowed to eat into.
MIN_QUIET_ZONE_PX = 20
QR_BOX = (
    _QR_CARD_MEASURED[0] - _QR_CARD_GROW,
    _QR_CARD_MEASURED[1] - _QR_CARD_GROW,
    _QR_CARD_MEASURED[2] + _QR_CARD_GROW,
    _QR_CARD_MEASURED[3] + _QR_CARD_GROW,
)

# The event name sits in the empty band under the baked-in "EVENT PASS"
# headline (which occupies y 379-537), left-aligned to that headline's own left
# edge of x=142. The right edge stops short of the cheetah's face.
EVENT_NAME_BOX = (142, 585, 1750, 915)

# The holder column starts at x=873, the measured left edge of the baked-in
# "HOLDER:" label (y 984-1070), and the three lines fill the empty space from
# just under that label down to the bottom of the QR card.
NAME_BOX = (873, 1100, 1650, 1250)
DATE_BOX = (873, 1270, 1650, 1410)
LOCATION_BOX = (873, 1420, 1650, 1570)

# Start sizes live on each FontSet above, because they are per-typeface. The
# shape of the calibration is the same either way: the holder's name matches the
# 87px cap height of the baked-in "HOLDER:" label, the event name sits a step
# below the ~140px-cap headline, and the date and location a step below the name.


# ---------- QR generation ----------

def _render_qr_matrix(data: str, target_size: int, max_size: int):
    """Build the QR image from the raw module matrix, close to `target_size` and
    never wider than `max_size`.

    The matrix is drawn at one pixel per module into a two-color palette image,
    then upscaled by a whole-number factor. That skips both the per-module
    rectangle drawing and any color quantization, and it keeps every module
    exactly the same width - no rounding shimmer that scanners have to work
    around. qrcode's own make_image() antialiases when resized (e.g. via
    LANCZOS), turning crisp module edges into a soft gray blur; this avoids that
    entirely.

    border=0: the quiet zone is added afterwards in pixels by _qr_card, not in
    modules here. A lead id one digit longer can push the code up a version
    (29 modules to 33), and a quiet zone counted in modules would then change
    width on the pass for that person alone. Measured in pixels it stays put.
    """
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, border=0)
    qr.add_data(data)
    qr.make(fit=True)

    matrix = qr.get_matrix()  # modules only, no border
    grid = len(matrix)
    base = Image.frombytes(
        "P", (grid, grid),
        bytes(1 if cell else 0 for row in matrix for cell in row))
    base.putpalette([255, 255, 255, 0, 0, 0])  # index 0 = white, 1 = black

    # A whole number of pixels per module, so every module stays exactly the
    # same width. Rounded to the nearest rather than floored: flooring costs a
    # whole pixel per module, which at 33 modules shrinks the block by 33px and
    # visibly changes the quiet zone for one lead id alone. Then clamped, so
    # rounding up can never push the block past the space it has.
    box = max(1, round(target_size / grid))
    if grid * box > max_size:
        box = max(1, max_size // grid)
    img = base.resize((grid * box, grid * box), Image.NEAREST)
    return img, qr.modules_count, box


def generate_qr(lead_id, target_size: int = 557, max_size: int = 573) -> Image.Image:
    """The bare QR modules for this lead, with the Knak mark on a white tile in
    the centre. No quiet zone - _qr_card adds that in pixels."""
    img, modules, box = _render_qr_matrix(f"{QR_PREFIX}{lead_id}", target_size, max_size)
    qr_width, qr_height = img.size
    img = img.convert("RGB")

    logo_target = int(min(qr_width, qr_height) * LOGO_SIZE_RATIO)
    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo.thumbnail((logo_target, logo_target), Image.LANCZOS)

    # White tile behind the mark, with a small margin so the modules don't crowd
    # it. Rounded to echo the pass's own rounded QR card.
    plate_w = int(logo.size[0] * LOGO_PLATE_MARGIN)
    plate_h = int(logo.size[1] * LOGO_PLATE_MARGIN)
    plate = Image.new("RGBA", (plate_w, plate_h), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        (0, 0, plate_w - 1, plate_h - 1),
        radius=max(2, plate_w // 8),
        fill=(255, 255, 255, 255),
    )

    # Coverage is measured against the modules, which is the whole image here,
    # since only real modules carry data.
    code_px = float(modules * box)
    coverage = (plate_w * plate_h) / (code_px * code_px)
    if coverage > COVERAGE_MAX:
        raise ValueError(
            f"Logo would cover {coverage:.0%} of the QR code's modules, past the "
            f"~{COVERAGE_MAX:.0%} that stays reliably scannable - lower LOGO_SIZE_RATIO."
        )
    if coverage > COVERAGE_WARN:
        logger.warning(
            f"Logo covers {coverage:.0%} of the QR code for lead {lead_id} - "
            "close to the scan-reliability ceiling.")

    img.paste(plate, ((qr_width - plate_w) // 2, (qr_height - plate_h) // 2), plate)
    img.paste(logo, ((qr_width - logo.size[0]) // 2, (qr_height - logo.size[1]) // 2), logo)

    return img.convert("RGB")


def _qr_card(lead_id) -> Image.Image:
    """The QR laid onto a white card, sized to sit where the template's own does.

    The code fills the card the way the template's baked-in artwork does, with
    the same QUIET_ZONE_PX of white around it, rather than the 4-module quiet
    zone the QR spec asks for. That is a deliberate tradeoff and the reason
    test_local.py decodes every generated pass at four sizes: the card is a
    bright white island on a dark background with a glowing border, so a scanner
    finds its edges from the card itself, not from the module-level quiet zone.

    The card is 613x635 - slightly taller than it is wide - so the square code
    is centred rather than stretched to fill it. Stretching would make every
    module a little taller than it is wide, which is exactly the distortion a
    phone camera at an angle also introduces; there is no reason to spend any of
    that tolerance here. The canvas is white throughout, so it covers the
    baked-in placeholder QR (and its rounded corners) completely.
    """
    card_w = QR_BOX[2] - QR_BOX[0]
    card_h = QR_BOX[3] - QR_BOX[1]

    # The quiet zone is measured against the card's visible white area, which is
    # what the eye reads, not against QR_BOX's few px of corner overshoot.
    visible = min(_QR_CARD_MEASURED[2] - _QR_CARD_MEASURED[0],
                  _QR_CARD_MEASURED[3] - _QR_CARD_MEASURED[1])
    qr = generate_qr(lead_id,
                     target_size=visible - 2 * QUIET_ZONE_PX,
                     max_size=visible - 2 * MIN_QUIET_ZONE_PX)

    canvas = Image.new("RGB", (card_w, card_h), (255, 255, 255))
    canvas.paste(qr, ((card_w - qr.size[0]) // 2, (card_h - qr.size[1]) // 2))
    return canvas


# ---------- Text fitting ----------

def _font(path, size) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _text_size(draw, text, font) -> Tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _wrap_to_width(draw, text, font, max_width) -> List[str]:
    """Greedy word-wrap: fits as many words per line as max_width allows."""
    words = text.split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _text_size(draw, candidate, font)[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_text(draw, text, font_path, box, max_lines, start_size, min_size):
    """Largest font size (down to min_size, in steps of 2) at which `text` wraps
    into at most `max_lines` lines and fits within the box.

    If even min_size doesn't fit within max_lines, wrapping still continues past
    max_lines rather than dropping words - a rare, unusually long value renders a
    little taller than its box instead of silently losing part of someone's name.
    """
    box_w, box_h = box[2] - box[0], box[3] - box[1]

    for size in range(start_size, min_size - 1, -2):
        font = _font(font_path, size)
        lines = _wrap_to_width(draw, text, font, box_w)
        line_h = _text_size(draw, "Ag", font)[1] * 1.15
        if len(lines) <= max_lines and line_h * len(lines) <= box_h:
            return font, lines, line_h

    font = _font(font_path, min_size)
    lines = _wrap_to_width(draw, text, font, box_w)
    line_h = _text_size(draw, "Ag", font)[1] * 1.15
    return font, lines, line_h


def _draw_fitted(draw, text, font_path, box, max_lines, start_size, color, min_size):
    font, lines, line_h = _fit_text(draw, text, font_path, box, max_lines, start_size, min_size)
    y = box[1]
    for line in lines:
        draw.text((box[0], y), line, font=font, fill=color)
        y += line_h


# ---------- Public entry point ----------

def generate_ticket_image(full_name: str, event_name: str, event_date: str,
                          event_location: str, lead_id) -> Tuple[bytes, str, str]:
    """Build this person's pass: the branded template + a QR encoding their lead
    id + the event details, fit to the template's own layout.

    Returns (image_bytes, mime_type, file_extension).
    """
    fonts = _active_font_set()
    bg = Image.open(TEMPLATE_PATH).convert("RGB")
    draw = ImageDraw.Draw(bg)

    # NEAREST, not LANCZOS - the code is already rendered at crisp, flat module
    # edges, and a smooth resize right before compositing would re-blur every
    # edge it just fixed.
    bg.paste(_qr_card(lead_id), (QR_BOX[0], QR_BOX[1]))

    # The template's own text is set in caps, so the event name, date and
    # location follow it - that way any value looks on-brand, not just the ones
    # that happen to be typed in caps already. The holder's name keeps its
    # natural case, because a person's name is not a design element.
    _draw_fitted(draw, (event_name or "Event").upper(), fonts.bold, EVENT_NAME_BOX,
                 max_lines=2, start_size=fonts.event_name, color=COLOR_HEADLINE, min_size=54)
    # Two lines allowed for the name only. A long double-barrelled name wraps at
    # a readable size instead of shrinking all the way to min_size chasing a
    # single line it will never fit on.
    _draw_fitted(draw, full_name or "Guest", fonts.regular, NAME_BOX,
                 max_lines=2, start_size=fonts.name, color=COLOR_DETAIL, min_size=52)
    _draw_fitted(draw, (event_date or "").upper(), fonts.regular, DATE_BOX,
                 max_lines=1, start_size=fonts.detail, color=COLOR_DETAIL, min_size=48)
    _draw_fitted(draw, (event_location or "").upper(), fonts.regular, LOCATION_BOX,
                 max_lines=1, start_size=fonts.detail, color=COLOR_DETAIL, min_size=48)

    # JPEG, not PNG: the nebula background is photographic, which PNG's lossless
    # compression handles poorly (several MB per pass). Quality 90 keeps it
    # around 500KB with no visible difference and the QR still scans - which
    # matters for something embedded in a confirmation email and served at scale
    # from Design Studio.
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg", "jpg"


def self_check() -> List[str]:
    """Render a throwaway pass to prove the whole image pipeline works.

    A missing template, an unreadable font, a missing Pillow or qrcode install
    or a logo that has grown past the scannable coverage ceiling would otherwise
    only surface mid-batch, one failed lead at a time. /status calls this so all
    of it fails in one place, before anyone installs the service in Marketo.
    """
    problems: List[str] = []

    fonts = _active_font_set()
    for label, path in (("template", TEMPLATE_PATH), ("logo", LOGO_PATH),
                        (f"{fonts.label} Bold", fonts.bold),
                        (f"{fonts.label} Regular", fonts.regular)):
        if not Path(path).exists():
            problems.append(f"Missing {label}: {path}")
    if problems:
        return problems

    try:
        image_bytes, _, _ = generate_ticket_image(
            "Self Check", "Status Endpoint", "JAN 1, 2026", "TORONTO, ON", 0)
        if len(image_bytes) < 10_000:
            problems.append(f"Rendered pass is implausibly small ({len(image_bytes)} bytes)")
    except Exception as e:
        problems.append(f"Rendering a test pass failed: {type(e).__name__}: {e}")

    return problems
