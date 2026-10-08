# Marketo Self-Service Flow Steps

Python services that back Marketo self-service flow steps (SSFS). Marketo sends
lead data to an endpoint here, the service processes it and calls back to update
the lead.

Adapted from [The Workflow Pro's Replit template](https://theworkflowpro.com/marketo-self-service-flow-steps/).

## Services

| Service | What it does |
|---|---|
| [`hearSource`](services/hearSource) | Categorizes the free-text "How did you hear about us?" answer into **Hear Source** and **Hear Source Detail** using OpenAI. |
| [`generateTicket`](services/generateTicket) | Builds a personalized Knak event pass with a check-in QR code, uploads it to Design Studio, and writes its URL to a program member field. |

## Layout

```
main.py                        router - registers each service's blueprint
googlesheets_functions.py      logging helpers (append, update, find row)
marketo_functions.py           Marketo REST API helpers (generateTicket)
requirements.txt               dependencies for a plain venv
docs/marketo-ssfs-guide.md     how SSFS actually work, and their quirks
services/<name>/
    routes.py                  the endpoints and the service definition
    swagger.json               the install file Marketo reads
    README.md                  that service's fields, inputs and quirks
    system_prompt.md           (hearSource) the classification rules
    taxonomy.py                (hearSource) allowed values and guardrails
    test_local.py              smoke test against a fake Marketo payload
    assets/                    (generateTicket) pass template, logo, fonts
    image_functions.py         (generateTicket) QR + pass compositing
    _measure.py                (generateTicket) re-measures a new template
```

## Two kinds of Marketo credential

`MARKETO_USER` / `MARKETO_PASSWORD` are the basic auth **Marketo uses to call
this app**. Every service needs them.

`MARKETO_BASE_URL` / `MARKETO_CLIENT_ID` / `MARKETO_CLIENT_SECRET` are how
**this app calls Marketo's REST API**, which is a separate integration set up in
Admin → LaunchPoint. Only `generateTicket` needs them, because uploading a file
to Design Studio and writing a program member field cannot be done through the
SSFS callback.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill it in
./.venv/bin/python main.py
```

Python 3.10 or newer — the code uses `str | None` syntax.

Any host works, since the code reads plain environment variables, so a
platform's own secrets manager does the same job as `.env`.

That is the whole setup — `generateTicket` renders a pass immediately, using a
bundled open font. Knak's own brand typeface is licensed and so is not in this
repo; drop the files into `services/generateTicket/assets/fonts/` and the pass
picks them up on its own, no configuration. See
[`services/generateTicket/assets/fonts/README.md`](services/generateTicket/assets/fonts/README.md).

## Testing against a real Marketo instance

Marketo needs to reach the service over HTTPS. `dev_ngrok.sh` starts the app and
a tunnel together and prints the install URL:

```bash
./dev_ngrok.sh
```

Then in Marketo: Admin → Service Providers → New Service, using
`https://<host>/<serviceName>/install`.

## Read this before changing a service definition

`docs/marketo-ssfs-guide.md` covers the things that cost the most time:

- Marketo has **no refresh** — a changed service definition means uninstall and
  reinstall, and deleted names stay reserved.
- Return the **202 before processing**, not after, or a large batch trips
  Marketo's read timeout.
- `userDrivenMapping` completely changes how `fields` behaves.
- The primary attribute can't be blank, can't appear in the callback attributes,
  and its tokens don't resolve in the activity log.

## Per-service docs

Each service has its own README with its fields, flow-step inputs and quirks —
see [`services/hearSource/README.md`](services/hearSource/README.md) and
[`services/generateTicket/README.md`](services/generateTicket/README.md).

## License

MIT — see [LICENSE](LICENSE).

The Knak brand assets in this repository are not covered by that license — the
event pass template and the Knak logo remain Knak's.

The bundled [Chivo](services/generateTicket/assets/fonts/OFL-Chivo.txt) font is
under the SIL Open Font License. Knak's Telegraf typeface is licensed from
[Pangram Pangram Foundry](https://pangrampangram.com) and is deliberately not
included here.
