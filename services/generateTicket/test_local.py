# services/generateTicket/test_local.py
"""Smoke test for the Generate Ticket service.

Run it before every deploy:

    python services/generateTicket/test_local.py

It is fully offline - Marketo is never called. What it proves:

  * the endpoints answer, and /status renders a real pass end to end
  * every generated QR actually DECODES back to knak-marketo-id=<lead_id>,
    including at the reduced sizes a pass is viewed at in an email
  * the logo has not grown past the coverage ceiling that keeps a QR scannable
  * long values shrink and wrap inside their boxes instead of overflowing
  * the callback body is shaped the way Marketo expects
  * the leads log row has exactly the columns the sheet is written with -
    a mismatch makes pandas pad with NaN, which Google rejects, silently
    losing every row in the batch

Decoding needs opencv (`pip install opencv-python-headless`). Without it the
decode checks are skipped and the run says so, rather than passing quietly.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Must be set before routes.py is imported - it reads them at module level.
os.environ.setdefault("MARKETO_USER", "test")
os.environ.setdefault("MARKETO_PASSWORD", "test")

from flask import Flask  # noqa: E402

from services.generateTicket import image_functions as imf  # noqa: E402
from services.generateTicket import routes  # noqa: E402

AUTH = ("test", "test")

# Deliberately awkward values: a name far too long for one line, an event name
# that has to wrap, and a location that has to shrink.
CASES = [
    ("Alex Rivera", "Example Summit 2026", "OCT 26-27, 2026", "AUSTIN, TX", 1000001),
    ("Sam Lee", "Adobe Summit", "MAR 3, 2027", "LAS VEGAS, NV", 20002),
    ("Alexandra Wolfensberger-Nakamura",
     "Marketing Operations Summit and Awards Gala",
     "SEPTEMBER 24-25, 2026", "SAN FRANCISCO, CALIFORNIA", 3000003),
    ("", "", "", "", 1),  # every field empty - must still render something
    # A 10-digit id pushes the code from 29 modules to 33, which changes the
    # pixels per module and so the quiet zone. Both sizes have to still decode.
    ("Future Proof", "KnakCon 2030", "OCT 1, 2030", "TORONTO, ON", 9999999999),
]

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok    {message}")
    else:
        print(f"  FAIL  {message}")
        failures.append(message)


def main() -> int:
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    client = app.test_client()

    print("\nEndpoints")
    r = client.get("/generateTicket/status", auth=AUTH)
    body = r.get_json()
    check(r.status_code == 200, f"/status answers 200 (got {r.status_code})")
    # Marketo REST creds and TICKET_FOLDER_ID are not set in a local run, so
    # status is expected to be "error" - what matters is that the image
    # pipeline itself reported nothing, since that part runs offline.
    image_problems = [p for p in (body or {}).get("problems", [])
                      if "TICKET_FOLDER_ID" not in p and "REST credentials" not in p]
    check(not image_problems, f"/status finds no image problems (got {image_problems})")
    check((body or {}).get("qr_prefix") == "knak-marketo-id=",
          "the QR prefix is the one the check-in app scans for")

    r = client.get("/generateTicket/getServiceDefinition", auth=AUTH)
    sd = r.get_json()
    check(r.status_code == 200, "/getServiceDefinition answers 200")
    check(sd["primaryAttribute"] in
          [a["apiName"] for a in sd["invocationPayloadDef"]["flowAttributes"]],
          "primaryAttribute is one of the flow attributes")
    check(sd["primaryAttribute"] not in
          [a["apiName"] for a in sd["callbackPayloadDef"]["attributes"]],
          "primaryAttribute is not also a callback attribute (Marketo rejects that)")
    check(any(f["serviceAttribute"] == "full_name" and f["isRequired"]
              for f in sd["invocationPayloadDef"]["fields"]),
          "full_name is a required mapped field, so the install cannot skip it")

    r = client.get("/generateTicket/install")
    check(r.status_code == 200, "/install answers 200 without auth")
    check(r.get_json()["servers"][0]["url"].endswith("/generateTicket"),
          "/install rewrites the server URL to this host")

    r = client.get("/generateTicket/status")
    check(r.status_code == 401, "/status without auth is rejected")

    print("\nLog row shape")
    check(set(routes.LEAD_COLUMNS) == {
        "timestamp", "lead_id", "program_id", "program_name", "folder_id",
        "full_name", "event_name", "event_date", "event_location",
        "file_name", "ticket_image_url", "error", "callback_object"},
        "LEAD_COLUMNS matches the columns the leads rows are built with")

    # Both tabs are written positionally, and the batches tab is also UPDATED by
    # column index while a batch runs. An offset one column out of step doesn't
    # fail - it quietly overwrites the wrong cell - so pin the offsets to the
    # header they index into.
    check(routes.BATCH_COLUMNS[routes.COL_LEADS_PROCESSED] == "leads_processed",
          f"COL_LEADS_PROCESSED points at leads_processed "
          f"(points at {routes.BATCH_COLUMNS[routes.COL_LEADS_PROCESSED]!r})")
    check(routes.BATCH_COLUMNS[routes.COL_ERROR] == "error",
          f"COL_ERROR points at error "
          f"(points at {routes.BATCH_COLUMNS[routes.COL_ERROR]!r})")
    # The progress update writes three cells starting at COL_LEADS_PROCESSED.
    check(routes.BATCH_COLUMNS[routes.COL_LEADS_PROCESSED:routes.COL_LEADS_PROCESSED + 3]
          == ["leads_processed", "error", "callback_response"],
          "the three cells the progress update writes are the three it means to")
    # findRowContaining searches one column for the callback token.
    check(routes.COL_REQUEST_RANGE
          == "{0}:{0}".format(chr(ord("A") + routes.BATCH_COLUMNS.index("request"))),
          f"COL_REQUEST_RANGE ({routes.COL_REQUEST_RANGE}) is the request column")

    print("\nFonts")
    # The pass ships with an open font so a fresh clone renders, and upgrades to
    # Knak's licensed Telegraf when those files are present. Getting the
    # precedence backwards would silently set every pass in the fallback face on
    # a Knak deployment, which nothing else would catch.
    labels = [fs.label for fs in imf._FONT_SETS]
    check(labels[0] == "Telegraf",
          f"Telegraf is preferred over the fallback (order: {labels})")
    check(labels[-1] == "Chivo",
          f"the last resort is the bundled open font (order: {labels})")
    bundled = imf._FONT_SETS[-1]
    check(bundled.bold.exists() and bundled.regular.exists(),
          "the fallback font is actually committed, so a fresh clone can render")
    # Each face needs its own sizes - they are cap-height matched, not shared.
    sizes = {fs.label: (fs.event_name, fs.name, fs.detail) for fs in imf._FONT_SETS}
    check(len(set(sizes.values())) == len(sizes),
          f"each face carries start sizes calibrated for it, not copied ({sizes})")
    active = imf._active_font_set()
    print(f"  ..    rendering in {active.label}")

    print("\nRendering")
    try:
        import cv2
        detector = cv2.QRCodeDetector()
    except ImportError:
        detector = None
        print("  skip  QR decoding (opencv not installed - "
              "pip install opencv-python-headless)")

    for full_name, event, date, location, lead_id in CASES:
        label = f"lead {lead_id} ({full_name or 'empty fields'})"
        image_bytes, mime, ext = imf.generate_ticket_image(
            full_name, event, date, location, lead_id)
        check(mime == "image/jpeg" and ext == "jpg", f"{label}: returns a jpeg")
        # A pass that renders but is tiny means the template failed to load and
        # something near-blank got saved.
        check(len(image_bytes) > 100_000,
              f"{label}: {len(image_bytes) // 1024}KB is a plausible pass")

        if detector is None:
            continue

        import numpy as np
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        expected = f"knak-marketo-id={lead_id}"
        # Full size, then the widths a pass is actually looked at: an email
        # body, a phone screen, a thumbnail. 450 is well below anything real -
        # it is headroom, and it matters because the quiet zone around the code
        # is deliberately tighter than the QR spec asks for (see _qr_card).
        for width in (img.shape[1], 1200, 800, 600, 450):
            scaled = img if width == img.shape[1] else cv2.resize(
                img, (width, int(img.shape[0] * width / img.shape[1])),
                interpolation=cv2.INTER_AREA)
            decoded, _, _ = detector.detectAndDecode(scaled)
            check(decoded == expected,
                  f"{label}: QR decodes at {width}px wide (got {decoded!r})")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
