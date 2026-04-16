# Echo 655 Remote Control

A Python library, REST API, and web UI for controlling a Beckman Coulter
**Echo 655** acoustic liquid dispenser over the network.

The protocol was **reverse-engineered from Wireshark captures** of the
vendor's client talking to the instrument. This project reconstructs 
the wire protocol and exposes it as:

1. A **Python library** (`echo_client.py`) — stdlib-only, drop-in
2. A **REST API** (`echo_api.py`) — FastAPI, with Server-Sent Events
3. A **web UI** (`static/`) — plain HTML + JS, embedded in the API server

All three layers are usable independently.

> ⚠️ **This is reverse-engineered software.** It is not endorsed by or
> affiliated with Beckman Coulter / Labcyte. Use at your own risk. It has
> been tested against firmware **3.2.2** on an **Echo 655**.
> Other models (Echo 525/550/575, 650T) may speak similar
> SOAP but haven't been tested.

---

## Table of contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Using the Python library](#using-the-python-library)
- [Using the REST API](#using-the-rest-api)
- [Using the web UI](#using-the-web-ui)
- [Pick-list CSV format](#pick-list-csv-format)
- [Protocol reference](#protocol-reference)
- [Protocol quirks (read this)](#protocol-quirks-read-this)
- [Reverse-engineering new features](#reverse-engineering-new-features)
- [Known limitations and roadmap](#known-limitations-and-roadmap)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Web browser                          │
│                  (plain HTML + JS)                       │
└───────────┬───────────────────────────────┬─────────────┘
            │ fetch(/api/...)               │ EventSource(/api/events)
            │                               │ (SSE for live event stream)
            ▼                               ▼
┌─────────────────────────────────────────────────────────┐
│     echo_api.py   (FastAPI REST + static file server)   │
│     - ~28 REST endpoints                                │
│     - SSE fan-out for event stream                      │
│     - Single-client model (one EchoClient instance)     │
└───────────────────────┬─────────────────────────────────┘
                        │ Python calls
                        ▼
┌─────────────────────────────────────────────────────────┐
│     echo_client.py   (stdlib-only library)              │
│     - SOAP/gzip over HTTP on port 8000 (RPC)           │
│     - Event stream listener on port 8010               │
└───────────────────────┬─────────────────────────────────┘
                        │ TCP
                        ▼
┌─────────────────────────────────────────────────────────┐
│                     Echo 655                             │
│      192.168.0.26 : 8000 (SOAP)                         │
│                   : 8010 (event push)                   │
└─────────────────────────────────────────────────────────┘
```

**Important:** the Echo instrument is itself a SOAP server. No vendor
software needs to be running on any PC. Our Python code is a client; it
talks directly to the instrument's IP. This means:

- You can run `echo_client.py` or `echo_api.py` from any host with a
  network route to the Echo — the original PC, a lab server, a Raspberry
  Pi, a laptop on the bench, etc.
- You don't need Echo Client Utility, VWORKS, or any Labcyte / Beckman
  drivers installed.
- The only caveat: the Echo appears to expect **one commanding client at
  a time** (it has a `GetInstrumentLockState`/`LockID` mechanism). If
  Echo Client Utility or VWORKS is *actively running* against the same
  instrument, stop it before using this code.

---

## Quick start

### Prerequisites

- Python 3.9 or newer
- An Echo 655 reachable from your machine on the network
- Its IP address (example here: `192.168.0.26`)

### Install

```bash
cd /path/to/echoInit
python3 -m pip install -r requirements.txt
```

`requirements.txt` contains only:

- `fastapi` (for the REST API)
- `uvicorn[standard]` (ASGI server)
- `python-multipart` (for CSV file uploads)

The standalone Python library has **zero third-party dependencies** — it
uses only the standard library. You only need the packages above if you
want the REST API / UI.

### Run the web UI

```bash
python3 echo_api.py
```

Then open **http://127.0.0.1:8765** in a browser. Enter the Echo's IP,
click **Connect**, and you're in.

### Run scripts directly

Without the UI or REST server, you can drive the Echo from a Python
script:

```bash
python3 echo_client.py 192.168.0.26                      # init + home
python3 echo_client.py 192.168.0.26 --init-only
python3 echo_client.py 192.168.0.26 --home-only
python3 echo_client.py 192.168.0.26 --load-src 384LDV_DMSO
python3 echo_client.py 192.168.0.26 --load-dst 384_CellVis
python3 echo_client.py 192.168.0.26 --survey-src 384PP_DMSO2
python3 echo_client.py 192.168.0.26 --dry
python3 echo_client.py 192.168.0.26 --eject-src
python3 echo_client.py 192.168.0.26 --eject-dst
python3 echo_client.py 192.168.0.26 --eject-all
python3 echo_client.py 192.168.0.26 \
    --transfer 384PP_DMSO2 384_CellVis 'A1:B07:2.5,A2:F07:5,O10:B08:10'
```

`--help` lists every flag.

---

## Using the Python library

The client is a single class, `EchoClient`, in `echo_client.py`.

### Minimal example

```python
from echo_client import EchoClient

client = EchoClient("192.168.0.26")

# Check we can reach it
info = client.get_instrument_info()
print(f"{info.model} S/N {info.serial_number} firmware {info.software_version}")

# Optional: subscribe to events pushed on port 8010
client.subscribe_events(callback=lambda eid, payload, src, ts: print(payload))

# Run the initialization sequence as observed in the vendor client
client.initialize()

# Home all motion axes (~22 seconds, blocking)
ok, status = client.home_axes()

# Cleanup
client.disconnect()
```

### Plate load → survey → transfer → eject

```python
from echo_client import EchoClient

c = EchoClient("192.168.0.26")
c.subscribe_events()

# Full init (required before physical actions)
c.initialize()
c.home_axes()

# Load a source plate (~25s — user puts plate in during the Present pause;
# here we just run the sequence back-to-back)
c.load_src_plate("384PP_DMSO2")

# Load a destination plate
c.load_dst_plate("384_CellVis")

# Survey the source plate (~23s for a 384-well plate)
survey = c.survey_src_plate("384PP_DMSO2")
for w in survey.wells[:5]:
    print(f"{w.name}: {w.volume_nL:.1f} nL {w.fluid}")

# Execute a transfer (volumes in nanoliters)
result = c.transfer_wells(
    src_plate_type="384PP_DMSO2",
    dst_plate_type="384_CellVis",
    transfers=[
        ("A1",  "B07", 2.5),
        ("A2",  "F07", 5.0),
        ("O10", "B08", 10.0),
    ],
    do_survey=False,   # already surveyed
    close_door=True,
)
for t in result.transfers:
    print(f"OK   {t.source_name} -> {t.dest_name}: {t.actual_volume_nL:.1f} nL")
for s in result.skipped:
    print(f"SKIP {s.source_name} -> {s.dest_name}: {s.reason}")

# Eject both plates and close the door
c.eject_all()
c.disconnect()
```

### Full method reference

All methods return a value; high-level wrappers print progress to stdout.

#### State / information

| Method | Returns | Notes |
|---|---|---|
| `get_instrument_info()` | `InstrumentInfo` | serial, model, firmware, boot time |
| `get_echo_configuration()` | `str` | full raw hardware config XML |
| `get_dio_ex2()` | `DIOEx2` | sensor flags, temperatures |
| `get_dio()` | `ET.Element` | legacy DIO, fewer fields |
| `get_pwr_cal()` | `ET.Element` | power calibration |
| `get_instrument_lock_state()` | `(is_locked, status)` | |
| `get_current_src_plate_type()` | `str` | `"None"` if empty |
| `get_current_dst_plate_type()` | `str` | |
| `is_src_plate_present()` / `is_dst_plate_present()` | `bool` | |
| `get_all_src_plate_names()` | `list[str]` | |
| `get_all_dest_plate_names()` | `list[str]` | |
| `get_plate_info_ex(name)` | `PlateInfo` | dimensions, fluid, format |
| `get_fluid_info(name)` | `FluidInfo` | e.g. `"DMSO"` → FC range |
| `is_storage_mode()` | `bool` | |

#### Door / stage / actuators

| Method | What it does |
|---|---|
| `open_door()` / `close_door()` | physical door |
| `home_axes()` | home all motion axes (~22 s) |
| `present_src_plate_gripper()` / `retract_src_plate_gripper(plate_type, ...)` | source gripper in/out |
| `present_dst_plate_gripper()` / `retract_dst_plate_gripper(plate_type, ...)` | destination gripper in/out |
| `set_pump_dir(normal: bool)` | pump direction |
| `enable_bubbler_pump(on)` | coupling fluid pump on/off |
| `actuate_bubbler_nozzle(up)` | coupling fluid up/down (~1.6 s) |
| `raise_coupling_fluid()` / `lower_coupling_fluid()` | aliases |
| `actuate_ionizer(on)` | anti-static ion bar on/off |
| `enable_vacuum_nozzle(on)` | vacuum pump on/off (no plate needed) |
| `actuate_vacuum_nozzle(engage)` | vacuum mechanism (**plate required**) |
| `dry_plate(dry_type="TWO_PASS")` | run dry cycle (~10 s) |

#### High-level wrappers (the ones you'll usually call)

| Method | Sequence |
|---|---|
| `initialize()` | full init sequence as captured from VWORKS-SBA |
| `load_src_plate(type)` | OpenDoor → Present → Retract → verify present |
| `load_dst_plate(type)` | same for destination |
| `eject_src_plate()` | Present → Retract(None) |
| `eject_dst_plate()` | Present → Retract(None) |
| `eject_all()` | src eject → dst eject → CloseDoor (correct interlock order) |
| `survey_src_plate(type)` | SetPlateMap(full) → PlateSurvey(full) → GetFluidInfo |
| `transfer_wells(src, dst, [(w, w, nL), ...])` | SetPlateMap(sparse) → PlateSurvey(partial) → DoWellTransfer |

#### Dataclasses returned

- `InstrumentInfo` — serial, model, firmware, boot time, status
- `PlateInfo` — name, rows, cols, fluid, format, well capacity
- `FluidInfo` — name, FC range, units
- `DIOEx2` — boolean flags + temps, `.raw` dict with all raw fields
- `WellSurvey` — per-well volume, fluid, position, thickness
- `PlateSurveyResult` — `.wells: list[WellSurvey]` + plate metadata
- `TransferWell` — completed transfer: source, dest, volume_nL, actual_nL, composition, timestamp
- `SkippedWell` — skipped transfer with `reason` code
- `TransferResult` — `.transfers: list[TransferWell]`, `.skipped: list[SkippedWell]`

---

## Using the REST API

### Start the server

```bash
python3 echo_api.py
# Listening on http://127.0.0.1:8765
```

Override host/port by editing the `uvicorn.run(...)` line at the bottom
of `echo_api.py`, or run via uvicorn directly:

```bash
python3 -m uvicorn echo_api:app --host 0.0.0.0 --port 8765
```

### Auto-generated docs

FastAPI publishes an OpenAPI schema at
**http://127.0.0.1:8765/openapi.json** and interactive docs at
**http://127.0.0.1:8765/docs** (Swagger) and **/redoc**.

### Endpoints

The server maintains **one `EchoClient` instance**. You must call
`/api/connect` before any other action.

#### Connection

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/connect` | `{ "ip": "192.168.0.26" }` | `{ connected, info }` |
| POST | `/api/disconnect` | — | `{ connected: false }` |
| GET  | `/api/status` | — | current state + DIO |

#### State reads

| Method | Path | Returns |
|---|---|---|
| GET | `/api/info` | instrument info |
| GET | `/api/dio` | DIO snapshot (all fields) |
| GET | `/api/plates/src` | source plate types + dimensions |
| GET | `/api/plates/dst` | destination plate types + dimensions |
| GET | `/api/plates/current` | `{src, dst}` currently loaded |

#### Event stream

| Method | Path | Notes |
|---|---|---|
| GET | `/api/events` | Server-Sent Events — fan-out of Echo log events |

Connect from JS with `new EventSource('/api/events')` or from `curl`:

```bash
curl -N http://127.0.0.1:8765/api/events
```

Event types in the stream:

```json
{"type": "hello"}                                  // on connect
{"type": "connected", "ip": "..."}                 // echo connected
{"type": "disconnected"}                           // echo disconnected
{"type": "echo_event", "id": 7000,
 "payload": "…", "source": "Logger", "timestamp": 0} // every log line
```

#### Controls

All take no body unless noted.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/door/open` | — | |
| POST | `/api/door/close` | — | |
| POST | `/api/home` | — | ~22 s blocking |
| POST | `/api/coupling-fluid/pump` | `{value: bool}` | pump on/off |
| POST | `/api/coupling-fluid/nozzle` | `{value: bool}` | True = up |
| POST | `/api/coupling-fluid/pump-dir` | `{value: bool}` | True = normal |
| POST | `/api/ionizer` | `{value: bool}` | |
| POST | `/api/vacuum/pump` | `{value: bool}` | |
| POST | `/api/dry` | `{dry_type: "TWO_PASS"}` | |

#### Plate handling (two-step, used by the UI)

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/plates/src/extend` | — | extend gripper, **leaves it out** |
| POST | `/api/plates/dst/extend` | — | same for destination |
| POST | `/api/plates/src/retract` | `{plate_type, barcode_location?}` | `"None"` = empty |
| POST | `/api/plates/dst/retract` | `{plate_type, barcode_location?}` | |
| POST | `/api/plates/eject-all` | — | atomic eject src → dst → CloseDoor |

When you pass `plate_type: "None"`, the server auto-sets
`barcode_location: "None"` so the scanner doesn't try to read.

#### Long-running workflows

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/survey` | `{plate_type}` | ~23 s for 384-well |
| POST | `/api/transfer` | see below | dispense |
| POST | `/api/picklist/parse` | multipart CSV upload | parses **only**, does not execute |

Transfer body:

```json
{
  "src_plate_type": "384PP_DMSO2",
  "dst_plate_type": "384_CellVis",
  "transfers": [
    {"src": "A1", "dst": "B07", "volume_nL": 2.5},
    {"src": "A2", "dst": "F07", "volume_nL": 5.0}
  ],
  "do_survey": true,
  "close_door": true,
  "protocol_name": "my-transfer"
}
```

### curl examples

```bash
# Connect
curl -X POST http://127.0.0.1:8765/api/connect \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.0.26"}'

# Open the door
curl -X POST http://127.0.0.1:8765/api/door/open

# Extend (eject) the source gripper; user takes plate off
curl -X POST http://127.0.0.1:8765/api/plates/src/extend

# ...physical work...

# Retract declaring plate type
curl -X POST http://127.0.0.1:8765/api/plates/src/retract \
  -H 'Content-Type: application/json' \
  -d '{"plate_type":"384PP_DMSO2"}'

# Or retract empty (after ejecting)
curl -X POST http://127.0.0.1:8765/api/plates/src/retract \
  -H 'Content-Type: application/json' \
  -d '{"plate_type":"None"}'

# Survey
curl -X POST http://127.0.0.1:8765/api/survey \
  -H 'Content-Type: application/json' \
  -d '{"plate_type":"384PP_DMSO2"}'

# Parse a picklist CSV
curl -X POST http://127.0.0.1:8765/api/picklist/parse \
  -F file=@picklist.csv

# Execute a transfer
curl -X POST http://127.0.0.1:8765/api/transfer \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "src_plate_type": "384PP_DMSO2",
  "dst_plate_type": "384_CellVis",
  "transfers": [
    {"src":"A1","dst":"B07","volume_nL":2.5},
    {"src":"A2","dst":"F07","volume_nL":5}
  ],
  "do_survey": true,
  "close_door": true
}
JSON

# Watch the event log live
curl -N http://127.0.0.1:8765/api/events
```

---

## Using the web UI

Open **http://127.0.0.1:8765** after starting `echo_api.py`.

### 1. Connect

Enter the Echo's IP (default `192.168.0.26`) and click **Connect**. If
the instrument is reachable, the main app appears and a Server-Sent
Events connection opens to stream log messages into the bottom pane.

### 2. Sidebar (always visible)

- **Source Plate** — current loaded plate name, presence LED, and two
  buttons:
  - **Eject** — extends the gripper out, leaves it there so you can
    physically add / remove the plate.
  - **Load…** — opens a modal asking *"What is on the stage now?"*.
    Options include all source plate types from the instrument plus a
    `None (empty / no plate)` option. Retracts the gripper declaring
    that plate.
  - Button state is enforced locally: Eject is disabled while the
    gripper is out; Load is disabled while the gripper is in; and the
    destination **Eject is disabled while the source gripper is out**
    (Echo interlock — attempting it faults with *"Source plate gripper
    must be inside the instrument"*).
- **Destination Plate** — same, for the dest stage.
- **Door** — Open / Close.
- **System Status** — LEDs derived from `GetDIOEx2`: Coupling Fluid,
  Motor At Position, Source/Dest plate sensors, Coupling Level, Air
  Pressure, Fluid Door, Focus Cal.
- **Temperatures** — Coupling fluid + RF subsystem, auto-refreshing.

Sidebar polls `/api/status` every 5 seconds for live state.

### 3. Tabs

**Controls** — Home Stages, Coupling Fluid pump (on/off, nozzle up/down,
direction), Dry Plate, Ionizer, Vacuum pump.

**Labware** — Read-only tables of source and destination plate types
with well counts and fluid info (from `GetPlateInfoEx`).

**Survey** — Pick a plate type, click **Run Survey**. After ~23 s
displays a 16×24 or 32×48 colour-coded grid of per-well volumes plus
min/max/avg/SD/CV statistics. Hover any well for details.

**Plate Transfer (manual)** — Add rows of `src well / dst well / volume
(nL)`, pick plate types, optionally skip survey and door-close, click
**Run Transfer**. Result table shows successful transfers (requested vs
actual volume, fluid composition %) and any skipped wells with reason
codes.

**Pick-List Transfer** — Upload a CSV, click **Parse**. The parsed list
appears in a table; if the CSV contains plate-type columns they
pre-select the dropdowns. Click **Run Picklist** to execute. See the
next section for CSV format.

### 4. Log pane

Black terminal-style pane at the bottom streams every Echo log event in
real time. **Clear** button empties it. Log is capped at 2000 lines.

### 5. Status bar

Shows Connected Instrument, Serial Number, Firmware Version. The right
side shows a live status message (*Ready*, *Homing…*, *Running survey
(~23 s)…*, etc.).

---

## Pick-list CSV format

The picklist parser auto-detects column names. All three of these work:

```csv
Source Well,Destination Well,Transfer Volume
A1,B07,2.5
A2,F07,5
B1,C03,10
```

```csv
src,dst,vol
A1,B07,2.5
```

```csv
SourceWell;DestWell;Volume
A1;B07;2.5
```

Column name detection is whitespace/punctuation-insensitive. Supported
aliases:

- **Source well:** `Source Well`, `SourceWell`, `src`, `source`,
  `SourcePlateWell`, `SourceWellId`
- **Destination well:** `Destination Well`, `DestWell`, `dst`,
  `destination`, `DestinationPlateWell`, `DestinationWellId`
- **Volume (nL):** `Transfer Volume`, `Volume`, `Vol`, `Vol_nL`,
  `VolumeNL`, `TransferVolumeNL`
- *(optional)* **Source plate type:** `Source Plate Type`, `Source Plate`,
  `SrcPlateType`
- *(optional)* **Destination plate type:** `Destination Plate Type`,
  `Destination Plate`, `DstPlateType`

The delimiter (`,`, `\t`, `;`) is auto-detected. UTF-8 BOMs are
stripped. **Volumes are in nanoliters** — the Echo's native unit.

If plate-type columns are present, the first non-empty values are used
to pre-select the dropdowns in the UI.

---

## Protocol reference

### Wire format

- **RPC**: SOAP 1.1 over HTTP 1.1 on TCP **port 8000**. One TCP
  connection per call (open, POST, read response, close).
- **Events**: persistent connections on TCP **port 8010**. Client sends
  a POST with body `add<LockID>` to subscribe; the device then pushes
  `handleEvent` SOAP messages whenever it logs anything.
- **Both sides gzip their SOAP bodies.** The HTTP headers don't always
  advertise it with `Content-Encoding: gzip` — the body just starts
  with the gzip magic (`1f 8b`). Our parser tries gzip first and falls
  back to plain text.
- HTTP headers are unusual: line endings are `\n` (not `\r\n`), and the
  Host header includes the LockID (e.g.
  `192.168.0.26:13564:18324:1776307172:14938`).

### LockID format

`<IP>:<port>:<port>:<epoch>:<pid>` — the vendor's client uses this to
identify itself across connections. Our `EchoClient` generates its own
on construction.

### The embedded-XML pattern

Several methods use **XML-escaped XML inside SOAP** for complex
payloads:

- `SetPlateMap` — param `xmlPlateMap` is an escaped `<PlateMap>…` doc
- `GetEchoConfiguration` — same idea
- `PlateSurvey` response — the per-well data is an escaped
  `<platesurvey>` doc inside the SOAP `<PlateSurvey>` element
- `DoWellTransfer` — param `ProtocolName` (misleading name!) is the
  full protocol XML; the response's `<Value>` is an escaped
  `<transfer>` doc with printmap / skippedwells / platemap

When parsing responses, always check whether a `.text` attribute starts
with `<` — you may need a second parse pass.

### DIO flags (partial)

Observed in `GetDIOEx2` responses. Not all meanings are confirmed.

| Flag | Likely meaning |
|---|---|
| `MAP` | Motor At Position |
| `MVP` | Motor Velocity Positive |
| `CFE` | Coupling Fluid Enabled |
| `SPP` | Source Plate Present (1 = plate, 0 = no plate) |
| `DPP` | Destination Plate Position |
| `IBUP` / `IBDN` | Ion Bar Up / Down |
| `DND` / `DNU` | Dryer Down / Up |
| `BNO` / `BNP` | Bubbler Nozzle Out / ? |
| `DFC` / `DFO` | Door Fluid Closed / Open |
| `LSO` / `LSI` | Loader Stage Out / In (*unconfirmed*) |
| `CDAP` | Coupling Fluid Level (probably) |
| `FCD` | Focus Calibration Done |
| `CouplingFluidTemp`, `RFSubsystemTemp` | temperatures °C |

When a new DIO flag becomes important, test on a real instrument and
confirm by toggling the relevant state and re-reading.

### Error handling

Two error styles coexist and you must handle both:

- **Method-level failure**: SOAP `<...Response>` with
  `<SUCCEEDED>False</SUCCEEDED>` and a `<Status>…</Status>` message.
- **Protocol / state fault**: SOAP `<Fault>` with `<faultstring>…
  </faultstring>`. Example: calling `ActuateVacuumNozzle(True)` with no
  plate loaded returns `MM1302001: Unknown Source Plate, inset`.

`EchoClient._rpc_ok()` handles both and returns
`(ok: bool, status: str, root: ET.Element)`.

Error codes we've observed:

- `MM0202007: Problem calc. well fluid volume fc: 0 ft: 0` — transfer
  can't measure a well (usually outside the surveyed region)
- `MM1302001: Unknown Source Plate, inset` — vacuum nozzle called
  without a plate
- *"Unable to proceed. Source plate gripper must be inside the
  instrument."* — extending dest gripper with src gripper out
- *"Barcode Reading Error: Failed to read bar code"* — scanner saw no
  barcode (usually means the plate is missing)

---

## Protocol quirks (read this)

These will bite you if you don't know them. They're all documented
inline in the code, but collected here for reference.

### 1. Plate presence is a multi-signal condition, not a dedicated error

`RetractSrcPlateGripper` returns `SUCCEEDED=True` **even if no plate was
on the stage**. Detecting absence requires combining:

- `BarCode` in the retract response contains
  `"Barcode Reading Error: Failed to read bar code"`
- `GetCurrentSrcPlateType()` returns `"None"`
- `GetDIOEx2` → `SPP == 0`

Use `client.is_src_plate_present()` / `is_dst_plate_present()` —
they wrap the `GetCurrent*PlateType` check.

### 2. "Eject" is Present + Retract-empty

The Echo has no dedicated eject call. To unload a plate:

```python
client.present_src_plate_gripper()       # extend gripper out
# user removes plate
client.retract_src_plate_gripper(
    plate_type="None",
    barcode_location="None",   # tells scanner not to try
    barcode="",
)
```

Pass the literal string `"None"` (not empty string) for both
`plate_type` and `barcode_location`.

### 3. Gripper state interlock

Destination gripper **cannot be extended while source gripper is out**.
Attempting it returns a SOAP Fault. To clear both plates, always eject
source first, then dest.

### 4. Two separate vacuum calls

- `EnableVacuumNozzle(True)` — turns the pump on/off. Does not require
  a plate.
- `ActuateVacuumNozzle(True)` — moves the physical mechanism. **Requires
  a plate.** Faults with `MM1302001` otherwise.

These are not interchangeable.

### 5. Bubbler nozzle ≡ coupling fluid up/down

The UI concept "raise/lower coupling fluid" is implemented by
`ActuateBubblerNozzle(True/False)` — not a separate call.

### 6. `SetPlateMap` has two uses

- Before `PlateSurvey`: **full** plate enumeration of every well.
- Before `DoWellTransfer`: **sparse** map of only the source wells
  actually being used.

`EchoClient.set_plate_map()` supports both:

```python
# full
client.set_plate_map("384PP_DMSO2", rows=16, cols=24)

# sparse
client.set_plate_map("384PP_DMSO2", wells=[
    ("A1", 0, 0), ("A2", 0, 1), ("O10", 14, 9),
])
```

### 7. `DoWellTransfer` parameter naming is misleading

The param is called `ProtocolName` but it holds the **entire protocol
XML**, not a name / reference. See `build_protocol_xml()`.

### 8. Volumes are nanoliters

Throughout the protocol: `vt`, `avt`, `v`, `volume_nL` — all nL. Echo
dispenses in 2.5 nL increments. Do not pass microliters.

### 9. `PlateSurvey` is slow *and* has large responses

~23 seconds for a 384-well plate (1 s per row). Response is ~290 KB of
XML. Never call it with a short timeout. Default is 120 s.

### 10. Pre-transfer workflow order matters

The capture shows this exact order:

1. `GetCurrent{Src,Dst}PlateType`
2. `RetrieveParameter("Client_IgnoreDestPlateSensor")`
3. `RetrieveParameter("Client_IgnoreSourcePlateSensor")`
4. `SetPlateMap` (sparse, source wells)
5. `GetPlateInfoEx` (source)
6. **`CloseDoor`**
7. `PlateSurvey` (partial — only `num_rows = 1 + max_source_row`)
8. `GetDIOEx2` + `GetDIO`
9. `DoWellTransfer`

`client.transfer_wells()` does all of this. If you skip the partial
survey, expect `MM0202007` skipped wells (no fluid volume data).

### 11. `DryPlate` `DioAction taking longer than expected` WARN looks benign

Seen in one capture. The call returned `SUCCEEDED=True` and the cycle
completed. Treat `SUCCEEDED` as the source of truth; revisit if a
future capture shows a client actually reacting to this WARN.

---

## Reverse-engineering new features

To add support for a feature not yet covered (Focus tab, Service tab,
Fluid Replacement, Labware Add/Edit, Pick-List features we haven't
seen, Soft/Hard Reset, etc.):

### Capture traffic

1. On any PC on the same network as the Echo, run Wireshark.
2. Start a capture on the interface that sees the instrument's IP.
   (Or mirror the port / hub-out.)
3. Filter: `tcp.port == 8000 or tcp.port == 8010`
4. In the vendor Echo Client Utility, perform the operation in the
   narrowest possible window — ideally nothing else happening.
5. Stop the capture and save as `.pcapng`.

### Analyze the capture

The pattern we've used:

```python
import subprocess, re, gzip

result = subprocess.run(
    ['tcpdump', '-r', 'yourcapture.pcapng', '-nn', '-X',
     'tcp port 8000 and src host <YOUR_PC_IP> and (tcp[tcpflags] & tcp-push != 0)'],
    capture_output=True, text=True
)
# Parse packet boundaries from the tcpdump output, reassemble hex,
# find the "POST" start, read the HTTP headers, gzip-decompress the
# body, and print the SOAP.
```

Key things to extract per packet:

- Method name (first child of `<SOAP-ENV:Body>`)
- Parameters (direct children)
- Response body — `<SUCCEEDED>`, `<Status>`, and the method-specific
  response element

Also check port 8010 events concurrent with the call — often reveals
the underlying `FluidTransferServer.cpp(line)` log lines which tell you
what the call did internally.

### Add a method to `echo_client.py`

Minimal pattern:

```python
def my_new_method(self, arg: str, timeout: float = 30.0) -> tuple[bool, str]:
    body = _call_with_body(
        "MyNewMethod",
        _element("SomeParam", arg, "string"),
    )
    ok, status, root = self._rpc_ok(body, timeout=timeout)
    return ok, status
```

For methods that return structured data, walk the response with
`root.iter()` or XPath and populate a dataclass.

For methods that accept or return embedded XML, use the existing
helpers (`generate_plate_map_xml`, `build_protocol_xml`) as patterns.

### Add to the REST API

Add an endpoint in `echo_api.py`:

```python
@app.post("/api/my-feature")
def api_my_feature(b: MyBody):
    c = _require_echo()
    ok, status = c.my_new_method(b.arg)
    return {"ok": ok, "status": status}
```

### Add to the UI

Add a button / form / tab in `static/index.html` and wire it in
`static/app.js` with a `fetch` / `apiPost` call.

---

## Known limitations and roadmap

### Not yet captured / implemented

- **Focus tab** — not captured
- **Service tab** — not captured
- **Fluid Replacement tab** — not captured
- **Labware Add/Edit/Delete** — we've only captured reads
- **Soft Reset / Hard Reset / Power** — intentionally omitted; destructive
- **Abort** for long-running ops — vendor UI has an Abort button for
  survey and picklist; the abort call hasn't been captured
- **Protocol management** — `GetAllProtocolNames` and `GetProtocol` are
  wired in the client but not exposed in the UI
- **Barcode handling beyond default `Right-Side`** — the plate info
  actually carries a `BarcodeLoc` which could be plumbed through

### Architectural limitations

- **Single client at a time.** The server holds one `EchoClient`
  instance. Multiple browser tabs work fine for viewing, but concurrent
  state-changing calls will serialize through that one Python object.
- **Long-running calls block HTTP.** `home_axes()`, `plate_survey()`,
  full `transfer_wells()` can each take 20+ seconds; the HTTP request
  stays open that whole time. The event stream still works. A job-based
  dispatch (spawn → poll `/jobs/{id}`) would be the right upgrade when
  multiple simultaneous long ops become needed.
- **No auth / authz.** REST API is fully open on whatever interface you
  bind. Don't expose it to an untrusted network.
- **Gripper position is tracked client-side.** Until the DIO flag for
  gripper extended is confirmed, the web UI's button disable uses local
  state. If something else drives the Echo between UI actions the state
  can drift (the SOAP Fault will surface the error).

### Compatibility

Tested on **Echo 655 firmware 3.2.2** only. The SOAP API looks like a
shared "EchoServer" across models (the `GetEchoConfiguration` response
enumerates the model), so it's likely that Echo 525/550/575 and 650T
speak the same protocol — but we haven't verified.

---

## Troubleshooting

**`Could not reach instrument`**
- Check ping first: `ping 192.168.0.26`
- Check the firewall / port: `nc -z 192.168.0.26 8000`
- Make sure Echo Client Utility / VWORKS is not actively running and
  holding the instrument.

**Calls succeed but the instrument ignores them**
- Check the event log pane for WARN messages.
- Verify `GetCurrent*PlateType` actually shows the plate you think is
  loaded.

**SOAP Fault: `Unable to proceed. Source plate gripper must be inside the instrument.`**
- Source gripper is extended. Retract it first.

**SOAP Fault: `MM1302001: Unknown Source Plate, inset`**
- You called `ActuateVacuumNozzle` without a plate. Load a plate first,
  or use `EnableVacuumNozzle` (pump control) instead.

**Skipped wells with `MM0202007: Problem calc. well fluid volume`**
- Either no survey was run, or the survey didn't cover that row. For
  `transfer_wells(..., do_survey=True)` (default) we auto-compute rows
  to cover all source wells — but if you called `plate_survey` manually
  with a small `num_rows`, extend it.

**CSV parse says "missing columns"**
- Check the actual header names. The error lists the headers it saw.
  Add a supported alias to `_SRC_WELL_COLS` / `_DST_WELL_COLS` /
  `_VOL_COLS` in `echo_api.py` if needed.

**"Connect" button hangs**
- The Echo is probably reachable but busy. Check the event log from
  another machine or the vendor Echo Client Utility.

**Web UI shows blank / JS errors in console**
- Hard refresh (Cmd-Shift-R / Ctrl-Shift-R) — there's no cache-busting
  on the static files yet.

---

## Project layout

```
echoInit/
├── README.md                    # this file
├── requirements.txt             # FastAPI + uvicorn + python-multipart
├── echo_client.py               # Python library (stdlib only)
├── echo_api.py                  # FastAPI REST + static file server
└── static/
    ├── index.html               # single-page UI layout
    ├── app.js                   # tab logic, API calls, SSE, grid rendering
    └── style.css                # vendor-UI-inspired styling
```

Plus `.pcapng` captures used during reverse-engineering — these are not
required at runtime but are kept in the working directory for future
reference.

---

## License / contributing

This is internal lab tooling. No license is declared. If you want to
share it beyond your lab, check with Beckman Coulter / Labcyte first —
protocol reverse-engineering of lab instrumentation may sit awkwardly
with vendor EULAs.

If you add support for a new feature, please:

1. Commit the `.pcapng` capture that motivated it (useful for future
   debugging).
2. Add the new method to the relevant reference table in this README.
3. Add a note to the *Protocol quirks* section if the feature has
   non-obvious behaviors (which, based on prior experience, it will).
