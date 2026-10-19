# Outreach landing builder

Local CSV → self-contained Armenian landing pages → desktop/mobile screenshots.
The existing service, passenger-transport, and automotive-detailing designs share
one Jinja2 template. No server, database, external API, deployment, or messaging.

## Windows setup and run

Python 3.10+ is required. In PowerShell:

```powershell
Set-Location C:\Users\SergeGalstyan\Development\outreach_landing_builder
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
New-Item -ItemType Directory -Force .\input | Out-Null
```

Put your CSV at `input/businesses.csv`, then run:

```powershell
.\.venv\Scripts\python.exe main.py
```

With an activated environment (`.\.venv\Scripts\Activate.ps1`), the command is
simply `python main.py`. Activation is optional; no execution-policy change is
needed. The three main settings are:

```python
input_csv = "input/businesses.csv"
output_directory = "output"
max_previews = 10
```

Paths resolve relative to `main.py`, regardless of the shell's current directory.
No manual place-ID selection is needed. `max_previews` limits **unique businesses
attempted**, including rendering/screenshot failures, rather than successes.
Rows are considered in their original order. All rows whose `processed` cell,
after trimming, equals `TRUE` in any casing are skipped. Blank, missing, `FALSE`,
and other values are pending. Processed rows are not checked for output files.

## Progress, validation, and recovery

Each completed business folder contains:

- `index.html`: all CSS, SVG, and supplied raster assets embedded.
- `desktop.png`: **1440 × 1000**, top-of-page viewport.
- `mobile.png`: **390 × 844**, top-of-page viewport.

Folders retain the safe business slug and deterministic 12-character place-ID
hash: `output/<slug>-<hash>/`. Armenian-only names use `business` as the slug.
The tool no longer generates `fullpage.png`; existing files, including old
full-page captures, are not automatically deleted.

The CSV is updated in place. A missing `processed` column is added; an existing
one is moved to the end when saving. The schema is normalized before processing.
Every other column, column order, and parsed cell value is preserved exactly,
including whitespace, IDs, phone apostrophes, Unicode, multiline values, and
unknown extra columns. Saves use UTF-8 BOM, standard CSV quoting, and an adjacent
temporary file followed by atomic replacement. Original byte-level quoting and
line endings may change, but cell values do not. Presentation whitespace may be
trimmed for rendering without changing stored cells.

A business is marked `TRUE` **only after HTML and both fresh screenshots succeed**.
PNG dimensions, complete chunks, and checksums are validated. Progress is saved
atomically after every successful business. If tracking cannot be saved, the
batch stops immediately with instructions to close Excel/check permissions and
rerun. The previous valid CSV and earlier committed completions remain intact.
The just-created output is retained but does not count as tracked completion.

Required `business_name` and `place_id` fields and duplicate IDs are validated
across the whole CSV before rendering. Invalid businesses are reported and left
pending; they do not consume the rendering-attempt limit. Identical duplicates
are generated once, and all their flags become `TRUE` together after success.
Duplicates are compared by **every original cell except `processed`**. Conflicting
duplicate IDs are reported and not rendered, including conflicts with a row
already marked `TRUE`. A mixed-status identical group is generated at its first
pending row's position; an already-TRUE duplicate alone never marks pending rows
complete. Missing required headers, duplicate/empty headers, or wrong cell counts
stop the run without rewriting malformed input.

For any unprocessed business, expected output files are regenerated/repaired even
if its folder exists. Partial or stale output never proves completion. Screenshot
failures leave the row pending, preserve successful HTML, and allow subsequent
businesses to continue. Missing Playwright/Chromium never marks a row complete.
If interrupted after output creation but before the CSV save, that business may
be regenerated on the next run. This is the intended simple recovery behavior.
To intentionally refresh a completed preview, clear its processed flag (and those
of any identical duplicates) before rerunning.

Run one instance at a time. An OS lock on an adjacent `<input>.csv.lock` sidecar
prevents two batches from processing the same CSV concurrently. The OS releases
the lock on exit/crash, so its small persistent file is harmless and ignored by
Git. The lock does not protect against manual CSV edits: close Excel and other
editors before running, and avoid concurrent runs using different CSVs with the
same output folder.

The summary reports already-processed **rows**, attempted/completed **businesses**,
failures, remaining pending **rows**, and output paths. Duplicate rows therefore
count individually in the row totals. Fatal input/tracking errors stop the batch;
`main.py` exits with status 1 for failures or a fatal error.

## Personalization and niches

Existing local `personalization.json` is read but never overwritten. Keys remain
exact place IDs, used for personalization rather than manual selection. Optional
settings in `main()` choose the default `niche` and `personalization_file`.
Supported explicit niches in `presets.json`:

- `general_service`: neutral business enquiry and a geometric motif.
- `passenger_transport`: route illustration, enquiry process, and transport FAQs.
- `detailing_cleaning`: charcoal automotive styling and the reusable detailing image.

Per-business `niche` wins over the default. Unsupported niches print a warning
and fall back to `general_service`; names/categories never determine a niche.
Raw Google category IDs are not displayed.

| Content | Highest to lowest priority |
| --- | --- |
| Header wordmark | `brand_name` → `display_name` → CSV `business_name` |
| H1 | `hero_title` → preset H1, never the business name |
| Hero description | `hero_description` → legacy `headline` → preset description |
| Introduction | `introduction` → preset introduction |
| Business-type label | `business_type_label` → preset label |
| FAQ | `faq` list → preset FAQ; `[]` hides the section |

For example, inside your local personalization file:

```json
{
  "your-exact-place-id": {
    "niche": "detailing_cleaning",
    "brand_name": "Ձեր բիզնեսի անունը",
    "hero_title": "Մաքրություն՝ մինչև վերջին դետալը։",
    "accent_color": "#aecbdc"
  }
}
```

`services` accepts `[{"title": "…", "description": "…"}]` and is never invented.
`faq` accepts `[{"question": "…", "answer": "…"}]`. Transport endpoints must be
explicit: `"route": {"from": "Երևան", "to": "Թբիլիսի"}`. Both endpoints are
required; route sections appear only for transport. Its illustration is schematic,
not a geographic map. No schedules, prices, duration, pickup, or availability are
inferred. Detailing defaults make no ceramic/PPF or other specific service claims.
Navigation follows the actual rendered sections. The compact menu and native FAQ
accordions retain keyboard support and a no-JavaScript fallback.

Optional images: `logo_path`, `hero_image_path`, `hero_image_position` (`center`,
`top`, or `bottom`), and `image_caption`. Customer logo takes precedence over
concept branding. Hero precedence: customer image → niche image → original graphic
fallback. The detailing niche image is
`assets/niches/detailing_cleaning/detailing_cleaning.png`, embedded in a 3:2 frame
and clearly labeled illustrative, not the customer's actual vehicle or premises.
Images are local PNG/JPEG/GIF/WebP, limited to 4 MiB and 24 megapixels each.
Invalid customer images fail clearly; an absent niche image prints a warning and
uses its graphic fallback. Supplied SVG uploads and remote images are rejected.

Jinja2 autoescaping, HTTP/HTTPS-only contact URLs, validated telephone links and
hex colors remain enabled. No raw user SVG, tracking, fake booking/enquiry form,
or inferred WhatsApp is included. Business facts come only from CSV and supplied
overrides. Fonts use local Armenian-capable fallbacks; no remote font requests.
System font appearance can differ between computers. Concept branding/notice and
HayWeb attribution remain in the footer. The original illustrative artwork in
`assets/examples/` has its own CC0 license; the supplied detailing image is separate.

## Fictional batch demonstration

`examples/batch.csv` contains two pending fictional businesses and one already
marked ` tRuE `. Copy it to a local working CSV; the tracked example stays unchanged:

```powershell
New-Item -ItemType Directory -Force .\output\batch-demo | Out-Null
if (-not (Test-Path .\output\batch-demo\businesses.csv)) {
    Copy-Item .\examples\batch.csv .\output\batch-demo\businesses.csv
}
.\.venv\Scripts\python.exe -c "from main import process_csv; process_csv('output/batch-demo/businesses.csv', 'output/batch-demo/previews', personalization_file='examples/batch_personalization.json')"
```

First run: one already processed row, two attempts, two completions, no failures,
zero remaining. Rerun: three processed rows, zero attempts. The fictional phone
numbers must not be called. Output locations:

- `output/batch-demo/previews/business-748d189c9102/` — transport example.
- `output/batch-demo/previews/business-8452d47497bd/` — detailing example.

Each contains HTML, `desktop.png`, and `mobile.png`. No new `fullpage.png` is created.
The real Chromium demonstration used one browser for both businesses, validated
both PNG dimensions, and the saved viewport screenshots were opened for visual
inspection. Your local customer personalization was left unchanged.

The standalone screenshot helper still captures existing local HTML without
changing any CSV flags:

```powershell
.\.venv\Scripts\python.exe screenshot.py .\output\batch-demo\previews\business-8452d47497bd\index.html
```

It accepts multiple paths, reuses one Chromium browser, blocks external requests,
waits for fonts and decoded images, disables animations, resets scrolling to zero
for **each** viewport, and uses bounded timeouts. PNGs are staged before atomic
replacement. An interrupted replacement can leave a partial pair; the CSV stays
pending and the next run repairs it. Browser resources close reliably.

## Tests and local data

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

All **43 tests passed**: the 29 existing template/safety tests and 14 batch tests
covering processed flags, cell preservation, duplicate handling, output validation,
missing dependencies, partial-output recovery, atomic save failures, persistence
of earlier progress, and single-instance locking. Browser integration was also
run with the two fictional businesses and a no-work rerun.

Customer CSVs, `personalization.json`, `assets/customers/`, output, secrets, virtual
environments, caches, and CSV lock sidecars remain ignored by Git. Reusable assets,
licenses, and fictional examples remain trackable. The existing HTML-only/manual
`generate()` utility remains available for compatibility; `main.py` always uses
this automatic CSV-tracked workflow with both screenshots required.
