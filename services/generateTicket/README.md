# Generate Ticket (SSFS)

Builds a personalized Knak event pass for every person in the flow, uploads it
to Design Studio, and writes its URL to a program member field so the pass can
be dropped into a confirmation email with a token.

The QR code on the pass encodes that person's Marketo lead id as
`knak-marketo-id=<lead_id>` — the format Knak's event check-in scanner reads —
so a badge printed from this pass works at the door with no extra work.

## Files

| File | What it does |
|---|---|
| `routes.py` | The Marketo endpoints, batch handling, callback and Sheets logging. |
| `image_functions.py` | **The pass itself** — QR generation, text fitting, and the measured layout constants. Edit this to change how a pass looks. |
| `_measure.py` | Re-measures a new template and prints the constants to paste into `image_functions.py`. Not used at runtime. |
| `swagger.json` | The install file Marketo reads. `/install` rewrites `servers[0].url`, so it never needs editing. |
| `test_local.py` | Offline smoke test. Run it before every deploy. |
| `assets/knak_event_pass.jpeg` | The 2524×1664 background template. |
| `assets/fonts/` | Telegraf Bold and Regular, Knak's brand typeface. |

## What the flow step asks for

Three inputs, typed on the flow step itself:

```
Event Name:      KnakCon 2026
Event Date:      OCT 26-27, 2026
Event Location:  AUSTIN, TX
```

They are printed on the pass exactly as typed, other than being upper-cased to
match the template's own lettering. **Event Name** is the `primaryAttribute`,
which Marketo requires and will not let you leave blank — here it does real
work rather than being a dummy.

There is no token picker on SSFS flow steps (guide §10), so if you want a token
in one of these, build it in a Change Data Value step and copy it across.

## Field mapping

One field is mapped by whoever installs the service, so no Marketo API name
lives in this code:

| Slot | Direction | Map it to | |
|---|---|---|---|
| `full_name` | Marketo → service | your full name field | **required** |

It is marked `isRequired`, so the install cannot be completed without pointing
it at a real field. Rename that field in Marketo later and you re-map in the
admin UI — no code change.

`callbackPayloadDef.fields` is **empty**, so Marketo skips the Map Incoming
Fields step entirely. The pass URL does not come back through the callback —
see below.

## Why the URL goes to a program member field, not the callback

The callback has no notion of which program the flow step ran in, so a URL
returned that way would have to land on a plain lead field and be overwritten
the next time that person attends anything. A program member field keeps one
pass per person **per program**, which is what an events team actually needs.

That means the pass URL is written over the Marketo REST API instead, by
`sync_program_member_field` in `marketo_functions.py`, 300 leads per call.

Two consequences worth knowing before you install:

1. **The field must already exist.** Create `ticketImage` (or whatever you set
   `TICKET_FIELD` to) in Admin → Program Member Fields first. The API cannot
   create it.
2. **The person must already be a program member.** That endpoint updates
   existing members only — it will not enroll anyone. A non-member comes back
   rejected, and the service counts that lead as failed and says so in
   `ticket_error`, because their pass exists but was never attached to them.
   Put a Change Program Status step ahead of Generate Ticket if this bites.

## What comes back

Nothing is written to the lead. Everything lands as activity attributes on the
**Ticket is Generated** activity, visible in the lead's Activity Log and usable
as smart list constraints:

| Attribute | |
|---|---|
| `event_name_value` | The resolved event name — the primary attribute shows the unresolved token |
| `event_date`, `event_location`, `full_name` | As printed on the pass |
| `program_name` | The program the step ran in |
| `ticket_image_url` | The Design Studio URL |
| `ticket_error` | Why it failed, when it did |

`ticket_image_url` is also on the program member record, which is where you
reference it from an email.

## Where the passes go

One Design Studio subfolder per program, created on first use, named after the
program — so passes for "2026 KnakCon" land in a folder called `2026 KnakCon`
inside whatever `TICKET_FOLDER_ID` points at.

Uploads use `insertOnly: false`, so re-running the campaign for the same person
**overwrites** their pass rather than failing on a duplicate name. Re-running
after fixing a typo in someone's name is safe.

## Two Marketo credentials, not one

`MARKETO_USER` / `MARKETO_PASSWORD` is the basic auth Marketo uses to call this
service. Every service in the repo needs it.

`MARKETO_BASE_URL` / `MARKETO_CLIENT_ID` / `MARKETO_CLIENT_SECRET` is how this
service calls Marketo — set it up in Admin → LaunchPoint as a Custom service,
with an API-Only user holding **Access API Files (read-write)** and **Access
API Program (read-write)**. Without those two roles, the upload or the program
member write fails for every lead.

Marketo allows 100 API calls per 20 seconds. `marketo_functions.py` paces every
call through a process-wide sliding-window limiter, so a 1000-lead batch does
not trip error 606.

## The layout is measured, not eyeballed

Every coordinate in `image_functions.py` was read off the template's own
pixels: the white QR card is exactly `(161, 949)`–`(773, 1583)`, the "HOLDER:"
label's left edge is `x=873`, and its 87px cap height is what sets the holder
name's 124pt Telegraf so the two lines match.

Replacing the template means re-measuring:

```bash
python services/generateTicket/_measure.py
```

It prints the card bounds, each baked-in text block's position, its actual
sampled color, and the Telegraf size matching its cap height — paste those into
`image_functions.py`.

Three details in there that are deliberate and easy to undo by accident:

- **The quiet zone is 28px, not 4 modules.** That matches the template's own
  baked-in QR, whose modules sit 27–29px in from every edge of the card, and is
  roughly 1.6 modules — well under the QR spec's usual minimum. It holds up
  because the card is a bright white island on a dark background with a glowing
  border, so a scanner finds its edges from the card itself rather than from the
  module-level quiet zone. It is measured in **pixels**, not modules, so a lead
  id long enough to push the code from 29 modules to 33 doesn't change how the
  pass looks for that one person. This is the reason `test_local.py` decodes
  every pass at five widths — if you widen the logo or shrink the margin, that
  test is what tells you it stopped scanning.
- **The QR card is 613×635, not square.** The code is centred on a white canvas
  of the card's shape rather than stretched to fill it. Stretching makes every
  module slightly taller than it is wide, which is the same distortion a phone
  camera at an angle introduces — no reason to spend that tolerance when white
  padding is free.
- **`QR_BOX` overshoots the card by 6px on every side.** The card has rounded
  corners, and a paste sized to fit inside that curve leaves a sliver of the
  template's own placeholder QR — finder squares included — showing at each
  corner.

Every text field auto-shrinks and word-wraps to fit its box, so a long event
name or a double-barrelled surname renders smaller instead of overflowing into
the artwork. If even the minimum size won't fit, it wraps past the line limit
rather than dropping words — a pass that's a little tall beats one missing half
of someone's name.

## Batches

A batch campaign can send up to 1000 leads per call (3000 leads from a campaign
arrives as three calls).

**The 202 goes back to Marketo immediately**, before any pass is generated, and
the work continues on a background thread. Holding the connection open through
a long batch trips Marketo's read timeout and fails the whole step with
`SocketTimeoutException -- Read timed out`.

Within a batch, passes are generated and uploaded across 10 threads
(`MAX_WORKERS`) — Marketo's own concurrent-call ceiling, so going wider just
queues behind the rate limiter.

`leads_processed` in the batches tab updates every 10 seconds while a batch
runs, so you can watch progress and spot a stall.

Each batch finds its own row by searching the logged request payload for
Marketo's callback `token`, which is unique per invocation. The row number
returned by a Sheets append is **not** reliable — two batches arriving in the
same second can both be told they landed on the same row, and then one writes
its progress over the other's.

One caveat of backgrounding: if the server restarts mid-batch, in-flight work
is lost. The batch row is already on the sheet with the full request payload,
and `leads_processed` shows how far it got.

## Setup

1. Copy `.env.example` to `.env` and fill in the Marketo REST values and
   `TICKET_FOLDER_ID` (or set them in your host's secrets manager — the code
   reads either).
2. Create the `ticketImage` program member field in Admin → Program Member
   Fields.
3. Create the parent Design Studio folder and put its id in `TICKET_FOLDER_ID`.
4. Create two tabs in the log spreadsheet: `generateTicketLeads` and
   `generateTicketBatches`.
5. Deploy, then install in Marketo Admin → Service Providers using
   `https://<your-host>/generateTicket/install`.

`/status` renders a real pass and checks the REST configuration, so hit it
before installing — it catches a missing template, an unreadable font or
half-filled credentials in one call, rather than one failed lead at a time.

## Testing

```bash
python services/generateTicket/test_local.py
```

Fully offline — Marketo is never called. It proves the endpoints answer, the
service definition is shaped the way Marketo demands, the log row has exactly
the columns the sheet is written with, and — the part that matters most — that
**every generated QR decodes back to `knak-marketo-id=<lead_id>`**, including
at 600px wide, the sort of size a pass is actually viewed at in an email.

Decoding needs opencv:

```bash
pip install opencv-python-headless
```

Without it the render checks still run and the decode checks are skipped out
loud, rather than passing quietly.

## Reinstalling after a definition change

Marketo has **no refresh**. Any change to `getServiceDefinition` — a new flow
attribute, a new callback attribute, a changed field — requires **uninstalling
and reinstalling** the service in Marketo. Redeploying alone does nothing.

Worse: **deleted names stay reserved.** The `apiName`, `triggerName` and
`filterName` remain taken after you delete a service, so a reinstall hits a
name collision and you have to increment them:

```python
"apiName": "generate-ticket-2",
"triggerName": "Ticket is Generated v2",
```

Changing only `image_functions.py` — the layout, the fonts, the template — is
**not** a definition change. Redeploy and the next batch picks it up.

See `docs/marketo-ssfs-guide.md` §10 for the full list of quirks.
