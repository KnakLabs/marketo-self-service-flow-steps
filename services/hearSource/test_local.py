"""Local smoke test for the hearSource SSFS.

Run from the repo root:

    python services/hearSource/test_local.py            # mocked OpenAI, no cost
    LIVE=1 python services/hearSource/test_local.py     # real OpenAI calls

It fakes the Marketo invocation payload, intercepts the callback POST and the
Google Sheets write, and prints what would have been written to each lead.
The LIVE run is the one that actually tests system_prompt.md.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# Force the test credentials regardless of what .env holds - this test never
# talks to Marketo, it only needs the basic-auth check to pass.
os.environ["MARKETO_USER"] = "test"
os.environ["MARKETO_PASSWORD"] = "test"
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

LIVE = os.environ.get("LIVE") == "1"

# (input text, expected hear_source, expected hear_source_detail)
# Expectations come straight from the worked examples in system_prompt.md.
CASES = [
    ("Google",                                                        "Organic Search", "Google"),
    ("googled 'marketo email builder'",                               "Organic Search", "Google"),
    ("found you through a search engine",                             "Organic Search", ""),
    ("clicked a Google ad",                                           "Paid Search",    "Google Ad"),
    ("a sponsored post on linkden",                                   "Paid Social",    "LinkedIn"),
    ("a banner ad on Adweek",                                         "Display",        ""),
    ("saw a post on LinkedIn from someone at Knak",                   "Organic Social", "LinkedIn"),
    ("ChatGPT recommended you",                                       "AI Assistant",   "ChatGPT"),
    ("A colleague at work recommended you",                           "Referral",       "WOM"),
    ("G2 reviews",                                                    "Referral",       "G2"),
    ("exitfive",                                                      "Referral",       "Exit Five Newsletter"),
    ("I saw the Knak plugin",                                         "Referral",       "Claude Plugin"),
    ("We met your team at the Adobe Summit booth",                    "Event",          "Adobe Summit"),
    ("an SDR emailed me",                                             "Sales",          "Email"),
    ("Used Knak before",                                              "Unknown",        ""),
    ("attended your webinar",                                         "Unknown",        ""),
    ("Email is my preferred method of contact, thanks!",              "Unknown",        ""),
    ("sheep",                                                         "Unknown",        ""),
    ("n/a",                                                           "Unknown",        ""),
    ("",                                                              "Unknown",        ""),
    # §3 step A - the sentence names the real discovery moment
    ("Saw you on LinkedIn but honestly discovered you through ChatGPT",        "AI Assistant",   "ChatGPT"),
    ("I saw an ad on LinkedIn, but I first heard about Knak from a colleague", "Referral",       "WOM"),
    # §3 step B - no sequencing language, so the ladder decides
    ("Found you on Google and X",                                     "Organic Social", "X"),
    ("LinkedIn and Google",                                           "Organic Social", "LinkedIn"),
    ("A colleague mentioned you, also saw an ad for you on LinkedIn",  "Paid Social",    "LinkedIn"),
    ("Read about you on G2, also found you on Google",                "Referral",       "G2"),
]

import requests as _requests  # noqa: E402
import googlesheets_functions as _gs  # noqa: E402

_sheet_writes = []
_gs.writeDF2Sheet = lambda df, sheet, sid: _sheet_writes.append((sheet, len(df)))

_captured = {}
_callback_posted = __import__("threading").Event()


class _FakeResp:
    ok = True
    status_code = 200
    text = '{"success":true}'


def _fake_post(url, headers=None, json=None, timeout=None):
    _captured["url"] = url
    _captured["headers"] = headers
    _captured["body"] = json
    _callback_posted.set()
    return _FakeResp()


_requests.post = _fake_post

from services.hearSource import routes, taxonomy  # noqa: E402

routes.requests.post = _fake_post

if not LIVE:
    # Mocked mode replays the expected answers so we test the wiring and the
    # guardrails, not the model. Two deliberately bad answers are injected to
    # prove the coercion in taxonomy.validate() actually fires.
    _BAD = {
        "sheep": ("Unknown", "Unknown"),          # placeholder detail -> must be blanked
        "an sdr emailed me": ("Sales", "Slack"),  # detail outside the closed list -> blanked
    }

    def _mock_classify(raw_text, model=None, temperature=None):
        raw_text = (raw_text or "").strip()
        if not raw_text:
            return {"hear_source": taxonomy.FALLBACK_SOURCE, "hear_source_detail": "",
                    "raw_model_answer": "", "coerced": False, "skipped": True}
        expected = {t.lower(): (s, d) for t, s, d in CASES}
        s, d = _BAD.get(raw_text.lower()) or expected.get(raw_text.lower(), ("Unknown", ""))
        answer = json.dumps({"hear_source": s, "hear_source_detail": d})
        src, det, clean = taxonomy.validate(s, d)
        return {"hear_source": src, "hear_source_detail": det,
                "raw_model_answer": answer, "coerced": not clean, "skipped": False}

    routes.openai_functions.classify = _mock_classify
else:
    _BAD = {}

# Build a minimal app with just this blueprint, so the test does not need the
# every other service's dependencies installed.
from flask import Flask  # noqa: E402

app = Flask(__name__)
app.register_blueprint(routes.bp)

payload = {
    "token": "tok",
    "apiCallBackKey": "key",
    "campaignId": 1234,
    "callbackUrl": "https://mkto-cfa.adobe.io/customflowaction/submitCustomFlowAction",
    "context": {"admin": {}},
    "objectData": [
        {
            "objectType": "lead",
            "objectContext": {"email": f"test{i}@example.com", "id": 1000 + i,
                              routes.IN_FIELD: text},
            "flowStepContext": {routes.DUMMY_ATTR: "n/a"},
        }
        for i, (text, _, _) in enumerate(CASES)
    ],
}

client = app.test_client()
auth = ("test", "test")

print("=" * 108)
print("/status")
r = client.get("/hearSource/status", auth=auth)
print(r.status_code, r.get_json())
assert r.get_json()["status"] == "ok", "taxonomy.py and system_prompt.md disagree"

print("=" * 108)
print("/getServiceDefinition")
sd = client.get("/hearSource/getServiceDefinition", auth=auth).get_json()
print("apiName        :", sd["apiName"])
print("flow attributes:", [a["apiName"] for a in sd["invocationPayloadDef"]["flowAttributes"]])
print("input fields   :", [f["serviceAttribute"] for f in sd["invocationPayloadDef"]["fields"]])
print("output fields  :", [f["serviceAttribute"] for f in sd["callbackPayloadDef"]["fields"]])

# Marketo's own validation rules, so a definition mistake fails here not there.
inv, cbp = sd["invocationPayloadDef"], sd["callbackPayloadDef"]
sd_errors = []
for section, section_def in (("invocationPayloadDef", inv), ("callbackPayloadDef", cbp)):
    for i, f in enumerate(section_def.get("fields", []), start=1):
        for key in ("serviceAttribute", "description", "dataType"):
            if key not in f:
                sd_errors.append(f"{section}->fields #{i} missing {key}")
pa = sd["primaryAttribute"]
if pa not in [a["apiName"] for a in inv["flowAttributes"]]:
    sd_errors.append(f'primaryAttribute "{pa}" not in invocationPayloadDef->flowAttributes')
if pa in [a["apiName"] for a in cbp["attributes"]]:
    sd_errors.append(f'primaryAttribute "{pa}" must not be in callbackPayloadDef->attributes')
print("marketo rules  :", "PASS" if not sd_errors else sd_errors)
assert not sd_errors, sd_errors

print("=" * 108)
print("/serviceIcon + /brandIcon  (no auth required)")
for route in ("/hearSource/serviceIcon", "/hearSource/brandIcon"):
    r = client.get(route)
    import struct
    w, h = struct.unpack(">II", r.data[16:24])
    print(f"  {route:<28} {r.status_code} {r.headers['Content-Type']} {w}x{h} {len(r.data)}B")
    assert r.status_code == 200 and r.headers["Content-Type"] == "image/png"

print("=" * 108)
print("/install  (no auth required; server URL follows the tunnel)")
r = client.get("/hearSource/install")
print("  plain request        ->", r.get_json()["servers"][0]["url"])
r = client.get("/hearSource/install", headers={
    "X-Forwarded-Proto": "https", "X-Forwarded-Host": "abc123.ngrok-free.app"})
ngrok_url = r.get_json()["servers"][0]["url"]
print("  behind ngrok headers ->", ngrok_url)
assert ngrok_url == "https://abc123.ngrok-free.app/hearSource", ngrok_url
os.environ["PUBLIC_BASE_URL"] = "https://ssfs.example.com"
r = client.get("/hearSource/install", headers={
    "X-Forwarded-Proto": "https", "X-Forwarded-Host": "abc123.ngrok-free.app"})
print("  PUBLIC_BASE_URL set  ->", r.get_json()["servers"][0]["url"], "(env wins)")
assert r.get_json()["servers"][0]["url"] == "https://ssfs.example.com/hearSource"
del os.environ["PUBLIC_BASE_URL"]

print("=" * 108)
print("JSON schema sent to OpenAI - checked against OpenAI's strict-mode rules")
# This is the one part of the OpenAI request we CAN verify without a key: strict
# mode rejects a schema that breaks any of these, and the failure only shows up
# as a 400 at runtime otherwise.
schema_wrapper = taxonomy.json_schema()
schema = schema_wrapper["schema"]
checks = [
    ("has a name", bool(schema_wrapper.get("name"))),
    ("strict is true", schema_wrapper.get("strict") is True),
    ("root type is object", schema.get("type") == "object"),
    ("additionalProperties is false", schema.get("additionalProperties") is False),
    ("every property is required",
     sorted(schema.get("required", [])) == sorted(schema.get("properties", {}).keys())),
    ("hear_source is an enum", "enum" in schema["properties"]["hear_source"]),
    ("enum matches HEAR_SOURCES exactly",
     schema["properties"]["hear_source"]["enum"] == taxonomy.HEAR_SOURCES),
    ("no unsupported keywords",
     not any(k in json.dumps(schema) for k in
             ('"minLength"', '"maxLength"', '"pattern"', '"format"', '"oneOf"', '"default"'))),
]
for label, passed in checks:
    print(f"  [{'ok' if passed else 'FAIL'}] {label}")
assert all(p for _, p in checks), [label for label, p in checks if not p]
print(json.dumps(schema_wrapper, indent=2)[:520] + " ...")

print("=" * 108)
print("/submitAsyncAction  (LIVE - real OpenAI)" if LIVE else "/submitAsyncAction  (mocked)")
r = client.post("/hearSource/submitAsyncAction", json=payload, auth=auth)
print("HTTP", r.status_code, "(202 is returned before processing starts)")
# The batch is now handled on a background thread, so wait for the callback.
if not _callback_posted.wait(timeout=300):
    raise SystemExit("callback never posted - the background worker did not finish")

# The per-case table is only meaningful in LIVE mode. In mocked mode the mock
# replays the expected answers, so comparing them proves nothing about accuracy
# - only the plumbing and the guardrails below are real evidence there.
if LIVE:
    print("-" * 108)
    print(f"{'':2}{'How did you hear about us':<56}{'Hear Source':<17}{'Detail':<24}")
    print("-" * 108)
mismatches = []
coercion_checks = []
for obj, cb, (text, exp_s, exp_d) in zip(payload["objectData"], _captured["body"]["objectData"], CASES):
    ld, ad = cb["leadData"], cb["activityData"]
    got_s = ld.get(routes.OUT_SOURCE, "-")
    got_d = ld.get(routes.OUT_DETAIL, "")

    if text.lower() in _BAD:
        # A deliberately bad model answer. It must have been coerced, not passed
        # through - the detail is blanked and the record flagged for review.
        coercion_checks.append((text, _BAD[text.lower()], got_s, got_d))
        ok = (got_d == "")
    else:
        ok = (got_s == exp_s and got_d == exp_d)
        if not ok:
            mismatches.append((text, exp_s, exp_d, got_s, got_d))
    if LIVE:
        print(f"{'  ' if ok else '! '}{(text or '(blank)')[:55]:<56}{got_s:<17}{(got_d or '-'):<24}"
    )

print("-" * 108)
print("callback url  :", _captured["url"])
print("callback hdrs :", sorted(_captured["headers"].keys()))
print("leads in cb   :", len(_captured["body"]["objectData"]))
print("sheet writes  :", _sheet_writes)

off = [cb["leadData"].get(routes.OUT_SOURCE) for cb in _captured["body"]["objectData"]
       if cb["leadData"].get(routes.OUT_SOURCE) not in taxonomy.HEAR_SOURCES]
print("off-taxonomy  :", len(off), "(must be 0)")

# Guide rule: every activityData key must be declared in
# callbackPayloadDef.attributes, or it silently never appears in the activity
# detail. "success" is native to Marketo and is not declared.
declared = {a["apiName"] for a in cbp["attributes"]} | {"success"}
used = set()
for cb in _captured["body"]["objectData"]:
    used |= set(cb["activityData"].keys())
undeclared = sorted(used - declared)
print("activity attrs:", "all declared" if not undeclared else f"UNDECLARED: {undeclared}")
assert not undeclared, undeclared
unused = sorted(declared - used - {"success"})
if unused:
    print(f"                (declared but not sent this run: {unused})")
if coercion_checks:
    print("\nGuardrail checks - deliberately bad model answers that had to be coerced:")
    for text, (bs, bd), got_s, got_d in coercion_checks:
        status = "COERCED" if got_d == "" else "LEAKED THROUGH"
        print(f'  "{text}"  model said {bs} / {bd or "(blank)"}  ->  '
              f'wrote {got_s} / {got_d or "(blank)"}  [{status}]')
        assert got_d == "", f"guardrail failed for {text!r}"
assert not off, off
assert len(_captured["body"]["objectData"]) == len(CASES)

if not LIVE:
    mismatches = []  # circular in mocked mode - not evidence either way
    print(f"plumbing      : {len(CASES)} leads in, {len(CASES)} callback objects out, all well-formed")

if mismatches:
    print(f"\n{len(mismatches)} of {len(CASES)} did not match the expected answer:")
    for text, es, ed, gs_, gd in mismatches:
        print(f'  "{text}"\n      expected: {es} / {ed or "(blank)"}\n      got:      {gs_} / {gd or "(blank)"}')
    if not LIVE:
        raise SystemExit("Mocked run must match exactly - the harness is broken")
    print("\nLIVE run: review the above and tune system_prompt.md if needed.")
else:
    label = "MODEL MATCHED ALL" if LIVE else "GUARDRAILS PASSED;"
    print(f"\n{label} {len(CASES) - len(coercion_checks)} EXPECTED CASES"
          f" + {len(coercion_checks)} COERCION CASES")

if not LIVE:
    print("""
WHAT THIS MOCKED RUN PROVES
  It proves the plumbing: payload parsing, field mapping, the callback body,
  threading and de-duplication, the coercion rules in taxonomy.validate(), the
  endpoints, and that taxonomy.py agrees with system_prompt.md.

  It does NOT prove classification accuracy, which is why no per-case table is
  printed here - the mock replays the expected answers, so comparing them would
  be circular. It also never executes the real openai_functions.classify(), so
  the OpenAI request itself is unverified beyond the offline schema checks above.

  Run with LIVE=1 to test system_prompt.md against the real model.""")
