# services/generateTicket/routes.py
"""Marketo Self-Service Flow Step: generate a personalized Knak event pass.

For every lead in the batch this builds a branded ticket image carrying a QR
code of their Marketo lead id, uploads it to a Design Studio folder named after
the calling program, and writes the resulting URL to a program member field.

The QR encodes `knak-marketo-id=<lead_id>` - the format the Knak event check-in
app's camera scanner expects - so a badge printed from this ticket can be
scanned at the door without any further work.

Unlike hearSource, this service also talks to the Marketo REST API (see
marketo_functions.py), because there is no way to create a Design Studio folder,
upload a file or write a program member field through the SSFS callback.
"""
import json
import hmac
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd
import pytz
import requests
from flask import Blueprint, Response, jsonify, request, send_file

import googlesheets_functions
import marketo_functions

from . import image_functions

base = "generateTicket"
bp = Blueprint(base, __name__, url_prefix=f"/{base}")

# ---------- CONFIG ----------
SHEET_LEADS = f"{base}Leads"
SHEET_BATCHES = f"{base}Batches"
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
# Only used to build clickable lead links in the Sheets log. The default
# is a placeholder, not a real instance - an unset value gives an obviously
# broken link rather than one pointing at somebody else's subscription.
MARKETO_INSTANCE = os.getenv("MARKETO_INSTANCE", "app-xxxxx")
pacific = pytz.timezone("America/Los_Angeles")
MAX_CELL = 50000    # Sheets' absolute limit
SAFE_SLICE = 48000  # leave UTF-8 head-room

# Marketo allows 10 concurrent API calls, and every lead here costs an upload,
# so going wider than that just queues behind the rate limiter.
MAX_WORKERS = 10

# How often the leads_processed cell is refreshed while a batch runs. Sheets
# allows ~60 writes/minute, so don't drop this much lower.
PROGRESS_INTERVAL_SECONDS = 10

# Program member field that receives the Design Studio URL of the ticket. It
# must already exist in Admin -> Program Member Fields; the API cannot create
# it. Overridable so a subscription that named the field differently does not
# need a code change.
TICKET_FIELD = os.getenv("TICKET_FIELD", "ticketImage")

# The batches tab is written positionally, like the leads tab, so this list IS
# the header row - put exactly these labels in row 1, in this order. Every column
# offset below is derived from it rather than hard-coded, because a progress
# update writes by index: a constant that drifts one column out of step with the
# row silently overwrites the wrong cell instead of failing.
BATCH_COLUMNS = [
    "timestamp", "leads", "leads_processed", "error", "callback_response", "request",
]
COL_LEADS_PROCESSED = BATCH_COLUMNS.index("leads_processed")
COL_ERROR = BATCH_COLUMNS.index("error")
# Marketo always puts "token" first in the payload, so it is always inside the
# first request cell - no need to search the overflow columns.
COL_REQUEST_RANGE = "{0}:{0}".format(chr(ord("A") + BATCH_COLUMNS.index("request")))

# The leads tab is written positionally, so every row must have exactly these
# columns in this order - including rows from the per-lead error path, which
# would otherwise be narrower and make pandas pad the frame with NaN. NaN is not
# valid JSON and Google rejects the whole append, losing every row in the batch.
LEAD_COLUMNS = [
    "timestamp", "lead_id", "program_id", "program_name", "folder_id",
    "full_name", "event_name", "event_date", "event_location",
    "file_name", "ticket_image_url", "error", "callback_object",
]

# Names declared in the service definition below.
IN_FIELD = "full_name"            # mapped lead field (invocation)
ATTR_EVENT_NAME = "event_name"    # primaryAttribute; also a flow-step input
ATTR_EVENT_DATE = "event_date"
ATTR_EVENT_LOCATION = "event_location"


# ---------- DATACLASSES ----------
@dataclass
class LeadData:
    """leadData carries only the id - the ticket URL goes to the program member
    field via the REST API, not through the callback, because the callback has
    no notion of which program the step ran in."""
    id: int


@dataclass
class ActivityData:
    event_name_value: str
    event_date: str
    event_location: str
    full_name: str = ""
    program_name: str = ""
    ticket_image_url: str = ""
    success: bool = True
    ticket_error: str = ""


# ---------- AUTH ----------
def _check_auth(username, password):
    """Basic-auth check. Refuses everything when the credentials are unset.

    .env.example ships MARKETO_USER and MARKETO_PASSWORD empty, so a deploy
    that never filled them in used to accept a blank username and password -
    "" == "" is true, and the service was open to anyone who found the URL.
    Refusing is the right failure: the flow step stops, which is loud.

    compare_digest rather than == so the comparison does not finish early on
    the first wrong character.
    """
    expected_user = os.getenv("MARKETO_USER") or ""
    expected_password = os.getenv("MARKETO_PASSWORD") or ""
    if not expected_user or not expected_password:
        return False
    return (hmac.compare_digest(username or "", expected_user)
            and hmac.compare_digest(password or "", expected_password))


@bp.before_request
def require_basic_auth():
    if request.path in (
        f"/{base}/install",
        f"/{base}/serviceIcon",
        f"/{base}/brandIcon",
    ):
        return
    if request.path.startswith(f"/{base}/status") or \
       request.path.startswith(f"/{base}/submitAsyncAction"):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm=Knak Generate Ticket'}
            )


# ---------- HELPERS ----------
def _split_long_text(col_name: str, value: str | None) -> dict[str, str]:
    """Return {col_name: value} unless it would overflow a Sheets cell."""
    if not value:
        return {col_name: ""}
    b = value.encode("utf-8")
    if len(b) <= MAX_CELL:
        return {col_name: value}

    chunks = [b[i:i + SAFE_SLICE].decode("utf-8", "ignore")
              for i in range(0, len(b), SAFE_SLICE)]
    return {f"{col_name}{'' if i == 0 else '_' + str(i + 1)}": s
            for i, s in enumerate(chunks)}


def _safe_file_part(value: str) -> str:
    """Trim a value down to something safe for a Marketo asset name."""
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", value or "").strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:60] or "guest"


# ---------- STATIC ICONS ----------
# Repo-root logo, shared with the other services. Marketo scales it down.
ICON_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "knak_logo.png")


@bp.route("/serviceIcon")
def service_icon():
    return send_file(ICON_FILE, mimetype="image/png")


@bp.route("/brandIcon")
def brand_icon():
    return send_file(ICON_FILE, mimetype="image/png")


# ---------- SSFS ENDPOINTS ----------
@bp.route("/submitAsyncAction", methods=["POST"])
def submit_async_action():
    """Acknowledge the batch immediately, then process it in the background.

    Marketo holds the connection open waiting for this response. Generating and
    uploading a 1000-lead batch takes minutes, which trips Marketo's read
    timeout (SocketTimeoutException) and fails the whole step. The 202 means
    "accepted, I will call you back" - so send it straight away and do the work
    after.
    """
    timestamp = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S")
    data = request.get_json(force=True)
    object_data = data.get("objectData", []) if isinstance(data, dict) else []

    # ---- log the batch the moment it arrives, before the 202 --------------
    # The request payload is on the sheet before any work starts, so a crash, a
    # restart or a hung upload still leaves a record of what came in.
    batch_row_number = None
    # Marketo's callback token is unique per invocation and already sits in the
    # request payload we log, so it doubles as this batch's id - no extra column.
    batch_token = str(data.get("token", "")) if isinstance(data, dict) else ""
    # Built in BATCH_COLUMNS order - appendRow2Sheet writes dict values
    # positionally, so the insertion order here is the column order on the sheet.
    batch_row = {
        "timestamp": timestamp,
        "leads": len(object_data),
        "leads_processed": 0,
        "error": "",
        "callback_response": "",
    }
    batch_row.update(_split_long_text("request", json.dumps(data)))
    try:
        googlesheets_functions.appendRow2Sheet(batch_row, SHEET_BATCHES, SPREADSHEET_ID)
        # Do NOT trust the row number the append reports - two batches arriving
        # in the same second can both be told they landed on the same row, and
        # then one writes its progress over the other's. Locate our own row by
        # finding the token inside the request payload we just wrote.
        if batch_token:
            batch_row_number = googlesheets_functions.findRowContaining(
                SHEET_BATCHES, SPREADSHEET_ID, COL_REQUEST_RANGE, batch_token)
        if batch_row_number is None:
            print(f"[{base}] could not locate this batch's row"
                  f"{' (no token in payload)' if not batch_token else ''};"
                  " progress and outcome will not be logged")
    except Exception as gs_err:
        print("Sheets error writing the incoming batch row:", gs_err)

    worker = threading.Thread(
        target=_process_batch,
        args=(data, timestamp, batch_row_number),
        name=f"{base}-batch-{batch_token[:12] or 'unknown'}",
        daemon=True,
    )
    worker.start()
    print(f"[{base}] accepted {len(object_data)} lead(s), processing in {worker.name}")

    # Accepted. The result reaches Marketo via the callback, not this response.
    return ("", 202)


def _process_batch(data: dict, timestamp: str, batch_row_number) -> None:
    """Generate, upload and record a whole batch, then post the callback. Runs
    off the request thread, so it must never raise into the caller and cannot
    touch the Flask request."""
    rows_leads: list[dict] = []
    cb_response = ""
    object_data = []

    try:
        object_data = data.get("objectData", [])

        # ---- resolve the calling program once for the whole batch ----------
        # Marketo only sends campaignId, so the program has to be looked up,
        # then its Design Studio folder found or created. A failure here hits
        # every lead, so record it and let each lead report it rather than
        # dropping the batch silently.
        program_id = None
        program_name = ""
        folder_id = None
        setup_error = ""
        try:
            program_id, program_name = marketo_functions.get_program_for_campaign(
                data.get("campaignId"))
            folder_id = marketo_functions.get_or_create_folder(
                program_name, marketo_functions.TICKET_FOLDER_ID)
            print(f"[{base}] program {program_name} ({program_id}), folder {folder_id}")
        except Exception as setup_err:
            setup_error = f"{setup_err}"
            print(f"[{base}] batch setup failed: {setup_error}")

        # ---- progress counter, refreshed on the sheet as leads complete -----
        progress = {"done": 0, "last_write": 0.0}
        progress_lock = threading.Lock()

        def note_progress() -> None:
            """Count one finished lead and, at most every PROGRESS_INTERVAL_SECONDS,
            push the running total to the sheet. Never raises."""
            try:
                with progress_lock:
                    progress["done"] += 1
                    done = progress["done"]
                    now = time.monotonic()
                    due = (now - progress["last_write"]) >= PROGRESS_INTERVAL_SECONDS
                    if due:
                        progress["last_write"] = now
                if due:
                    googlesheets_functions.updateCells(
                        [done], SHEET_BATCHES, SPREADSHEET_ID,
                        batch_row_number, COL_LEADS_PROCESSED)
            except Exception as gs_err:
                print("Sheets error updating progress:", gs_err)

        def process(obj: dict) -> dict:
            """Build and upload one person's ticket.

            Never raises. One bad lead must not take down the batch, so even
            reading the payload happens inside the try - a malformed objectData
            entry would otherwise blow up here, outside any handler, and
            ThreadPoolExecutor would re-raise it for the whole batch.
            """
            result = {
                "lead_id": None, "full_name": "", "event_name": "",
                "event_date": "", "event_location": "",
                "file_name": "", "ticket_image_url": "", "error": "",
            }
            try:
                octx = obj.get("objectContext", {}) or {}
                ctx = obj.get("flowStepContext", {}) or {}
                result["lead_id"] = octx.get("id")
                # Mapped at install time via invocationPayloadDef.fields, so
                # this is the service attribute name, not a Marketo API name.
                result["full_name"] = str(octx.get(IN_FIELD, "") or "")
                result["event_name"] = str(ctx.get(ATTR_EVENT_NAME, "") or "")
                result["event_date"] = str(ctx.get(ATTR_EVENT_DATE, "") or "")
                result["event_location"] = str(ctx.get(ATTR_EVENT_LOCATION, "") or "")

                if setup_error:
                    raise RuntimeError(setup_error)

                image_bytes, mime_type, ext = image_functions.generate_ticket_image(
                    full_name=result["full_name"],
                    event_name=result["event_name"],
                    event_date=result["event_date"],
                    event_location=result["event_location"],
                    lead_id=result["lead_id"],
                )

                file_name = f"ticket-{result['lead_id']}-{_safe_file_part(result['full_name'])}.{ext}"
                result["file_name"] = file_name
                result["ticket_image_url"] = marketo_functions.upload_file(
                    file_name, image_bytes, folder_id,
                    mime_type=mime_type,
                    description=f"{result['event_name']} ticket for {result['full_name']}",
                )
                return result

            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
                # Per-lead failures need to reach the terminal too - the only
                # other copy lives in a Sheets row that might not get written.
                print(f"[{base}] lead {result['lead_id']} failed: {type(e).__name__}: {e}")
                return result

            finally:
                note_progress()

        if object_data:
            workers = max(1, min(MAX_WORKERS, len(object_data)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(process, object_data))
        else:
            results = []

        # ---- write the ticket URL to the program member field ---------------
        # Batched at 300 per call by sync_program_member_field. That endpoint
        # updates existing members only, so anyone who is not a member of the
        # program comes back rejected and has to count as a failed lead - their
        # ticket exists but was never attached to them.
        field_errors: dict = {}
        pending_writes = [
            (int(r["lead_id"]), r["ticket_image_url"])
            for r in results
            if r["ticket_image_url"] and r["lead_id"] not in ("", None)
        ]
        if pending_writes and program_id:
            try:
                field_errors = marketo_functions.sync_program_member_field(
                    program_id, TICKET_FIELD, pending_writes)
            except Exception as sync_err:
                print(f"[{base}] program member write failed wholesale: {sync_err}")
                field_errors = {lid: f"{sync_err}" for lid, _ in pending_writes}

        # ---- build the callback objects and the log rows --------------------
        callback_objects = []
        for r in results:
            error = r["error"]
            try:
                field_err = field_errors.get(int(r["lead_id"]))
            except (TypeError, ValueError):
                field_err = None
            if field_err and not error:
                error = field_err

            callback_object = {
                "leadData": asdict(LeadData(id=r["lead_id"])),
                "activityData": asdict(ActivityData(
                    event_name_value=r["event_name"],
                    event_date=r["event_date"],
                    event_location=r["event_location"],
                    full_name=r["full_name"],
                    program_name=program_name,
                    ticket_image_url=r["ticket_image_url"],
                    success=not error,
                    ticket_error=error[:1000],
                )),
            }
            callback_objects.append(callback_object)

            rows_leads.append({
                "timestamp": timestamp,
                "lead_id": f"https://{MARKETO_INSTANCE}.marketo.com/leadDatabase/loadLeadDetail?leadId={r['lead_id']}",
                "program_id": program_id or "",
                "program_name": program_name,
                "folder_id": folder_id or "",
                "full_name": r["full_name"],
                "event_name": r["event_name"],
                "event_date": r["event_date"],
                "event_location": r["event_location"],
                "file_name": r["file_name"],
                "ticket_image_url": r["ticket_image_url"],
                "error": error,
                # Exactly what Marketo was told about this one person. The batch
                # row logs the whole payload, but reading one lead out of a
                # 1000-lead blob is not something anyone wants to do while
                # debugging. JSON rather than a Python repr, so it can be pasted
                # straight into a parser. ticket_error is already capped at
                # 1000 chars, so this stays far inside a Sheets cell.
                "callback_object": json.dumps(callback_object, ensure_ascii=False),
            })

        # ─────────── ONE callback back to Marketo ────────────
        # The callback token is single-use, so every lead in the batch has to go
        # in this one request.
        r = requests.post(
            data["callbackUrl"],
            headers={
                "x-api-key": data["apiCallBackKey"],
                "x-callback-token": data["token"],
                "Content-Type": "application/json"
            },
            # munchkinId is required by the callback schema but Marketo does
            # not read it - the x-callback-token header is what identifies
            # the invocation. A constant is honest about that; a real
            # munchkin id here would imply it mattered.
            json={"munchkinId": "123", "objectData": callback_objects},
            timeout=120
        )

        cb_response = r.text

        # ---- fill in the outcome on the row written when the batch arrived ----
        try:
            googlesheets_functions.writeDF2Sheet(
                pd.DataFrame(rows_leads, columns=LEAD_COLUMNS), SHEET_LEADS, SPREADSHEET_ID)

            failed = sum(1 for row in rows_leads if row["error"])
            if setup_error:
                batch_error = setup_error
            elif not r.ok:
                batch_error = f"Callback HTTP {r.status_code}"
            elif failed:
                batch_error = f"{failed} of {len(rows_leads)} leads failed"
            else:
                batch_error = ""

            googlesheets_functions.updateCells(
                [progress["done"], batch_error, cb_response],
                SHEET_BATCHES, SPREADSHEET_ID, batch_row_number, COL_LEADS_PROCESSED)
        except Exception as gs_err:
            print("Sheets logging error:", gs_err)

        if not r.ok:
            print(f"[{base}] callback to Marketo failed: HTTP {r.status_code} {r.text[:200]}")
        return

    except Exception as e:
        # Print it too - if the Sheets write below also fails, this is the only
        # place the error survives.
        # The trace goes to the log, where an operator can read it. Only the
        # message rides into Marketo: an activity field is shown to anyone with
        # access to the lead, and a trace leaks file paths and internals.
        traceback.print_exc()
        error_text = f"{type(e).__name__}: {e}"

        try:
            # Whatever leads did get built are worth keeping - a failure in the
            # callback must not throw away a batch of real uploaded tickets.
            if rows_leads:
                googlesheets_functions.writeDF2Sheet(
                    pd.DataFrame(rows_leads, columns=LEAD_COLUMNS), SHEET_LEADS, SPREADSHEET_ID)

            if batch_row_number:
                # The row is already on the sheet with the request payload -
                # just record how it failed.
                googlesheets_functions.updateCells(
                    [error_text, cb_response],
                    SHEET_BATCHES, SPREADSHEET_ID, batch_row_number, COL_ERROR)
            else:
                # We failed before the row was written, so append one now.
                fail_row = {
                    "timestamp": timestamp,
                    "leads": len(object_data),
                    "leads_processed": 0,
                    "error": error_text,
                    "callback_response": cb_response,
                }
                fail_row |= _split_long_text("request", json.dumps(data, ensure_ascii=False))
                googlesheets_functions.writeDF2Sheet(
                    pd.DataFrame([fail_row]), SHEET_BATCHES, SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets error while logging fatal failure:", gs_err)

        return


@bp.route("/getServiceDefinition")
def get_service_definition():
    return jsonify({
        "apiName": "generate-ticket",
        "i18n": {
            "en_US": {
                "name": "Generate Ticket",
                "description": ("Generates a personalized event pass image with a check-in QR code, "
                                "uploads it to a Design Studio folder named after the calling program, "
                                "and stores its URL in a program member field."),
                "triggerName": "Ticket is Generated",
                "filterName": "Ticket was Generated"
            }
        },
        # Marketo requires a primaryAttribute, it must be one of
        # invocationPayloadDef->flowAttributes, and it cannot be left blank.
        # Here it carries real weight - it is the event name printed on the pass.
        "primaryAttribute": ATTR_EVENT_NAME,

        "invocationPayloadDef": {
            "flowAttributes": [
                {
                    "apiName": ATTR_EVENT_NAME,
                    "dataType": "string",
                    "description": "The name of the event, printed as the headline on the pass",
                    "i18n": {"en_US": {"name": "Event Name"}}
                },
                {
                    "apiName": ATTR_EVENT_DATE,
                    "dataType": "string",
                    "description": "The event date, exactly as it should appear on the pass",
                    "i18n": {"en_US": {"name": "Event Date"}}
                },
                {
                    "apiName": ATTR_EVENT_LOCATION,
                    "dataType": "string",
                    "description": "The event location, exactly as it should appear on the pass",
                    "i18n": {"en_US": {"name": "Event Location"}}
                }
            ],
            # Mapped by the admin at install time. Required, so the install
            # cannot be completed without pointing it at a real field.
            "fields": [
                {
                    "serviceAttribute": IN_FIELD,
                    "dataType": "string",
                    "isRequired": True,
                    "description": "The person's full name, printed on the pass",
                    "i18n": {
                        "en_US": {
                            "name": "Full Name",
                            "description": "The person's full name, printed on the pass"
                        }
                    }
                }
            ],
            "userDrivenMapping": False
        },

        "callbackPayloadDef": {
            # The ticket URL is written straight to the program member field
            # over the REST API, so nothing needs mapping back onto the lead.
            "fields": [],
            "attributes": [
                {
                    "apiName": "event_name_value",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Event Name Value",
                                       "description": "The resolved event name used on the pass - the primary attribute shows the unresolved token"}}
                },
                {
                    "apiName": ATTR_EVENT_DATE,
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Event Date",
                                       "description": "The event date used on the pass"}}
                },
                {
                    "apiName": ATTR_EVENT_LOCATION,
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Event Location",
                                       "description": "The event location used on the pass"}}
                },
                {
                    "apiName": "full_name",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Full Name",
                                       "description": "The full name printed on the pass"}}
                },
                {
                    "apiName": "program_name",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Program Name",
                                       "description": "The program the flow step was called from"}}
                },
                {
                    "apiName": "ticket_image_url",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Ticket Image URL",
                                       "description": "Design Studio URL of the generated pass"}}
                },
                {
                    "apiName": "ticket_error",
                    "dataType": "text",
                    "i18n": {"en_US": {"name": "Ticket Error",
                                       "description": "Error detail when generation, upload or the program member write failed"}}
                }
            ],
            "userDrivenMapping": False
        }
    })


@bp.route("/status")
def status():
    """Health check that also proves the image pipeline actually works.

    A missing template, an unreadable font or a missing qrcode/Pillow install
    would otherwise only surface mid-batch, one failed lead at a time. Rendering
    a throwaway pass here catches all of it in one call, before anyone installs
    the service in Marketo.
    """
    problems = image_functions.self_check()

    if not marketo_functions.TICKET_FOLDER_ID:
        problems.append("TICKET_FOLDER_ID is not set")
    if not (marketo_functions.BASE_URL and marketo_functions.CLIENT_ID
            and marketo_functions.CLIENT_SECRET):
        problems.append("Marketo REST credentials are not fully set "
                        "(MARKETO_BASE_URL / MARKETO_CLIENT_ID / MARKETO_CLIENT_SECRET)")

    return jsonify({
        "status": "ok" if not problems else "error",
        "problems": problems,
        "ticket_field": TICKET_FIELD,
        "qr_prefix": image_functions.QR_PREFIX,
        # Which typeface the pass is actually being set in. Telegraf is the
        # brand face and wins when its files are present; Chivo is the bundled
        # open fallback, so seeing it here on a Knak deployment means the
        # Telegraf files did not make it onto the host.
        "font": image_functions._active_font_set().label,
    })


def _public_base_url() -> str:
    """The URL Marketo should call back on, including the /generateTicket prefix.

    Order of preference:
      1. PUBLIC_BASE_URL env var  - set this in production.
      2. The forwarded host/proto of the incoming request - this is what makes
         ngrok work: the tunnel URL changes on every restart and /install picks
         it up automatically, so swagger.json never needs editing.
    """
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return f"{configured}/{base}" if not configured.endswith(f"/{base}") else configured

    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}/{base}"


@bp.route("/install")
def serve_openapi():
    """Serve swagger.json with the server URL rewritten to wherever we are
    actually reachable, so the same file works in production and behind ngrok."""
    with open(os.path.join(os.path.dirname(__file__), "swagger.json"), encoding="utf-8") as f:
        spec = json.load(f)
    spec["servers"] = [{"url": _public_base_url()}]
    return Response(json.dumps(spec, indent=2), mimetype="application/json")
