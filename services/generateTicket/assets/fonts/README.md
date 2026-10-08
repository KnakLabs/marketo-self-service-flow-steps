# Fonts

**This works out of the box.** Clone the repo and the pass renders — no font
setup, nothing to download.

| | |
|---|---|
| `Chivo-Regular.ttf`, `Chivo-Bold.ttf` | Committed. [SIL Open Font License](OFL-Chivo.txt), so redistributing them here is fine. |
| `Telegraf-Regular.otf`, `Telegraf-Bold.otf` | **Not** committed. Knak's brand typeface — add them yourself. |

`image_functions.py` uses Telegraf whenever both of its files are present and
falls back to Chivo otherwise. There is no setting to flip: drop the files in
and the next pass is set in Telegraf. `/generateTicket/status` reports which
face is actually in use, so you can tell at a glance whether a deployment got
them.

## For Knak: adding Telegraf

Copy both files into this directory from the Knak Marketing Design System,
where the rest of the brand assets live:

```
services/generateTicket/assets/fonts/
    Telegraf-Bold.otf
    Telegraf-Regular.otf
```

They are gitignored, so there is no risk of committing them by accident.

If the fonts live elsewhere on the host — a shared volume, a mount in a
container image — point `FONT_DIR` at that directory instead:

```
FONT_DIR=/opt/fonts/knak
```

## Why Telegraf isn't in the repo

It is licensed from [Pangram Pangram Foundry](https://pangrampangram.com).
That license covers Knak *using* the typeface; it does not cover redistributing
the font binaries, which is what committing them to a public repository would
do.

## Why Chivo specifically

It was measured against Telegraf, not picked by eye. Candidates were rendered at
a cap height matched to Telegraf's and compared on the strings the pass actually
sets:

| Font | Bold width | Regular width |
|---|---|---|
| **Chivo** | 0.93× | **0.98×** |
| Archivo | 0.94× | 0.94× |
| Familjen Grotesk | 0.89× | 0.92× |
| Space Grotesk | 0.89× | 0.93× |
| Manrope | 0.87× | 0.88× |

Chivo sets closest to Telegraf's width, so the layout boxes calibrated for
Telegraf still hold and the fallback doesn't need its own geometry. It is also
the smallest of the finalists (88KB per weight against Archivo's 285KB).

## Using a different typeface

Add a `FontSet` to `_FONT_SETS` in `image_functions.py`, in preference order.
Each entry carries **its own start sizes**, and they are not interchangeable:

```python
FontSet("Telegraf", FONT_DIR / "Telegraf-Bold.otf", FONT_DIR / "Telegraf-Regular.otf",
        event_name=130, name=124, detail=110),
```

`name=124` is the point size at which Telegraf Regular renders an **87px cap
height**, which is exactly the cap height of the `HOLDER:` label baked into the
template artwork — that is what makes the holder's name line up with its own
label. A different face has different metrics at the same point size, so derive
the numbers rather than copying them:

```bash
python services/generateTicket/_measure.py
```

Prefer static font files over variable ones. Variable fonts need PIL to be built
against a FreeType with variation support, and a named instance that is missing
fails quietly at the wrong weight. Static instances can be cut from a variable
font with:

```bash
python -m fontTools.varLib.instancer Chivo.ttf wght=700 -o Chivo-Bold.ttf
```
