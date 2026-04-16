"""
Echo 655 REST API + embedded web UI.

Run with:
    pip install -r requirements.txt
    python3 echo_api.py

Then open http://127.0.0.1:8765 in your browser.

Protocol:
  - The Echo instrument is the SOAP server (port 8000) + event push (port 8010).
  - This process wraps the EchoClient library and exposes a REST API + static
    HTML/JS UI. It maintains ONE EchoClient instance and fans out the Echo's
    event stream to any number of browser clients via Server-Sent Events.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import queue as tqueue
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from echo_client import (
    EchoClient,
    PlateInfo,
    TransferResult,
    build_protocol_xml,
    well_name_to_rc,
)

# ---------------------------------------------------------------------------
# Global Echo client state
# ---------------------------------------------------------------------------

_echo: Optional[EchoClient] = None
_echo_lock = threading.Lock()

# Event pub/sub — each SSE subscriber has its own thread-safe queue.
_subscribers: list[tqueue.Queue] = []
_subs_lock = threading.Lock()


def _broadcast(item: dict) -> None:
    """Push an event to all SSE subscribers (called from echo event thread)."""
    with _subs_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(item)
            except tqueue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _on_echo_event(event_id: int, payload: str, source: str, timestamp: int) -> None:
    _broadcast(
        {
            "type": "echo_event",
            "id": event_id,
            "payload": payload,
            "source": source,
            "timestamp": timestamp,
        }
    )


def _require_echo() -> EchoClient:
    if _echo is None:
        raise HTTPException(status_code=503, detail="Not connected to instrument")
    return _echo


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Shutdown: disconnect Echo if connected
    global _echo
    if _echo is not None:
        try:
            _echo.unsubscribe_events()
        except Exception:
            pass
        _echo = None


app = FastAPI(title="Echo 655 API", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------

class ConnectBody(BaseModel):
    ip: str
    rpc_port: int = 8000
    event_port: int = 8010


class BoolValue(BaseModel):
    value: bool


class PlateTypeBody(BaseModel):
    plate_type: str
    barcode_location: str = "Right-Side"


class TransferItem(BaseModel):
    src: str
    dst: str
    volume_nL: float


class TransferBody(BaseModel):
    src_plate_type: str
    dst_plate_type: str
    transfers: list[TransferItem]
    do_survey: bool = True
    close_door: bool = True
    protocol_name: str = "ui-transfer"


class DryBody(BaseModel):
    dry_type: str = "TWO_PASS"


class SurveyBody(BaseModel):
    plate_type: str


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@app.post("/api/connect")
def api_connect(body: ConnectBody):
    global _echo
    with _echo_lock:
        if _echo is not None:
            raise HTTPException(400, "Already connected — disconnect first")
        client = EchoClient(body.ip, rpc_port=body.rpc_port, event_port=body.event_port)
        try:
            # Verify reachable with a cheap call
            info = client.get_instrument_info()
            client.subscribe_events(callback=_on_echo_event)
        except Exception as e:
            raise HTTPException(502, f"Could not reach instrument: {e}")
        _echo = client
    _broadcast({"type": "connected", "ip": body.ip})
    return {
        "connected": True,
        "ip": body.ip,
        "info": {
            "serial_number": info.serial_number,
            "model": info.model,
            "software_version": info.software_version,
            "instrument_status": info.instrument_status,
            "boot_time": info.boot_time,
        },
    }


@app.post("/api/disconnect")
def api_disconnect():
    global _echo
    with _echo_lock:
        if _echo is None:
            return {"connected": False}
        try:
            _echo.unsubscribe_events()
        except Exception:
            pass
        _echo = None
    _broadcast({"type": "disconnected"})
    return {"connected": False}


@app.get("/api/status")
def api_status():
    if _echo is None:
        return {"connected": False}
    try:
        info = _echo.get_instrument_info()
        dio = _echo.get_dio_ex2()
    except Exception as e:
        return {"connected": False, "error": str(e)}
    return {
        "connected": True,
        "ip": _echo.ip,
        "info": {
            "serial_number": info.serial_number,
            "model": info.model,
            "software_version": info.software_version,
            "instrument_status": info.instrument_status,
            "boot_time": info.boot_time,
        },
        "dio": {
            "MAP": dio.MAP, "MVP": dio.MVP, "CFE": dio.CFE,
            "SPP": dio.SPP, "DPP": dio.DPP,
            "coupling_fluid_temp": dio.coupling_fluid_temp,
            "rf_subsystem_temp": dio.rf_subsystem_temp,
            "raw": dio.raw,
        },
    }


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def api_events():
    q: tqueue.Queue = tqueue.Queue(maxsize=2000)
    with _subs_lock:
        _subscribers.append(q)

    loop = asyncio.get_event_loop()

    async def gen():
        # Initial hello so the client knows it's connected
        yield f"data: {json.dumps({'type': 'hello'})}\n\n"
        try:
            while True:
                try:
                    item = await loop.run_in_executor(None, q.get, True, 15.0)
                    yield f"data: {json.dumps(item)}\n\n"
                except tqueue.Empty:
                    yield ": ping\n\n"
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# State reads
# ---------------------------------------------------------------------------

def _plate_to_dict(p: PlateInfo) -> dict:
    return {
        "name": p.name,
        "rows": p.rows,
        "cols": p.cols,
        "well_capacity": p.well_capacity,
        "fluid": p.fluid,
        "plate_format": p.plate_format,
        "usage": p.usage,
    }


@app.get("/api/info")
def api_info():
    c = _require_echo()
    info = c.get_instrument_info()
    return info.__dict__


@app.get("/api/dio")
def api_dio():
    c = _require_echo()
    dio = c.get_dio_ex2()
    return {
        "MAP": dio.MAP, "MVP": dio.MVP, "CFE": dio.CFE,
        "SPP": dio.SPP, "DPP": dio.DPP,
        "coupling_fluid_temp": dio.coupling_fluid_temp,
        "rf_subsystem_temp": dio.rf_subsystem_temp,
        "raw": dio.raw,
    }


@app.get("/api/plates/src")
def api_list_src_plates():
    c = _require_echo()
    names = c.get_all_src_plate_names()
    plates = [_plate_to_dict(c.get_plate_info_ex(n)) for n in names]
    return {"plates": plates}


@app.get("/api/plates/dst")
def api_list_dst_plates():
    c = _require_echo()
    names = c.get_all_dest_plate_names()
    plates = [_plate_to_dict(c.get_plate_info_ex(n)) for n in names]
    return {"plates": plates}


@app.get("/api/plates/current")
def api_current_plates():
    c = _require_echo()
    return {
        "src": c.get_current_src_plate_type(),
        "dst": c.get_current_dst_plate_type(),
    }


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

@app.post("/api/door/open")
def api_door_open():
    c = _require_echo()
    ok, status = c.open_door()
    return {"ok": ok, "status": status}


@app.post("/api/door/close")
def api_door_close():
    c = _require_echo()
    ok, status = c.close_door()
    return {"ok": ok, "status": status}


@app.post("/api/home")
def api_home():
    c = _require_echo()
    ok, status = c.home_axes()
    return {"ok": ok, "status": status}


@app.post("/api/coupling-fluid/pump")
def api_coupling_pump(b: BoolValue):
    c = _require_echo()
    ok, status = c.enable_bubbler_pump(b.value)
    return {"ok": ok, "status": status}


@app.post("/api/coupling-fluid/nozzle")
def api_coupling_nozzle(b: BoolValue):
    """True = nozzle up (coupling fluid engaged), False = down (disengaged)."""
    c = _require_echo()
    ok, status = c.actuate_bubbler_nozzle(b.value)
    return {"ok": ok, "status": status}


@app.post("/api/coupling-fluid/pump-dir")
def api_pump_dir(b: BoolValue):
    """True = normal direction, False = reverse."""
    c = _require_echo()
    ok, status = c.set_pump_dir(b.value)
    return {"ok": ok, "status": status}


@app.post("/api/ionizer")
def api_ionizer(b: BoolValue):
    c = _require_echo()
    ok, status = c.actuate_ionizer(b.value)
    return {"ok": ok, "status": status}


@app.post("/api/vacuum/pump")
def api_vacuum_pump(b: BoolValue):
    c = _require_echo()
    ok, status = c.enable_vacuum_nozzle(b.value)
    return {"ok": ok, "status": status}


@app.post("/api/dry")
def api_dry(b: DryBody):
    c = _require_echo()
    ok, status = c.dry_plate(b.dry_type)
    return {"ok": ok, "status": status}


# ---------------------------------------------------------------------------
# Plate load / eject
# ---------------------------------------------------------------------------

# Two-step plate handling (used by the UI). The Echo protocol is:
#   1. Present*PlateGripper  - extends the gripper out
#   2. Retract*PlateGripper(PlateType) - pulls gripper in, declaring what's
#      now on the stage ("None" = empty/ejected, or a known plate type).
# In between, the user physically swaps / removes / places the plate.

@app.post("/api/plates/src/extend")
def api_extend_src():
    """Extend (present) the source plate gripper. Leaves it extended."""
    c = _require_echo()
    ok, status = c.present_src_plate_gripper()
    return {"ok": ok, "status": status}


@app.post("/api/plates/dst/extend")
def api_extend_dst():
    """Extend (present) the destination plate gripper. Leaves it extended."""
    c = _require_echo()
    ok, status = c.present_dst_plate_gripper()
    return {"ok": ok, "status": status}


class RetractBody(BaseModel):
    plate_type: str                   # "None" for empty, or a known type
    barcode_location: Optional[str] = None  # None → auto based on plate_type


def _auto_barcode_loc(plate_type: str, override: Optional[str]) -> str:
    if override is not None:
        return override
    # Eject (no plate): tell the scanner not to try.
    if not plate_type or plate_type.strip().lower() == "none":
        return "None"
    return "Right-Side"


@app.post("/api/plates/src/retract")
def api_retract_src(b: RetractBody):
    """
    Retract the source gripper, declaring what's now on the stage.
    plate_type='None' = empty (after ejecting), or a known plate type name.
    """
    c = _require_echo()
    loc = _auto_barcode_loc(b.plate_type, b.barcode_location)
    ok, status, barcode = c.retract_src_plate_gripper(
        plate_type=b.plate_type, barcode_location=loc, barcode=""
    )
    present = c.is_src_plate_present()
    return {"ok": ok, "status": status, "barcode": barcode,
            "plate_present": present}


@app.post("/api/plates/dst/retract")
def api_retract_dst(b: RetractBody):
    """
    Retract the destination gripper, declaring what's now on the stage.
    """
    c = _require_echo()
    loc = _auto_barcode_loc(b.plate_type, b.barcode_location)
    ok, status, barcode = c.retract_dst_plate_gripper(
        plate_type=b.plate_type, barcode_location=loc, barcode=""
    )
    present = c.is_dst_plate_present()
    return {"ok": ok, "status": status, "barcode": barcode,
            "plate_present": present}


# Atomic helpers kept for CLI / scripted use — the UI does NOT use these
# because they don't give the user time to physically handle the plate.

@app.post("/api/plates/eject-all")
def api_eject_all():
    """Eject source then destination, close door. Atomic — no user pause."""
    c = _require_echo()
    ok, status = c.eject_all()
    return {"ok": ok, "status": status}


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------

@app.post("/api/survey")
def api_survey(b: SurveyBody):
    c = _require_echo()
    result = c.survey_src_plate(b.plate_type)
    wells = [
        {
            "name": w.name, "row": w.row, "col": w.col,
            "volume_nL": w.volume_nL,
            "fluid": w.fluid, "fluid_units": w.fluid_units,
        }
        for w in result.wells
    ]
    return {
        "plate_type": result.plate_type,
        "barcode": result.barcode,
        "date": result.date,
        "rows": result.rows,
        "cols": result.cols,
        "total_wells": result.total_wells,
        "wells": wells,
    }


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

def _transfer_to_dict(result: TransferResult) -> dict:
    return {
        "succeeded": result.succeeded,
        "status": result.status,
        "src_plate": result.src_plate,
        "dst_plate": result.dst_plate,
        "date": result.date,
        "transfers": [
            {
                "source": tw.source_name, "dest": tw.dest_name,
                "volume_nL": tw.volume_nL,
                "actual_volume_nL": tw.actual_volume_nL,
                "fluid": tw.fluid,
                "composition": tw.composition,
                "timestamp": tw.timestamp,
                "reason": tw.reason,
            } for tw in result.transfers
        ],
        "skipped": [
            {
                "source": sw.source_name, "dest": sw.dest_name,
                "volume_nL": sw.requested_volume_nL,
                "reason": sw.reason,
            } for sw in result.skipped
        ],
    }


@app.post("/api/transfer")
def api_transfer(b: TransferBody):
    c = _require_echo()
    transfers = [(t.src, t.dst, t.volume_nL) for t in b.transfers]
    # Validate wells parseable up front
    for src, dst, _ in transfers:
        try:
            well_name_to_rc(src)
            well_name_to_rc(dst)
        except ValueError as e:
            raise HTTPException(400, f"Bad well name: {e}")
    result = c.transfer_wells(
        b.src_plate_type, b.dst_plate_type, transfers,
        protocol_name=b.protocol_name,
        do_survey=b.do_survey,
        close_door=b.close_door,
    )
    return _transfer_to_dict(result)


# ---------------------------------------------------------------------------
# Pick-list upload
# ---------------------------------------------------------------------------

# Possible column name aliases (normalized: lowercase, alphanumeric only)
_SRC_WELL_COLS = {"sourcewell", "srcwell", "src", "source", "sourceplatewell", "sourcewellid"}
_DST_WELL_COLS = {"destinationwell", "destwell", "dst", "destination",
                  "destinationplatewell", "destinationwellid"}
_VOL_COLS = {"transfervolume", "volume", "vol", "vol_nl", "volumen",
             "volumenl", "transfervolumenl"}
_SRC_PLATE_COLS = {"sourceplatetype", "sourceplate", "srcplatetype", "srcplate"}
_DST_PLATE_COLS = {"destinationplatetype", "destinationplate", "destplatetype", "destplate"}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def parse_picklist_csv(content: str) -> dict:
    """
    Parse a picklist CSV into a list of transfers.
    Detects common column-name variants. Returns:
      {"src_plate_type": Optional[str], "dst_plate_type": Optional[str],
       "transfers": [{"src": "A1", "dst": "B07", "volume_nL": 2.5}, ...]}
    """
    # Read with a Sniffer for delimiter detection
    sample = content[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    # Map normalized -> original header names
    norm_map = {_norm(h): h for h in reader.fieldnames}

    def find(candidates):
        for c in candidates:
            if c in norm_map:
                return norm_map[c]
        return None

    src_col = find(_SRC_WELL_COLS)
    dst_col = find(_DST_WELL_COLS)
    vol_col = find(_VOL_COLS)
    src_plate_col = find(_SRC_PLATE_COLS)
    dst_plate_col = find(_DST_PLATE_COLS)

    missing = []
    if not src_col: missing.append("source well")
    if not dst_col: missing.append("destination well")
    if not vol_col: missing.append("transfer volume (nL)")
    if missing:
        raise ValueError(
            f"CSV missing columns: {', '.join(missing)}. "
            f"Headers seen: {reader.fieldnames}"
        )

    transfers = []
    src_plate = None
    dst_plate = None
    for i, row in enumerate(reader, start=2):
        src = (row.get(src_col) or "").strip()
        dst = (row.get(dst_col) or "").strip()
        vol = (row.get(vol_col) or "").strip()
        if not src and not dst and not vol:
            continue
        if not (src and dst and vol):
            raise ValueError(f"Row {i}: missing src/dst/volume")
        try:
            vol_f = float(vol)
        except ValueError:
            raise ValueError(f"Row {i}: volume {vol!r} is not a number")
        transfers.append({"src": src, "dst": dst, "volume_nL": vol_f})
        if src_plate_col and not src_plate:
            src_plate = (row.get(src_plate_col) or "").strip() or None
        if dst_plate_col and not dst_plate:
            dst_plate = (row.get(dst_plate_col) or "").strip() or None

    return {
        "src_plate_type": src_plate,
        "dst_plate_type": dst_plate,
        "transfers": transfers,
    }


@app.post("/api/picklist/parse")
async def api_parse_picklist(file: UploadFile = File(...)):
    """Upload a CSV, get back parsed transfers. Does NOT run them."""
    content_bytes = await file.read()
    try:
        content = content_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = content_bytes.decode("latin-1")
    try:
        parsed = parse_picklist_csv(content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    parsed["filename"] = file.filename
    return parsed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
