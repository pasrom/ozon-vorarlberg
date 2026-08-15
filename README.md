# Ozone Vorarlberg

Fetch the ozone readings of the four Vorarlberg monitoring stations, back them
with up to 39 years of measurement history, and show the result as a training
light.

**→ [pasrom.github.io/ozon-vorarlberg](https://pasrom.github.io/ozon-vorarlberg/)**

Two sources, deliberately combined:

| Source | What it gives | Lag |
|---|---|---|
| [vorarlberg-luft.at](https://www.vorarlberg-luft.at/tab1O3.htm) | the current value | none |
| EEA Air Quality e-Reporting | hourly values since 1988 | 1–25 hours |

```
python3 -m pip install -r requirements.txt
python3 eea_archive.py --build                     # full record (one-off, ~26 MB)
python3 ozon_vorarlberg.py --log --out data.json   # current values
python3 -m http.server 8000
open http://localhost:8000/index.html
```

Everything runs without the archive step too — just without long-term metrics.
To look at the dashboard without waiting hours for a series:

```
python3 ozon_vorarlberg.py --demo --out data.json  # synthetic values
```

The demo is flagged with a banner in the dashboard. Do not confuse the two.

---

## Repository layout

```
main    code and the site itself — GitHub Pages serves from here
data    data.json + archive.json, nothing else
```

The dashboard reads the `data` branch at runtime via
`raw.githubusercontent.com`, with local files taking precedence so a checkout
still works offline. That keeps the two apart: the logger pushes three times an
hour without ever touching the site or triggering a Pages rebuild.

The `data` branch is force-pushed as a single orphan commit. At this cadence a
real commit history would mean ~72 commits per day and roughly half a gigabyte
of git objects per year — for numbers the EEA archives permanently anyway. The
irreplaceable part of the record lives upstream, not here.

## Where the history comes from

**The official regional page has none.** It is a server-side generated static
HTML export (generator in the meta tag: InterConnect Software, footer
"Land Vorarlberg 2004"), rewritten hourly, carrying exactly five numbers per
station:

| Column | Meaning |
|---|---|
| Akt. Messwert 1-h | current hourly mean |
| Tagesmax. 1-h | highest hourly value today |
| Tagesmax. 8-h gleitend | highest running 8-hour mean today |
| Vortag 1-h / 8-h | the same two maxima for yesterday |

The linked station pages (`statATVA007.htm`) do offer a "graphical course of
the last 2 or 8 days" — but behind it sits nothing more than a pre-rendered
JPEG (`images/ATVA007O3EU.jpg`). No JSON, no XML, no API. Same story at the
Umweltbundesamt: the time-series tool at
`luft.umweltbundesamt.at/pub/map_chart/index.pl` accepts arbitrary dates and
intervals up to 28 days, and returns PNGs.

**The EEA has it.** The very same measurements sit as Parquet in publicly
readable Azure blob containers, no API key:

| Container | Dataset | Period | Per station |
|---|---|---|---|
| `airquality-p` | E2a, unverified | 2025-01-01 → now −1…25 h | ~0.6 MB |
| `airquality-p-e1a` | E1a, verified | 2013-01-01 → 2024-12-31 | ~2.0 MB |
| `airquality-p-airbase` | AIRBASE | from 1988 → 2012-12-31 | 1.4–3.5 MB |

```
https://eeadmz1batchservice02.blob.core.windows.net/<container>/AT/<point>.parquet
```

The sampling-point identifier has two parts and lines up with the Austrian
immission data network, where **08 is the Vorarlberg network**:

| EEA sampling point | Network | Station |
|---|---|---|
| `SPO.08.0706.983.7.1` | 08/0706 | Lustenau Wiesenrain |
| `SPO.08.2708.5527.7.1` | 08/2708 | Bludenz Herrengasse |
| `SPO.08.2801.3213.7.1` | 08/2801 | Wald am Arlberg S16 |
| `SPO.08.0503.3670.7.1` | 08/0503 | Sulzberg Gmeind |

The trailing `.7.` is the pollutant code for ozone.

Result: **1,010,324 hourly values**. The series have different lengths — the
airbase container reaches much further back than the EEA front end suggests:

| Station | From | Hours | Coverage |
|---|---|---|---|
| Lustenau Wiesenrain | 1988-01 | 322,405 | 95.2 % |
| Sulzberg Gmeind | 1989-05 | 305,518 | 93.6 % |
| Wald am Arlberg | 2003-01 | 194,280 | 93.8 % |
| Bludenz Herrengasse | 2004-01 | 188,121 | 94.9 % |

### Verified, not assumed

That these are the same measurements is checked against **23 independent
reference points**, all exact:

| Check | Reference | Result |
|---|---|---|
| 11 hourly values (3 times × 4 stations) | screenshots 2026-07-30 | 11/11 exact |
| daily 8h max up to 13:00, 4 stations | screenshots 2026-07-30 | 4/4 exact |
| daily 1h max, 4 stations | previous-day column 2026-08-14 | 4/4 exact |
| daily 8h max, 4 stations | previous-day column 2026-08-14 | 4/4 exact |

The EEA delivers unrounded values (`138.487` instead of `138`); the regional
page rounds to whole µg/m³. All of these run as tests in
`test_eea_archive.TestAgainstOfficialSource` and skip themselves when the
parquet cache is absent.

### Two time conventions, both guessed wrong at first

This was the hardest part and the place where silent one-hour errors live:

**1. `Start` holds UTC** and marks the beginning of the averaged hour. The
regional page labels by the **end** of the hour and in local time: the value
shown there as "13:00" sits in the EEA data under `Start = 10:00` (= 12:00
CEST, window 12–13 local).

An earlier assumption of "fixed CET" was one hour off. It came from a phase
correlation against model data that barely separates 0 from −1 h (r 0.87 vs
0.86). **Exact integer hits on 11 values beat correlation** — which became the
rule: pin conventions against the source itself, never against a proxy.

**2. Daily metrics are aggregated in fixed CET** (UTC+1, no daylight saving),
even though individual values are displayed in local time. Two different
things, easy to conflate. With the day boundary in CEST only 3 of 8 daily
maxima matched; with fixed CET all 8 did.

On top of that the EU convention: an 8-hour mean belongs to the day on which it
**ends**, with 24:00 still counting towards the old day. The first window of a
day therefore starts the previous evening — and on quiet mornings it is often
already the daily maximum. That is exactly how the regional page arrives at
107 µg/m³ for Bludenz at 13:00 on 30 July, when no window of that morning
exceeded 94.

`EEA_TZ` and `AGG_TZ` in `eea_archive.py` pin both down; seven tests hold them.

The **daily cycle**, by contrast, is labelled by window *start* in local time:
"06" means the hour 06:00–07:00. That is the reading a training window needs
("head out at 06:00").

## The two assessment axes

This is the substantive correction over the first version. There the acute
1-hour value was shown in large type while the traffic light was coloured by
the **daily 8-hour maximum**. That is misleading: around midday the running
8-hour mean still drags the cool morning hours along and sits well below what
is actually in the air. On 2026-07-30 at 13:00 Bludenz read 130 µg/m³ acute but
only 107 in the 8-hour mean — the card went green-yellow while 130 was outside.

Now separated:

**1. Training light** — runs on the **current 1-hour value**, i.e. on what you
are breathing right now.

| µg/m³ (1 h) | Status | Word | Meaning |
|---|---|---|---|
| 0–99 | good | free | full session possible, intervals included |
| 100–119 | warning | ok | short sessions fine, shorten long hard blocks |
| 120–179 | serious | easy | base pace only; move intervals to tomorrow morning |
| 180+ | critical | indoors | information threshold, cancel outdoor sport |

The 100 and 120 marks are borrowed from the 8-hour guidelines. That makes this
a **pragmatic scale, not a legal limit** — the dashboard says so too. Only the
180 is an actual statutory value.

**2. Daily assessment** — runs on the **daily 8-hour maximum** and is the
health context of the day, measured against the real reference values:

| Value | Origin |
|---|---|
| 60 µg/m³ | WHO 2021, long-term: mean of the daily 8h maxima over the six most ozone-rich months |
| 100 µg/m³ | WHO 2021, short-term guideline, max. daily 8h mean. Purely health-based |
| 120 µg/m³ | EU target value, running 8 h. May be exceeded on a limited number of days (max. 25, averaged over three years) |
| 180 µg/m³ | Austrian information threshold, 1h mean. From here the public is actively warned |

That the WHO values are stricter than the statutory ones is no contradiction:
the WHO values are derived purely epidemiologically, the statutory ones are
politically and technically feasible compromises.

**3. Long-term comparison** — from the archive: where does today's daily value
sit relative to the same calendar window (±3 days) of all previous years?
Today's value for this comes from the **live source**, not the archive — the
archive lags 1 to 25 hours behind, so its maximum for the running day would be
a morning value. Exactly that error produced reproducibly absurd percentiles
(Lustenau 67 instead of 163 µg/m³) before the responsibilities were split.

## Files

| File | Purpose |
|---|---|
| `ozon_vorarlberg.py` | scraper, history logger, assessment |
| `eea_archive.py` | fetch the EEA archive, cache it, condense it into metrics |
| `index.html` | the dashboard. One file, no external libraries |
| `deploy.sh` | fetch, build, push. Lock, heartbeat, Telegram on failure |
| `refresh_archive.sh` | pull the EEA archive forward (daily) |
| `mini/install.sh` | setup on the agent server, without root |
| `mini/*.plist` | the two LaunchDaemons |
| `fixtures/tab1O3_live.htm` | real dump of the source page, unmodified. Basis of the tests |
| `fixtures/tab1O3_minimal.htm` | hand-written minimal fixture (older structure) |
| `test_ozon_vorarlberg.py` | 60 tests |
| `test_eea_archive.py` | 73 tests; the source comparisons need the cache, the rest runs offline |

`python3 -m unittest -v` runs all 133.

## CLI

```
python3 ozon_vorarlberg.py                       # JSON on stdout
python3 ozon_vorarlberg.py --compact             # plain text per station
python3 ozon_vorarlberg.py --log --out data.json # the cron invocation
python3 ozon_vorarlberg.py --station sulzberg    # filter
python3 ozon_vorarlberg.py --html fixtures/tab1O3_live.htm   # offline
python3 ozon_vorarlberg.py --no-archive          # ignore the archive
python3 ozon_vorarlberg.py --strict              # abort on a layout change

python3 eea_archive.py --build                   # fetch + archive.json
python3 eea_archive.py --build --since 2003      # shorten the series
python3 eea_archive.py --stats                   # print the metrics
python3 eea_archive.py --coverage                # coverage per station/year
python3 eea_archive.py --build --refresh         # discard the cache
```

`--compact` with the archive present:

```
# 15.08.2026 11:00  (source: vorarlberg-luft.at)
warning   ok       Lustenau         akt= 101 ▼  1h-Max= 163 8h-Max= 155 [above EU target]
serious   easy     Bludenz          akt= 130 ▼  1h-Max= 159 8h-Max= 150 [above EU target]
good      free     Wald a. Arlberg  akt=  94 ▼  1h-Max= 147 8h-Max= 136 [above EU target]
serious   easy     Sulzberg         akt= 154 ▲  1h-Max= 158 8h-Max= 152 [above EU target]

-> Cleanest station: Wald a. Arlberg (94 ug/m3)
-> Archive: 1010324 hourly values, 1988-2026
-> Best training window (1988-2026, season data): 06-09, median 37 ug/m3
   Lustenau         today 163 =  92th percentile (median 112, max 237 since 1988)
```

### `--strict`

Without `--strict` the parser is tolerant and returns whatever it finds. With
it, the run aborts with exit code 2 when a station is missing, the timestamp is
unreadable, or the page's own threshold row is no longer
`180 / 180 / 120 / 180 / 120`. The latter is the canary for a column reshuffle:
the page documents its own limits in the last table row. For the cron job
`--strict` is the right choice — a hard error beats silently misassigned
numbers.

## What the archive says

From `eea_archive.py --stats`, as of August 2026:

- **Best training window: 06–09** at the valley stations, median 35–38 µg/m³.
  The rule of thumb "mornings in the valley" is now computed, not just
  plausible.
- **Sulzberg has a different window: 09–12**, and even there the median is
  79 µg/m³ — more than twice the valley's best. The high-altitude station
  barely has a daily cycle; its minimum falls in the late morning. For ozone
  the valley floor in the morning clearly beats the mountain tour.
- **Long-term decline, most visible at altitude.** Sulzberg: 128 µg/m³
  peak-season mean in 1990 → 97–103 in 2024/25. Lustenau: 100–107 in 1988–90 →
  88–94 in 2024/25. Air quality policy is working, slowly.
- **2003 was the exceptional summer**: Lustenau 121.5 µg/m³, 101 days above the
  EU target, 82 hours above the information threshold. No year since came close.
- **2024 was the cleanest year** of the record: Lustenau 88.1, only 2 days
  above 120.
- **2026 stands out**: by mid-August already 107.8 peak-season mean and 43 days
  above 120 at Lustenau — higher than 2018 (107.6) and the second-highest of
  the whole record. The year is not over, so that is an interim figure (drawn
  hollow in the dashboard).
- **The EU target is missed in roughly every second year**: 25 days are allowed
  averaged over three years. Very unevenly spread — Sulzberg 87 % of years,
  Lustenau 65 %, Bludenz 23 %, Wald am Arlberg 9 %.
- The distance to the **WHO long-term goal of 60 µg/m³** is large and stable:
  no station was ever below 80 in the entire record.

## How accurate is the history?

- **Agreement with the source: 23/23 reference points exact** (see above).
- **Resolution**: the EEA delivers unrounded floats, the regional page rounds
  to whole µg/m³. Rounding is the only difference between the two.
- **Validity filter**: of 1,060,448 rows, 1,010,324 are valid (95.3 %); 50,124
  carry flag `-1` (invalid) and are dropped. No other codes occur. An invalid
  value is worse than a gap.
- **Gaps**: around 8,500 per station across the whole record, the vast majority
  single hours — the automatic calibration cycles of the reference instruments,
  roughly 220 per year. Only 10–44 gaps per station exceed 24 hours.
- **Two real outages**: Sulzberg is missing all of 2000 (8,820 h), Wald am
  Arlberg 1,428 h from February 2003. Both years drop out of the metrics
  automatically via the 80 % coverage threshold.
- **Gaps are not interpolated**: `rolling_8h` breaks a window at gaps rather
  than averaging across, and requires at least 6 of 8 hours. As a result
  0.01–0.05 % of days have no 8h maximum — negligible, but it does mean
  exceedance days can be marginally undercounted.
- **2025/2026 is unverified** (E2a). Those values may be corrected later;
  `--refresh` picks corrections up. From 2024 backwards the data is
  quality-assured (E1a and AIRBASE).

## Operation on the agent server

The logger runs on the Mac mini as the `agent` user, following the conventions
in `tools-workflow/concepts/mac-mini-agent-server.md`:

| | |
|---|---|
| `io.ebs.agent.ozon` | three times per hour (:07, :27, :47) — scrape, build, push to `data` |
| `io.ebs.agent.ozon-archive` | daily 04:17 — pull the EEA archive forward |

Both are **LaunchDaemons** in `/Library/LaunchDaemons/` with `UserName=agent`,
not LaunchAgents: per-user agents only run while that user has an active GUI
session and would be dead after a reboot of the headless server.

```
ssh agent@mac-mini
git clone git@github.com:pasrom/ozon-vorarlberg.git ~/git/ozon-vorarlberg
cd ~/git/ozon-vorarlberg && ./mini/install.sh
```

`install.sh` handles everything that works without root — venv, dependencies,
deploy key, archive, test run — and prints the sudo block for the two daemons
at the end.

### Why it is built this way

- **`StartCalendarInterval`, not `StartInterval`.** If the machine was off at
  the scheduled time the run is skipped rather than fired retroactively —
  otherwise a power cut is followed by a stampede of simultaneous runs.
- **`HOME`, `PATH`, `LANG` live in the plist.** launchd does not set `$HOME`
  and does not read `.zshenv`; without those three, `git` and `gh` fail.
- **Lock via `mkdir`.** macOS has no `flock(1)`. The `trap` clears the lock on
  every exit, including on a signal. If it stands for more than 30 minutes the
  job reports that via Telegram but does not remove it — that would allow two
  runs in parallel.
- **Its own venv instead of the system python.** The mini's system python 3.9
  lacks the dependencies and is externally managed; `agent` is non-admin and
  cannot `brew install`. The venv builds on the existing Homebrew python 3.12.
- **Deploy key instead of the credential helper.** launchd jobs have no
  keychain access; over HTTPS the push would hang silently. `deploy.sh`
  therefore aborts when `origin` is HTTPS rather than trying.
- **Telegram on failure only**, through the existing `~/agents/bin/notify.sh`.
  Successful runs stay silent, otherwise the channel turns into noise.
- **Logs under `~/agents/logs/ozon/`**, where the `newsyslog` rotation applies
  (14 days, bzip2). Heartbeat after every successful run in
  `~/agents/state/ozon/heartbeat`.

### Operating it

```
# Status. Plain `launchctl list` shows only the user domain and will NOT find
# system daemons — which looks exactly as if nothing were installed.
launchctl print system/io.ebs.agent.ozon | grep -E "state|runs|last exit"
sudo launchctl list | grep io.ebs.agent.ozon

sudo launchctl kickstart -k system/io.ebs.agent.ozon  # fire immediately
tail -f ~agent/agents/logs/ozon/$(date +%F).log       # follow
date -r $(cat ~agent/agents/state/ozon/heartbeat)     # last success

sudo launchctl bootout system/io.ebs.agent.ozon       # stop
```

## The dashboard

A single HTML file, no external requests, no build step. It must be served over
HTTP rather than opened by double-click — under `file://` the browser blocks
reading `data.json`. The dashboard says so explicitly instead of staying blank.

- **Hero** — situation now at the highest station, cleanest station, best time
  window, today's long-term percentile.
- **Station tiles** — acute value, trend against the previous log entry,
  sparkline, daily assessment, altitude, long-term percentile.
- **Series** — 24/48/72 h, assembled from **two sources**: the EEA archive for
  the initial fill, the local log for the recent hours. The card text names how
  many values come from which source. Reference lines at 100/120/180, crosshair
  with values for every series, by mouse or arrow keys. Gaps over 2 h are
  **not** interpolated so a missed cron run does not run through as a smooth
  line; isolated points are drawn as points so a lone daily peak does not
  become invisible.
- **Daily cycle** — median per hour from five years of season data
  (April–September). Selecting a single station in the legend shows its
  25th–75th percentile band; with four stations at once four bands would be
  mush. Without the archive the card falls back to the locally logged days.
- **Annual series** — since 1988, switchable between peak-season mean, days
  above the EU target and highest 1-hour value. Three metrics with two units,
  hence a switch and always only **one** y-axis — never two scales in one plot.
- **Table view** — the same numbers without colour coding.
- **Pattern** — switches the lines to distinguishable dash patterns, for colour
  vision deficiency, print and black and white.

Auto-refresh every 5 minutes. While reloading, the previous render is held at
reduced opacity — no skeleton flash, no layout jump.

### Colours

Four stations = four categorical series colours (blue, orange, aqua, yellow),
assigned per station via the `slot` field in the JSON. Colour follows the
station, not its current rank — repainting survivors when a filter changes
misleads the reader.

The status colours (good/warning/serious/critical) are kept separate and are
**never** used as a series colour. They always appear with a glyph and a word
so the meaning never rests on colour alone.

The palette was checked with the validator (lightness band, chroma floor, CVD
separation, normal-vision floor, contrast) and clears the gates for line charts
in both light and dark. With four series on screen, yellow and orange fall
below the floor in the all-pairs comparison — no four-colour set clears it in
dark mode. Hence every line also carries **direct labels at its end**, a table
view exists, and pattern mode supplies the secondary encoding through shape.
Both colour schemes are chosen independently, not auto-inverted.

## Limits and pitfalls

- **The archive is rewritten only about once per day.** Measured from the
  blob's `Last-Modified`: written 2026-08-14 at 08:12 UTC and unchanged 12.5
  hours later. The data lag therefore swings between roughly **1 hour** right
  after a write and roughly **25 hours** just before the next. As a filler for
  the last few hours the archive is unusable — it supplies the long-term
  metrics, the 72 h window is carried by the local log. `archive.json` records
  the `Last-Modified` under `upstream` so the cadence becomes evidence rather
  than guesswork.
- **So the logger really has to run.** Without it the series is empty apart
  from whatever the archive held at the last run — in the worst case 25 hours
  old. The page flags that with the "data is stale" banner, but only the cron
  job can fix it.
- **Two labellings in the same JSON.** The series (`history`) runs by window
  *end*, like the regional page. The daily cycle (`hour_profile`) runs by
  window *start*, because "06" there means the hour you head out in.
  `eea_archive.recent_series` converts specifically for this; without it the
  archive and the local log would sit an hour apart.
- **Four ozone stations, no more.** The immission data network knows eight
  Vorarlberg stations (additionally Höchst Gemeindeamt, Lustenau Zollamt,
  Dornbirn Stadtstraße, Feldkirch Bärenkreuzung), but vorarlberg-luft.at shows
  only four for ozone.
- **Ozone only.** The source has further tabs (NO2, PM10, PM2.5, CO) and the
  station pages include those values for free; deliberately ignored here.
- **Altitudes** in the tiles are approximations and do **not** come from the
  source.
- **Trend** is the step to the previous log entry, not a smoothed slope. After
  a missed cron run it compares across the gap.
- **The source layout is stable but not guaranteed.** It is from 2004. Station
  IDs are read from the detail links (`statATVA007.htm` → `ATVA007`), which is
  more stable than name matching; if the links disappear the name serves as a
  fallback. `--strict` reports when both break.
- **The parser stumbled on nested tables.** The page wraps the values table in
  a layout table, so the outer `<tr>` also carries the first station link and
  was read as a data row — Bludenz came out as `akt=None, max_1h=14` while the
  other three were fine. A real page dump now sits in the repo as a fixture for
  exactly this; the old hand-written fixture had no such nesting.

## Dead ends (so nobody walks them twice)

- `data.gv.at` CKAN API: every path under `/katalog/api/3/action/…` returns 404.
- The EEA legacy interface `fme.discomap.eea.europa.eu/…/AQData_Extract.fmw` is
  switched off, HTTP 401.
- The new EEA API's `/ParquetFile/urls` endpoint returns 0 hits for `dataset:1`
  and `dataset:2` for **every** country (DE and CH checked too). Only
  `dataset:3` returns anything. Direct blob access sidesteps the problem.
- OpenAQ v3 needs a free API key (without: HTTP 401), v2 is retired with 410.
  Since OpenAQ ingests the same EEA data, it gains nothing here.
- **Open-Meteo Air Quality** provides keyless hourly ozone with history and
  forecast, but it is a **CAMS model**, not a measurement: systematically
  25–50 µg/m³ too low (Bludenz 97 instead of 146), and for Wald am Arlberg the
  model cell picks a 1978 m summit rather than the valley station. Useless as a
  substitute for measurements. As a *forecast* it would be interesting — not
  wired up so far.

## Open next steps

- Wire Open-Meteo in as an ozone **forecast** for tomorrow (bias-corrected
  against the local record — there are now up to 39 years of reference).
- ESP32 traffic light with an RGB LED pulling `data.json` from a small service.
- Home Assistant sensor.
- Add the four extra stations from the immission data network.
- Own sensing: for the sports use case a good electrochemical sensor with NO2
  correction is enough (the Alphasense OX-B431 measures OX = O3 + NO2 and needs
  a second sensor to separate them). Absolute accuracy against the official
  stations is hard with cheap sensors; UV absorption at 254 nm is the reference
  method the authorities use.

## Sources

- vorarlberg-luft.at, `tab1O3.htm` (Province of Vorarlberg, Umweltinstitut)
- EEA Air Quality e-Reporting, Parquet blobs (E2a / E1a / AIRBASE)
- Umweltbundesamt Austria, immission data network
  (`luft.umweltbundesamt.at/pub/map_chart/index.pl`) — for the station codes
- WHO global air quality guidelines 2021, Table 3.10
- IARC monographs on carcinogenicity (ozone is **not** classified as
  carcinogenic — it is an irritant gas, not a DNA-damaging carcinogen; the
  long-term risks run through oxidative stress and inflammation)
- Jerrett et al. 2009 (ACS study, respiratory mortality); Children's Health
  Study (lung growth and new-onset asthma in children)
