# services/hearSource/routes.py
"""Marketo Self-Service Flow Step: categorise 'How did you hear about us?'
free text into Hear Source + Hear Source Detail.

All three fields are mapped by the admin at install time, so no Marketo API
names appear in this code and nothing has to be typed into the flow step:

  invocation  hear_about_us        <- map to How_did_you_hear_about_us__c
  callback    hear_source          -> map to your Hear Source field
  callback    hear_source_detail   -> map to your Hear Source Detail field

Marketo requires a primaryAttribute that cannot be blank, so there is one dummy
flow-step input whose value is ignored.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import time
from flask import Blueprint, request, jsonify, Response, send_file
import json
import os
import traceback

import pandas as pd
import pytz
import requests

import googlesheets_functions
from . import openai_functions, taxonomy

base = "hearSource"
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
MAX_WORKERS = 8     # parallel OpenAI calls per batch
# How often the leads_processed cell is refreshed while a batch runs. Sheets
# allows ~60 writes/minute, so don't drop this much lower.
PROGRESS_INTERVAL_SECONDS = 10

# Column positions in the batches tab (0-based, A=0). The header is:
#   timestamp | leads | leads_processed | error | callback_response | request...
COL_LEADS_PROCESSED = 2
COL_ERROR = 3
# Marketo always puts "token" first in the payload, so it is always inside the
# first request cell - no need to search the overflow columns.
COL_REQUEST_RANGE = "F:F"

# The leads tab is written positionally, so every row must have exactly these
# columns in this order - including rows from the per-lead error path, which
# would otherwise be narrower and make pandas pad the frame with NaN. NaN is not
# valid JSON and Google rejects the whole append, losing every row in the batch.
LEAD_COLUMNS = [
    "timestamp", "lead_id", "hear_about_us", "hear_source", "hear_source_detail",
    "skipped", "cached", "model", "raw_model_answer", "error",
]

# Fixed classification settings. Deliberately NOT flow-step inputs - the flow
# step asks only for the Hear About Us value. Change them here.
MODEL = "gpt-5.6-luna"
# None means "don't send temperature" - newer models only accept their default
# and reject the parameter. Use 0.0 on gpt-4o-generation models for determinism.
TEMPERATURE = None

# Names declared in the service definition below.
IN_FIELD = "hear_about_us"               # mapped lead field (invocation)
IN_FIELD_VALUE = "hear_about_us_value"   # activity attribute holding the text categorized
DUMMY_ATTR = "note"                      # required primaryAttribute; value ignored
OUT_SOURCE = "hear_source"
OUT_DETAIL = "hear_source_detail"


# ---------- AUTH ----------
def _check_auth(username, password):
    return username == os.getenv("MARKETO_USER") and password == os.getenv("MARKETO_PASSWORD")


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
                {"WWW-Authenticate": 'Basic realm=Knak Hear Source'}
            )


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

    Marketo holds the connection open waiting for this response. Classifying a
    1000-lead batch takes minutes, which trips Marketo's read timeout
    (SocketTimeoutException) and fails the whole step. The 202 means "accepted,
    I will call you back" - so send it straight away and do the work after.
    """
    timestamp = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S")
    data = request.get_json(force=True)
    object_data = data.get("objectData", []) if isinstance(data, dict) else []

    # ---- log the batch the moment it arrives, before the 202 --------------
    # The request payload is on the sheet before any work starts, so a crash, a
    # restart or a hung OpenAI call still leaves a record of what came in.
    batch_row_number = None
    # Marketo's callback token is unique per invocation and already sits in the
    # request payload we log, so it doubles as this batch's id - no extra column.
    batch_token = str(data.get("token", "")) if isinstance(data, dict) else ""
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
            print("[hearSource] could not locate this batch's row"
                  f"{' (no token in payload)' if not batch_token else ''};"
                  " progress and outcome will not be logged")
    except Exception as gs_err:
        print("Sheets error writing the incoming batch row:", gs_err)

    worker = threading.Thread(
        target=_process_batch,
        args=(data, timestamp, batch_row_number),
        name=f"hearSource-batch-{batch_token[:12] or 'unknown'}",
        daemon=True,
    )
    worker.start()
    print(f"[hearSource] accepted {len(object_data)} lead(s), processing in {worker.name}")

    # Accepted. The result reaches Marketo via the callback, not this response.
    return ("", 202)


def _process_batch(data: dict, timestamp: str, batch_row_number) -> None:
    """Classify a whole batch and post the callback. Runs off the request thread,
    so it must never raise into the caller and cannot touch the Flask request."""
    rows_leads: list[dict] = []
    cb_response = ""
    object_data = []

    try:
        object_data = data.get("objectData", [])

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

        # ---- classify every lead, in parallel, de-duplicating identical text ----
        # A batch campaign can send 1000 leads; the same phrase ("Google",
        # "LinkedIn") shows up constantly, so identical text is classified once.
        cache: dict[tuple, dict] = {}

        def classify_one(obj: dict) -> dict:
            octx = obj.get("objectContext", {})
            raw_text = str(octx.get(IN_FIELD, "") or "")

            key = raw_text.strip().lower()
            if key in cache:
                return {**cache[key], "cached": True}

            result = openai_functions.classify(
                raw_text=raw_text,
                model=MODEL,
                temperature=TEMPERATURE,
            )
            result["cached"] = False
            cache[key] = result
            return result

        def process(obj: dict) -> tuple[dict, dict]:
            """Returns (callback_object, log_row).

            Never raises. One bad lead must not take down the batch, so even
            reading the payload happens inside the try - a malformed objectData
            entry would otherwise blow up here, outside any handler, and
            ThreadPoolExecutor would re-raise it for the whole batch.
            """
            lead_id = None
            raw_text = ""
            try:
                octx = obj.get("objectContext", {}) or {}
                lead_id = octx.get("id")
                raw_text = str(octx.get(IN_FIELD, "") or "")

                r = classify_one(obj)

                callback_object = {
                    "leadData": {
                        "id": lead_id,
                        OUT_SOURCE: r["hear_source"],
                        OUT_DETAIL: r["hear_source_detail"],
                    },
                    "activityData": {
                        IN_FIELD_VALUE: raw_text,
                        "hear-source": r["hear_source"],
                        "hear-source-detail": r["hear_source_detail"],
                        "model-used": MODEL,
                        "success": True,
                    },
                }
                log_row = {
                    "timestamp": timestamp,
                    "lead_id": f"https://{MARKETO_INSTANCE}.marketo.com/leadDatabase/loadLeadDetail?leadId={lead_id}",
                    "hear_about_us": raw_text,
                    "hear_source": r["hear_source"],
                    "hear_source_detail": r["hear_source_detail"],
                    "skipped": r["skipped"],
                    "cached": r.get("cached", False),
                    "model": MODEL,
                    "raw_model_answer": r["raw_model_answer"],
                    "error": "",
                }
                return callback_object, log_row

            except Exception as e:
                error = f"{e}\n{traceback.format_exc()}"
                # Per-lead failures never reached the terminal before, so the
                # only copy lived in a Sheets row that might not get written.
                print(f"[hearSource] lead {lead_id} failed: {type(e).__name__}: {e}")
                callback_object = {
                    "leadData": {"id": lead_id},
                    "activityData": {
                        IN_FIELD_VALUE: raw_text,
                        "hear-source-error": error[:1000],
                        "success": False,
                    },
                }
                log_row = {
                    "timestamp": timestamp,
                    "lead_id": f"https://{MARKETO_INSTANCE}.marketo.com/leadDatabase/loadLeadDetail?leadId={lead_id}",
                    "hear_about_us": raw_text,
                    "error": error,
                }
                return callback_object, log_row

            finally:
                note_progress()

        if object_data:
            workers = max(1, min(MAX_WORKERS, len(object_data)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(process, object_data))
        else:
            results = []

        callback_objects = [cb for cb, _ in results]
        rows_leads = [row for _, row in results]

        # ─────────── ONE callback back to Marketo ────────────
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
            timeout=30
        )

        cb_response = r.text

        # ---- fill in the outcome on the row written when the batch arrived ----
        try:
            googlesheets_functions.writeDF2Sheet(
                pd.DataFrame(rows_leads, columns=LEAD_COLUMNS), SHEET_LEADS, SPREADSHEET_ID)
            googlesheets_functions.updateCells(
                [progress["done"],
                 "" if r.ok else f"Callback HTTP {r.status_code}",
                 cb_response],
                SHEET_BATCHES, SPREADSHEET_ID, batch_row_number, COL_LEADS_PROCESSED)
        except Exception as gs_err:
            print("Sheets logging error:", gs_err)

        if not r.ok:
            print(f"[hearSource] callback to Marketo failed: HTTP {r.status_code} {r.text[:200]}")
        return

    except Exception as e:
        # Print it too - if the Sheets write below also fails, this is the only
        # place the error survives.
        traceback.print_exc()
        error_text = f"{e}\n{traceback.format_exc()}"

        try:
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
        "apiName": "hear-source-categorizer",
        "i18n": {
            "en_US": {
                "name": "Categorize Hear About Us",
                "description": "Reads the free-text 'How did you hear about us?' answer and writes back a Hear Source and Hear Source Detail from your picklists.",
                "triggerName": "Hear About Us is Categorized",
                "filterName": "Hear About Us was Categorized"
            }
        },
        # Marketo requires a primaryAttribute, it must be one of
        # invocationPayloadDef->flowAttributes, and it cannot be left blank - it
        # always renders on the flow step. This service needs no flow-step input,
        # so the slot is filled by a dummy whose value is never read.
        "primaryAttribute": DUMMY_ATTR,

        "invocationPayloadDef": {
            "flowAttributes": [
                {
                    "apiName": DUMMY_ATTR,
                    "dataType": "string",
                    "description": "Not used. Marketo requires one flow step input - type anything, e.g. n/a.",
                    "i18n": {"en_US": {"name": "Note (not used)"}}
                }
            ],
            # Mapped by the admin at install time. Required, so the install
            # cannot be completed without pointing it at a real field.
            "fields": [
                {
                    "serviceAttribute": IN_FIELD,
                    "dataType": "string",
                    "isRequired": True,
                    "description": "The free-text answer to categorize",
                    "i18n": {
                        "en_US": {
                            "name": "How Did You Hear About Us",
                            "description": "The free-text answer to categorize"
                        }
                    }
                }
            ],
            "userDrivenMapping": False
        },

        "callbackPayloadDef": {
            # The admin maps these to the real Hear Source / Hear Source Detail
            # fields at install time, so no API names live in this code.
            "fields": [
                {
                    "serviceAttribute": OUT_SOURCE,
                    "dataType": "string",
                    "description": "Top-level category written back to the lead",
                    "i18n": {
                        "en_US": {
                            "name": "Hear Source",
                            "description": "Top-level category written back to the lead"
                        }
                    }
                },
                {
                    "serviceAttribute": OUT_DETAIL,
                    "dataType": "string",
                    "description": "Sub-category within the Hear Source",
                    "i18n": {
                        "en_US": {
                            "name": "Hear Source Detail",
                            "description": "Sub-category within the Hear Source"
                        }
                    }
                }
            ],
            "attributes": [
                {
                    "apiName": IN_FIELD_VALUE,
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Hear About Us Value",
                                       "description": "The resolved text that was categorized - the primary attribute shows the unresolved token"}}
                },
                {
                    "apiName": "hear-source",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Hear Source",
                                       "description": "Category assigned"}}
                },
                {
                    "apiName": "hear-source-detail",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Hear Source Detail",
                                       "description": "Sub-category assigned"}}
                },
                {
                    "apiName": "model-used",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Model Used",
                                       "description": "Model used"}}
                },
                {
                    "apiName": "hear-source-error",
                    "dataType": "text",
                    "i18n": {"en_US": {"name": "Error",
                                       "description": "Error detail when categorization failed"}}
                }
            ],
            "userDrivenMapping": False
        }
    })


@bp.route("/status")
def status():
    problems = taxonomy.self_check()
    return jsonify({
        "status": "ok" if not problems else "error",
        "hear_sources": len(taxonomy.HEAR_SOURCES),
        "taxonomy_problems": problems,
    })


def _public_base_url() -> str:
    """The URL Marketo should call back on, including the /hearSource prefix.

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
