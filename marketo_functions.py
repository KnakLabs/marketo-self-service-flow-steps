"""Marketo REST API helpers, shared by every service in this repo.

Only generateTicket uses these today. The Marketo REST API is a different
integration from the SSFS callback: SSFS pushes lead data to us and we call
back, while these functions reach *into* the instance to look up programs,
create Design Studio folders, upload files and write program member fields.
That needs its own credentials (MARKETO_BASE_URL / CLIENT_ID / CLIENT_SECRET),
which the SSFS basic-auth pair does not cover.
"""
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = (os.environ.get("MARKETO_BASE_URL") or "").rstrip("/")
CLIENT_ID = os.environ.get("MARKETO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("MARKETO_CLIENT_SECRET")

# Design Studio folder that holds one subfolder per program. Set this to the id
# of a folder you have created in Design Studio - there is no sensible default,
# because a wrong folder id silently scatters generated tickets somewhere
# unexpected rather than failing.
TICKET_FOLDER_ID = os.environ.get("TICKET_FOLDER_ID", "")

# Sync Program Member Data accepts at most 300 records per call.
PROGRAM_MEMBER_BATCH = 300

TIMEOUT = 60


class _SlidingWindowRateLimiter:
    """Allow max_requests per window_seconds, blocking callers past the limit."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window = window_seconds
        self.events: deque = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            while self.events and now - self.events[0] > self.window:
                self.events.popleft()

            if len(self.events) >= self.max_requests:
                sleep_for = self.window - (now - self.events[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.time()
                while self.events and now - self.events[0] > self.window:
                    self.events.popleft()

            self.events.append(time.time())


# Marketo allows 100 API calls per 20 seconds and 10 concurrent calls, so a
# 1000-lead batch has to be paced or the instance starts returning error 606.
#
# These are the platform's limits, identical in every subscription and every
# sandbox, so they are constants rather than configuration - there is no
# environment in which a different value would be correct. Raising them does not
# raise the limit, it just moves the failure from here to Marketo.
_LIMITER = _SlidingWindowRateLimiter(max_requests=100, window_seconds=20.0)

_token_lock = threading.Lock()
_token: Dict[str, object] = {"value": None, "expires_at": 0.0}


# ---------- AUTH ----------
def _access_token() -> str:
    """Client-credentials token, cached process-wide until 60s before expiry."""
    with _token_lock:
        if _token["value"] and time.time() < float(_token["expires_at"]):
            return str(_token["value"])

        if not (BASE_URL and CLIENT_ID and CLIENT_SECRET):
            raise RuntimeError(
                "Missing Marketo REST credentials: set MARKETO_BASE_URL, "
                "MARKETO_CLIENT_ID and MARKETO_CLIENT_SECRET"
            )

        _LIMITER.acquire()
        r = requests.get(
            f"{BASE_URL}/identity/oauth/token",
            params={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()

        _token["value"] = body["access_token"]
        _token["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 60
        logger.info("Marketo access token refreshed")
        return str(_token["value"])


def _auth_header() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_access_token()}"}


def _check(body: Dict, what: str) -> Dict:
    """Marketo returns HTTP 200 with success:false, so status codes aren't enough."""
    if not body.get("success", False):
        raise RuntimeError(f"{what} failed: {json.dumps(body.get('errors', body))}")
    return body


# ---------- WHICH PROGRAM CALLED US ----------
def get_program_for_campaign(campaign_id) -> Tuple[int, str]:
    """Resolve the program that owns a smart campaign.

    Marketo's invocation payload only carries campaignId. The smart campaign
    asset record has a `folder` reference, which is either the program itself
    (type "Program") or a campaign folder nested inside one, so walk upwards
    until a Program turns up.

    Returns (program_id, program_name). Raises if the campaign isn't in a program.
    """
    if not campaign_id:
        raise RuntimeError("No campaignId in the Marketo payload")

    _LIMITER.acquire()
    r = requests.get(
        f"{BASE_URL}/rest/asset/v1/smartCampaign/{campaign_id}.json",
        headers=_auth_header(),
        timeout=TIMEOUT,
    )
    body = _check(r.json(), f"Lookup of smart campaign {campaign_id}")
    result = body.get("result") or []
    if not result:
        raise RuntimeError(f"Smart campaign {campaign_id} not found")

    ref = result[0].get("folder") or {}

    # Bounded walk so a malformed folder tree can't spin forever.
    for _ in range(10):
        if not ref or not ref.get("id"):
            break
        if str(ref.get("type", "")).lower() == "program":
            program_id = int(ref["id"])
            return program_id, get_program_name(program_id)

        _LIMITER.acquire()
        fr = requests.get(
            f"{BASE_URL}/rest/asset/v1/folder/{int(ref['id'])}.json",
            headers=_auth_header(),
            timeout=TIMEOUT,
        )
        fresult = _check(fr.json(), f"Lookup of folder {ref['id']}").get("result") or []
        if not fresult:
            break
        ref = fresult[0].get("parent") or {}

    raise RuntimeError(
        f"Smart campaign {campaign_id} is not inside a program - Generate Ticket "
        "must be called from a smart campaign that lives in a program"
    )


def get_program_name(program_id: int) -> str:
    """Program name for a program id."""
    _LIMITER.acquire()
    r = requests.get(
        f"{BASE_URL}/rest/asset/v1/program/{int(program_id)}.json",
        headers=_auth_header(),
        timeout=TIMEOUT,
    )
    result = _check(r.json(), f"Lookup of program {program_id}").get("result") or []
    if not result:
        raise RuntimeError(f"Program {program_id} not found")
    return result[0]["name"]


# ---------- DESIGN STUDIO FOLDER ----------
def _folder_by_name(name: str, root: str) -> Optional[int]:
    _LIMITER.acquire()
    r = requests.get(
        f"{BASE_URL}/rest/asset/v1/folder/byName.json",
        headers=_auth_header(),
        params={"name": name, "root": root},
        timeout=TIMEOUT,
    )
    body = r.json()
    # byName returns success:true with an empty result and a "No assets found"
    # warning when the folder is absent, so that is not an error.
    if body.get("success") and (body.get("result") or []):
        return int(body["result"][0]["id"])
    return None


def get_or_create_folder(name: str, parent_id) -> int:
    """Folder id for `name` inside `parent_id`, creating it if it isn't there."""
    if not parent_id:
        raise RuntimeError(
            "TICKET_FOLDER_ID is not set - point it at the Design Studio folder "
            "that should hold one subfolder of tickets per program"
        )

    root = json.dumps({"id": int(parent_id), "type": "Folder"})

    existing = _folder_by_name(name, root)
    if existing is not None:
        logger.info(f"Using existing Design Studio folder '{name}' ({existing})")
        return existing

    _LIMITER.acquire()
    cr = requests.post(
        f"{BASE_URL}/rest/asset/v1/folders.json",
        headers=_auth_header(),
        data={
            "name": name,
            "parent": root,
            "description": f"Ticket images generated for the {name} program",
        },
        timeout=TIMEOUT,
    )
    cbody = cr.json()

    if not cbody.get("success"):
        # A concurrent batch may have created it between the lookup and the
        # create, so re-read before giving up.
        retry = _folder_by_name(name, root)
        if retry is not None:
            return retry
        _check(cbody, f"Creation of folder '{name}'")

    folder_id = int(cbody["result"][0]["id"])
    logger.info(f"Created Design Studio folder '{name}' ({folder_id})")
    return folder_id


# ---------- FILE UPLOAD ----------
def upload_file(file_name: str, file_bytes: bytes, folder_id: int,
                mime_type: str = "image/png", description: str = "") -> str:
    """Upload to Design Studio and return the public asset URL.

    insertOnly is false so re-running the campaign for the same person
    overwrites their ticket rather than failing on a duplicate name.
    """
    _LIMITER.acquire()
    r = requests.post(
        f"{BASE_URL}/rest/asset/v1/files.json",
        headers=_auth_header(),
        files={"file": (file_name, file_bytes, mime_type)},
        data={
            "name": file_name,
            "folder": json.dumps({"id": int(folder_id), "type": "Folder"}),
            "description": description,
            "insertOnly": "false",
        },
        timeout=TIMEOUT,
    )
    result = _check(r.json(), f"Upload of '{file_name}'").get("result") or []
    if not result or not result[0].get("url"):
        raise RuntimeError(f"Upload of '{file_name}' returned no url")
    return str(result[0]["url"])


# ---------- PROGRAM MEMBER FIELD ----------
def sync_program_member_field(program_id: int, field_api_name: str,
                              values: List[Tuple[int, str]]) -> Dict[int, Optional[str]]:
    """Write one program member field for many leads, 300 per call.

    `values` is [(lead_id, value), ...]. Returns {lead_id: None if written else error}.

    This endpoint updates existing program members only - it will not enroll
    anyone - so a lead who is not a member of the program comes back rejected.
    """
    outcome: Dict[int, Optional[str]] = {}

    for start in range(0, len(values), PROGRAM_MEMBER_BATCH):
        chunk = values[start:start + PROGRAM_MEMBER_BATCH]

        try:
            _LIMITER.acquire()
            r = requests.post(
                f"{BASE_URL}/rest/v1/programs/{int(program_id)}/members.json",
                headers={**_auth_header(), "Content-Type": "application/json"},
                json={
                    "input": [
                        {"leadId": int(lead_id), field_api_name: value}
                        for lead_id, value in chunk
                    ]
                },
                timeout=TIMEOUT,
            )
            body = r.json()
        except Exception as e:
            for lead_id, _ in chunk:
                outcome[lead_id] = f"Program member update request failed: {e}"
            continue

        if not body.get("success"):
            err = json.dumps(body.get("errors", body))
            for lead_id, _ in chunk:
                outcome[lead_id] = f"Program member update failed: {err}"
            continue

        results = body.get("result") or []
        for i, (lead_id, _) in enumerate(chunk):
            # Prefer matching on leadId, then seq, then input order.
            entry = (
                next((x for x in results if x.get("leadId") == lead_id), None)
                or next((x for x in results if x.get("seq") == i), None)
                or (results[i] if i < len(results) else None)
            )

            if entry is None:
                outcome[lead_id] = "No result returned by Marketo"
                continue

            status = str(entry.get("status", "")).lower()
            if status in ("updated", "created", "added"):
                outcome[lead_id] = None
            else:
                reasons = entry.get("reasons") or []
                detail = "; ".join(
                    x.get("message", json.dumps(x)) for x in reasons
                ) or "no reason given"
                outcome[lead_id] = (
                    f"Program member update {status or 'skipped'} - {detail}. "
                    "The person must already be a member of this program."
                )

        logger.info(f"Program member field written for {len(chunk)} leads")

    return outcome
