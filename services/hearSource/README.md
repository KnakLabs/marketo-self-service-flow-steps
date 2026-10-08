# Categorize Hear About Us (SSFS)

Reads the free-text answer to "How did you hear about us?" and writes back a
**Hear Source** and **Hear Source Detail**.

## Files

| File | What it does |
|---|---|
| `system_prompt.md` | **The classification rules.** Plain markdown - edit this to change how text is categorized. The code reads it at runtime. |
| `taxonomy.py` | The 10 allowed Hear Source values and the hard detail rules. Only edit when the picklist itself changes. |
| `openai_functions.py` | Builds the request and calls OpenAI. |
| `routes.py` | The Marketo endpoints, batch handling, callback and Sheets logging. |
| `swagger.json` | The install file Marketo reads. `/install` rewrites `servers[0].url` to wherever the app is actually reachable, so it needs no editing. |
| `test_local.py` | Smoke test with 26 real-world phrases. Run it before every deploy. |

## The Hear Source list is enforced, not suggested

`hear_source` is sent to OpenAI as a JSON-schema **enum**, with
`strict: true` and `additionalProperties: false`. The model physically cannot
return a value that isn't one of your 10 picklist entries — it's constrained at
the token level, not asked politely in a prompt.

`hear_source_detail` is deliberately open text, because per `system_prompt.md`
it's a normalized name (an event, a publication, a person, a community) and that
list can't be closed. Two hard rules from the prompt are enforced in code
anyway:

- `Unknown` must always have a blank detail.
- `Sales` detail must be exactly `Call`, `Email` or `LinkedIn`.
- Placeholder text (`Unspecified`, `Unknown`, `N/A`, `-`) is blanked.

## Detail aliases

The model normalizes spelling well but not *consistently* — the same input came
back as "Exit Five" on one run and "Exit Five Newsletter" on the next, which
splits your reporting across two picklist values. `DETAIL_ALIASES` in
`taxonomy.py` forces known names to one spelling after the model answers:

```python
"Exit Five Newsletter": ["exit five", "exit 5", "exitfive", ...],
"LinkedIn":             ["linked in", "linkden", "li", ...],
"X":                    ["x twitter", "twitter", "tweet", ...],
```

Matching ignores case, spaces and punctuation, so `EXIT-5`, `exitfive` and
`Exit 5` all resolve from the one variant `exit five`. Names not in the map pass
through as the model normalized them, so a new event or publication still works
the first time it appears — **add a line whenever the Sheets log shows the same
source arriving under two spellings.** `/status` fails if two canonical values
claim the same variant.

When a rule has to fire, the source is kept, the detail is dropped, and the
record is marked **Needs Review** (see below for where a human sees that).

> The model you pick must support structured outputs.

**Reasoning models need a generous output cap.** Reasoning tokens are billed as
completion tokens and count against `MAX_OUTPUT_TOKENS` in
`openai_functions.py`. The answer itself is ~25 tokens, but a genuinely ambiguous
multi-source answer has been measured at 330, nearly all of it reasoning. Too low
a cap spends the entire budget thinking and returns nothing — which is why the
default is 4000. Unused headroom is not billed. If the cap is ever hit the lead
fails loudly rather than having a fabricated `Unknown` written to it.

## Where "Needs Review" actually shows up

`needs-review` is an **activity attribute**, not a field on the lead. Today a
human sees it in three places:

1. **The lead's Activity Log** — the "Hear About Us is Categorized" row. Open it
   and the attributes are listed: the original text, the source and detail
   assigned, the model used, and Needs Review true/false.
2. **A smart list** — filter on `Hear About Us was Categorized` and constrain on
   the Needs Review attribute, then use that as your review queue. (Marketo
   exposes SSFS activity attributes as filter constraints; confirm it renders the
   way you want before you build a campaign on it.)
3. **The Google Sheet log** — a `needs_review` column, one row per lead,
   alongside the raw model answer. This is the practical place to work a review
   queue, because you can sort and filter it freely.

It is set to true when either (a) nothing could be extracted, so the answer
landed on `Unknown`, or (b) the model returned a detail that broke one of the
hard rules and had to be blanked.

**What it is not:** there is no Hear Source Review field on the lead record, so
it will not show up on the lead detail page and you cannot report on it in
Salesforce. If you want that, say so and I'll add a third mapping slot — you'd
create one more Marketo field and map it at install like the other two.

## taxonomy.py and system_prompt.md can't drift apart

`/status` parses the Hear Source list out of §1 of `system_prompt.md` and
compares it to the enum in `taxonomy.py`. If you add a source to one and forget
the other, `/status` returns `"status": "error"` and names the mismatch — and
the test fails before you deploy.

## What the flow step asks for

Nothing you need to fill in. Marketo requires a `primaryAttribute` and it cannot
be blank — it always renders on the flow step whether the service uses it or not
— so there is one dummy input:

```
Note (not used):  n/a
```

Its value is never read. Everything the service needs arrives through the field
mappings below, so there are no tokens to paste and nothing to get wrong.

The model and temperature are fixed in `routes.py` (`MODEL`, `TEMPERATURE`)
rather than exposed as flow-step inputs.

## Field mapping

All three fields are mapped by whoever installs the service, so no Marketo API
names live in this code:

| Slot | Direction | Map it to | |
|---|---|---|---|
| `hear_about_us` | Marketo → service | `How_did_you_hear_about_us__c` | **required** |
| `hear_source` | service → Marketo | your Hear Source field | optional |
| `hear_source_detail` | service → Marketo | your Hear Source Detail field | optional |

`hear_about_us` is marked `isRequired`, so the install cannot be completed
without pointing it at a real field. Rename any of them in Marketo later and you
re-map in the admin UI — no code change.

## How a record flows through

1. Every answer goes to OpenAI with `system_prompt.md` as the system message and
   the raw text as the user message — including non-answers like `n/a` and
   `test`, because §4 of the prompt already specifies how those are handled. The
   only input that doesn't make a call is a completely empty string.
2. The answer is checked against the hard rules in `taxonomy.py`.
3. `hear_source` + `hear_source_detail` go back to Marketo, along with the
   original text, the model used, and the Needs Review flag.
4. Every record is logged to Google Sheets — including the raw model answer, so
   you can audit any decision after the fact.

## Batches

A batch campaign can send up to 1000 leads per call (3000 leads from a campaign
arrives as three calls).

**The 202 goes back to Marketo immediately**, before any classification starts,
and the work continues on a background thread. This is what the 202 means, and
it matters: holding the connection open through a long batch trips Marketo's read
timeout and fails the whole step with
`SocketTimeoutException -- Read timed out`. `generateTicket` does the same —
see `docs/marketo-ssfs-guide.md` for the detail.

Within a batch, leads are classified across 8 threads (`MAX_WORKERS`) and
identical text is de-duplicated, so 100 people who typed "Google" cost one OpenAI
call rather than 100.

`leads_processed` in the batches tab updates every 10 seconds while a batch runs,
so you can watch progress and spot a stall.

Each batch finds its own row by searching the logged request payload for
Marketo's callback `token`, which is unique per invocation. The row number
returned by a Sheets append is **not** reliable — two batches arriving in the
same second can both be told they landed on the same row, and then one writes its
progress over the other's.

Marketo always puts `token` first in the payload, so it is always inside the
first request cell — the search only needs column F.

One caveat of backgrounding: if the server restarts mid-batch, in-flight work is
lost. The batch row is already on the sheet with the full request payload, and
`leads_processed` shows how far it got.

## Setup

1. Copy `.env.example` to `.env` in the repo root and fill it in (or set them in
   your host's secrets manager — the code reads either).
2. Create two tabs in the log spreadsheet: `hearSourceLeads` and
   `hearSourceBatches`.
3. Deploy, then install in Marketo Admin → Service Providers using
   `https://<your-host>/hearSource/install`.

## Reinstalling after a definition change

Marketo has **no refresh**. Any change to `getServiceDefinition` — a new flow
attribute, a new callback attribute, a changed field — requires **uninstalling
and reinstalling** the service in Marketo. Redeploying alone does nothing, and
the step will keep behaving as it did at install time.

Worse: **deleted names stay reserved.** The `apiName`, `triggerName` and
`filterName` remain taken after you delete a service, so a reinstall hits a name
collision and you have to increment them:

```python
"apiName": "hear-source-categorizer-2",
"triggerName": "Hear About Us is Categorized v2",
```

See `docs/marketo-ssfs-guide.md` §10 for the full list of quirks.

## Running it locally for a real Marketo test (ngrok)

Marketo has to reach your machine over HTTPS. `dev_ngrok.sh` in the repo root
starts the app and the tunnel together and prints the install URL:

```bash
./dev_ngrok.sh
```

Leave `PUBLIC_BASE_URL` **blank** in `.env` when you do this. `/install` reads
the tunnel hostname off the incoming request, so the swagger it hands Marketo
always points at the current ngrok URL — restart the tunnel, get a new URL, and
nothing needs editing.

The ngrok URL changes every restart on the free plan, and Marketo stores the
service URL at install time. So if you restart the tunnel mid-test, the existing
service in Marketo now points at a dead URL — delete and re-install it, or use
a reserved ngrok domain. Set `PUBLIC_BASE_URL` once you deploy somewhere stable.

`main.py` registers each service independently: if a service's dependencies
isn't installed locally, that service is skipped with a warning instead of
crashing the app, so you can tunnel `hearSource` on its own.

## Testing

```bash
python services/hearSource/test_local.py          # mocked, free
LIVE=1 python services/hearSource/test_local.py   # real OpenAI calls
```

The mocked run proves the wiring and the guardrails: endpoints answer, the
callback body is shaped correctly, `taxonomy.py` matches `system_prompt.md`, and
two deliberately bad model answers get coerced instead of reaching a lead
record.

The **LIVE run is the one that tests `system_prompt.md`**. It sends all 26
phrases — including the multi-source tie-break examples from §3 — to the real
model and prints any answer that doesn't match the expected one.

It is not optional theatre: the first two live runs caught two real defects the
mocked run could not see (a plugin mention returning "Knak Plugin" instead of
the fixed `Claude Plugin`, and "exitfive" drifting between two spellings across
runs). Run it after every change to `system_prompt.md` or `taxonomy.py`, and run
it more than once — the failures that matter most are the ones that only appear
on some runs.
