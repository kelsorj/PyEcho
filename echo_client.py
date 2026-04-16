"""
Echo 655 Acoustic Dispenser Client
Replicates the connection, initialization, and home stage sequence
as observed from Wireshark capture analysis.

Protocol: SOAP/XML over HTTP with gzip-compressed bodies
  - Port 8000: Request/response RPC (one TCP connection per call)
  - Port 8010: Event notification push channel (persistent)
"""

import socket
import gzip
import re
import time
import os
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# SOAP helpers
# ---------------------------------------------------------------------------

SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    '<SOAP-ENV:Envelope SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
    'xmlns:SOAPSDK1="http://www.w3.org/2001/XMLSchema" '
    'xmlns:SOAPSDK2="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:SOAPSDK3="http://schemas.xmlsoap.org/soap/encoding/" '
    'xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
    '<SOAP-ENV:Body SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '{body}'
    '</SOAP-ENV:Body></SOAP-ENV:Envelope>'
)

NS = {"SOAP-ENV": "http://schemas.xmlsoap.org/soap/envelope/"}

ENCODING_ATTR = 'SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"'


def _soap_body(inner_xml: str) -> str:
    return SOAP_ENVELOPE.format(body=inner_xml)


def _element(tag: str, value: str, xsd_type: str) -> str:
    return (
        f'<{tag} {ENCODING_ATTR} type="xsd:{xsd_type}">{value}</{tag}>'
    )


def _empty_call(method: str) -> str:
    return _soap_body(f'<{method} {ENCODING_ATTR}/>')


def _call_with_body(method: str, inner: str) -> str:
    return _soap_body(f'<{method} {ENCODING_ATTR}>{inner}</{method}>')


# ---------------------------------------------------------------------------
# Raw HTTP/SOAP transport (replicates the one-connection-per-call pattern)
# ---------------------------------------------------------------------------

def _build_request(host_header: str, body_xml: str, is_client: bool = True) -> bytes:
    """Build an HTTP POST with gzip-compressed SOAP body."""
    compressed = gzip.compress(body_xml.encode("utf-8"))
    headers = f"POST /Medman HTTP/1.1\n"
    headers += f"Host: {host_header}\n"
    if is_client:
        headers += "Client: 3.1.1\n"
    headers += "Protocol: 3.1\n"
    headers += 'Content-Type: text/xml; charset="utf-8"\n'
    headers += f"Content-Length: {len(compressed)}\n"
    headers += 'SOAPAction: "Some-URI"\r\n\r\n'
    return headers.encode("utf-8") + compressed


def _recv_all(sock: socket.socket, timeout: float = 30.0) -> bytes:
    """Receive a complete HTTP response from the Echo device."""
    sock.settimeout(timeout)
    data = b""
    # Read until we have headers + full body per Content-Length
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk

    if b"\r\n\r\n" not in data:
        return data

    header_end = data.index(b"\r\n\r\n") + 4
    headers_raw = data[:header_end].decode("utf-8", errors="replace")

    # Parse Content-Length
    content_length = 0
    for line in headers_raw.split("\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break

    # Read remaining body bytes
    while len(data) - header_end < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk

    return data


def _parse_response(raw: bytes) -> Optional[ET.Element]:
    """Parse an HTTP response, decompress gzip body, return XML root."""
    if b"\r\n\r\n" not in raw:
        return None
    header_end = raw.index(b"\r\n\r\n") + 4
    body = raw[header_end:]
    if not body:
        return None
    try:
        xml_bytes = gzip.decompress(body)
    except Exception:
        xml_bytes = body
    return ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Data classes for parsed responses
# ---------------------------------------------------------------------------

@dataclass
class DIOEx2:
    MAP: bool = False      # Motor at position
    MVP: bool = False      # Motor velocity positive
    CFE: bool = False      # Coupling fluid enabled
    DPP: int = 0           # Dest plate position
    SPP: int = 0           # Source plate position
    coupling_fluid_temp: float = 0.0
    rf_subsystem_temp: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class InstrumentInfo:
    serial_number: str = ""
    instrument_name: str = ""
    ip_address: str = ""
    software_version: str = ""
    boot_time: str = ""
    instrument_status: str = ""
    model: str = ""


@dataclass
class PlateInfo:
    name: str = ""
    rows: int = 0
    cols: int = 0
    well_capacity: float = 0.0
    fluid: str = ""
    plate_format: str = ""
    usage: str = ""


@dataclass
class FluidInfo:
    name: str = ""
    description: str = ""
    fc_min: float = 0.0
    fc_max: float = 0.0
    fc_units: str = ""


@dataclass
class WellSurvey:
    """Single-well result from a PlateSurvey."""
    name: str = ""                # e.g. "A1"
    row: int = 0
    col: int = 0
    volume_nL: float = 0.0        # measured volume
    current_volume_nL: float = 0.0
    status: str = ""
    fluid: str = ""
    fluid_units: str = ""
    x: float = 0.0                # physical position
    y: float = 0.0
    surface_height: float = 0.0
    thickness: float = 0.0
    bottom: float = 0.0


@dataclass
class PlateSurveyResult:
    """Result of a PlateSurvey call."""
    plate_type: str = ""
    barcode: str = ""
    date: str = ""
    serial_number: str = ""
    rows: int = 0
    cols: int = 0
    total_wells: int = 0
    wells: list = field(default_factory=list)  # list[WellSurvey]
    raw_xml: str = ""             # original embedded XML


@dataclass
class TransferWell:
    """One completed source->destination transfer within a DoWellTransfer response."""
    source_name: str = ""          # n  (e.g. "A1")
    source_row: int = 0            # r
    source_col: int = 0            # c
    dest_name: str = ""            # dn (e.g. "B07")
    dest_row: int = 0              # dr
    dest_col: int = 0              # dc
    volume_nL: float = 0.0         # vt  (requested transfer volume)
    actual_volume_nL: float = 0.0  # avt (actual dispensed)
    current_volume_nL: float = 0.0 # cvl (source remaining after transfer)
    start_volume_nL: float = 0.0   # vl  (source before transfer)
    timestamp: str = ""            # t
    fluid: str = ""                # fld
    fluid_units: str = ""          # fldu
    composition: float = 0.0       # fc  (fluid composition %)
    fluid_thickness: float = 0.0   # ft
    reason: str = ""               # empty on success


@dataclass
class SkippedWell:
    """A transfer that was skipped with a reason (bad well, out of range, etc.)."""
    source_name: str = ""
    source_row: int = 0
    source_col: int = 0
    dest_name: str = ""
    dest_row: int = 0
    dest_col: int = 0
    requested_volume_nL: float = 0.0
    reason: str = ""               # error code, e.g. 'MM0202007: ...'


@dataclass
class TransferResult:
    """Parsed result of a DoWellTransfer call."""
    succeeded: bool = False
    status: str = ""
    src_plate: str = ""
    dst_plate: str = ""
    date: str = ""
    serial_number: str = ""
    transfers: list = field(default_factory=list)   # list[TransferWell]
    skipped: list = field(default_factory=list)     # list[SkippedWell]
    raw_xml: str = ""              # the inner <transfer>...</transfer> XML


# ---------------------------------------------------------------------------
# Protocol XML helpers
# ---------------------------------------------------------------------------

def build_protocol_xml(
    transfers: list,
    protocol_name: str = "protocol",
) -> str:
    """
    Build the Protocol XML string passed to DoWellTransfer.
    `transfers` is a list of (source_well, dest_well, volume_nL) tuples, e.g.
        [("A1", "B07", 2.5), ("A2", "F07", 5), ("O10", "B08", 10)]
    Volumes are in nanoliters (Echo native unit, typically 2.5 nL increments).
    """
    layout = []
    for src, dst, vol in transfers:
        # Format volume without trailing zeros
        if float(vol) == int(vol):
            vstr = str(int(vol))
        else:
            vstr = str(vol)
        layout.append(f'<wp n="{src}" dn="{dst}" v="{vstr}" />')
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<Protocol Name="{protocol_name}">'
        f'<Name />'
        f'<Layout>{"".join(layout)}</Layout>'
        f'</Protocol>'
    )


def build_plate_map_xml_sparse(plate_type: str, wells: list) -> str:
    """
    Build a sparse plate map containing only the given wells (the real client
    sends only the source wells being used, not the full grid).
    `wells` is a list of (name, row, col) tuples, e.g. [("A1", 0, 0), ...]
    """
    wells_xml = [
        f'<Well n="{n}" r="{r}" c="{c}" wc="" sid="" />'
        for n, r, c in wells
    ]
    return (
        f'<PlateMap p="{plate_type}"><Wells>'
        + "".join(wells_xml)
        + '</Wells></PlateMap>'
    )


def well_name_to_rc(name: str) -> tuple[int, int]:
    """Convert 'A1', 'B07', 'O10', 'AA12' to (row, col) 0-indexed."""
    m = re.match(r'^([A-Z]+)(\d+)$', name.upper())
    if not m:
        raise ValueError(f"Invalid well name: {name!r}")
    letters, digits = m.group(1), m.group(2)
    if len(letters) == 1:
        row = ord(letters) - ord("A")
    else:  # double-letter rows (e.g. AA..AF for 1536-well)
        row = (ord(letters[0]) - ord("A") + 1) * 26 + (ord(letters[1]) - ord("A"))
    return row, int(digits) - 1


# ---------------------------------------------------------------------------
# Plate map helpers
# ---------------------------------------------------------------------------

def _row_letter(idx: int) -> str:
    """Convert 0-indexed row to plate letter. 0->A, 25->Z, 26->AA, ..."""
    if idx < 26:
        return chr(ord("A") + idx)
    # For 1536-well plates (32 rows) — A..Z, AA..AF
    return chr(ord("A") + (idx // 26) - 1) + chr(ord("A") + (idx % 26))


def generate_plate_map_xml(plate_type: str, rows: int, cols: int) -> str:
    """
    Generate a PlateMap XML document enumerating every well with empty
    well-content / sample-ID fields. Matches the format observed in the
    capture for full-plate surveys.
    """
    wells_xml = []
    for r in range(rows):
        letter = _row_letter(r)
        for c in range(cols):
            wells_xml.append(
                f'<Well n="{letter}{c+1}" r="{r}" c="{c}" wc="" sid="" />'
            )
    return (
        f'<PlateMap p="{plate_type}"><Wells>'
        + "".join(wells_xml)
        + '</Wells></PlateMap>'
    )


# ---------------------------------------------------------------------------
# Echo Client
# ---------------------------------------------------------------------------

class EchoClient:
    """Client for the Beckman Coulter Echo 655 acoustic dispenser."""

    def __init__(self, ip: str, rpc_port: int = 8000, event_port: int = 8010):
        self.ip = ip
        self.rpc_port = rpc_port
        self.event_port = event_port

        # Generate a lock ID matching the observed format:
        # IP:port1:port2:epoch:pid
        self.lock_id = (
            f"{ip}:{rpc_port + 5564}:{rpc_port + 10324}"
            f":{int(time.time())}:{os.getpid()}"
        )
        self.host_header = self.lock_id

        # Event listener state
        self._event_sock: Optional[socket.socket] = None
        self._event_thread: Optional[threading.Thread] = None
        self._stop_events = threading.Event()
        self._event_callback = None

    # -- low-level RPC --

    def _rpc(self, body_xml: str, timeout: float = 30.0) -> Optional[ET.Element]:
        """Send a single SOAP RPC to port 8000 and return parsed XML root."""
        request = _build_request(self.host_header, body_xml)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((self.ip, self.rpc_port))
            sock.sendall(request)
            raw = _recv_all(sock, timeout=timeout)
            return _parse_response(raw)
        finally:
            sock.close()

    def _rpc_ok(self, body_xml: str, timeout: float = 30.0) -> tuple[bool, str, Optional[ET.Element]]:
        """
        RPC that checks SUCCEEDED/Status in the response.
        Returns (ok, status, root). If the device returned a SOAP Fault
        (e.g. 'MM1302001: Unknown Source Plate'), ok=False and status contains
        the fault string.
        """
        root = self._rpc(body_xml, timeout=timeout)
        if root is None:
            return False, "No response", None

        # Check for SOAP Fault first
        for el in root.iter():
            # tag may be namespaced like '{http://...}Fault' or just 'Fault'
            tag = el.tag.split("}", 1)[-1]
            if tag == "Fault":
                faultstring = ""
                for child in el.iter():
                    ctag = child.tag.split("}", 1)[-1]
                    if ctag == "faultstring" and child.text:
                        faultstring = child.text
                        break
                return False, f"SOAP Fault: {faultstring}", root

        succeeded = False
        status = ""
        for el in root.iter():
            if el.tag == "SUCCEEDED":
                succeeded = el.text == "True"
            if el.tag == "Status":
                status = el.text or ""
        return succeeded, status, root

    # -- event channel --

    def subscribe_events(self, callback=None):
        """
        Subscribe to event notifications on port 8010.
        Callback receives (event_id: int, payload: str, source: str, timestamp: int).
        """
        self._event_callback = callback or self._default_event_handler
        self._stop_events.clear()

        # Connect and send the "add" subscription message
        self._event_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._event_sock.settimeout(5.0)
        self._event_sock.connect((self.ip, self.event_port))

        sub_body = f"add{self.lock_id}"
        request = _build_request(self.host_header, sub_body)
        self._event_sock.sendall(request)

        # Start listener thread
        self._event_thread = threading.Thread(
            target=self._event_listener, daemon=True
        )
        self._event_thread.start()
        print(f"[event] Subscribed to event stream on port {self.event_port}")

    def _event_listener(self):
        """Background thread that reads event push messages from port 8010."""
        buf = b""
        self._event_sock.settimeout(1.0)
        while not self._stop_events.is_set():
            try:
                chunk = self._event_sock.recv(8192)
                if not chunk:
                    break
                buf += chunk
                # Process complete HTTP messages in the buffer
                while b"\r\n\r\n" in buf:
                    header_end = buf.index(b"\r\n\r\n") + 4
                    headers_raw = buf[:header_end].decode("utf-8", errors="replace")
                    content_length = 0
                    for line in headers_raw.split("\n"):
                        if line.lower().startswith("content-length:"):
                            content_length = int(line.split(":", 1)[1].strip())
                            break
                    total = header_end + content_length
                    if len(buf) < total:
                        break  # incomplete message, wait for more data
                    msg = buf[:total]
                    buf = buf[total:]
                    self._handle_event_message(msg)
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_events.is_set():
                    print(f"[event] Listener error: {e}")
                break

    def _handle_event_message(self, raw: bytes):
        """Parse a single event push message and invoke callback."""
        root = _parse_response(raw)
        if root is None:
            return
        event_id = 0
        payload = ""
        source = ""
        timestamp = 0
        for el in root.iter():
            if el.tag == "id":
                event_id = int(el.text or "0")
            elif el.tag == "payload":
                payload = (el.text or "").strip()
            elif el.tag == "source":
                source = el.text or ""
            elif el.tag == "timestamp":
                timestamp = int(el.text or "0")
        if self._event_callback:
            self._event_callback(event_id, payload, source, timestamp)

    @staticmethod
    def _default_event_handler(event_id, payload, source, timestamp):
        print(f"[event] {payload}")

    def unsubscribe_events(self):
        """Stop the event listener."""
        self._stop_events.set()
        if self._event_sock:
            try:
                self._event_sock.close()
            except Exception:
                pass
        if self._event_thread:
            self._event_thread.join(timeout=3.0)
        print("[event] Unsubscribed")

    # -- SOAP API methods (matching observed capture sequence) --

    def get_dio_ex2(self) -> DIOEx2:
        root = self._rpc(_empty_call("GetDIOEx2"))
        dio = DIOEx2()
        if root is None:
            return dio
        for el in root.iter():
            tag, text = el.tag, (el.text or "")
            if tag == "MAP": dio.MAP = text == "True"
            elif tag == "MVP": dio.MVP = text == "True"
            elif tag == "CFE": dio.CFE = text == "True"
            elif tag == "DPP": dio.DPP = int(text) if text else 0
            elif tag == "SPP": dio.SPP = int(text) if text else 0
            elif tag == "CouplingFluidTemp": dio.coupling_fluid_temp = float(text) if text else 0.0
            elif tag == "RFSubsystemTemp": dio.rf_subsystem_temp = float(text) if text else 0.0
            if text and tag not in ("SOAP-ENV:Envelope", "SOAP-ENV:Body", "DIOEx2T"):
                dio.raw[tag] = text
        return dio

    def get_instrument_lock_state(self) -> tuple[bool, str]:
        """Returns (is_locked, status_message)."""
        body = _call_with_body(
            "GetInstrumentLockState",
            _element("LockID", self.lock_id, "string")
        )
        ok, status, root = self._rpc_ok(body)
        return ok, status

    def set_pump_dir(self, value: bool = True) -> tuple[bool, str]:
        body = _call_with_body(
            "SetPumpDir",
            _element("Value", str(value), "boolean")
        )
        ok, status, _ = self._rpc_ok(body)
        return ok, status

    def enable_bubbler_pump(self, value: bool = True) -> tuple[bool, str]:
        body = _call_with_body(
            "EnableBubblerPump",
            _element("Value", str(value), "boolean")
        )
        ok, status, _ = self._rpc_ok(body)
        return ok, status

    def get_instrument_info(self) -> InstrumentInfo:
        root = self._rpc(_empty_call("GetInstrumentInfo"))
        info = InstrumentInfo()
        if root is None:
            return info
        for el in root.iter():
            tag, text = el.tag, (el.text or "")
            if tag == "SerialNumber": info.serial_number = text
            elif tag == "InstrumentName": info.instrument_name = text
            elif tag == "IPAddress": info.ip_address = text
            elif tag == "SoftwareVersion": info.software_version = text
            elif tag == "BootTime": info.boot_time = text
            elif tag == "InstrumentStatus": info.instrument_status = text
            elif tag == "Model": info.model = text
        return info

    def get_echo_configuration(self) -> str:
        """Request the full Echo configuration XML. Returns raw config string."""
        inner = _element(
            "xmlEchoConfig",
            '&lt;?xml version=&quot;1.0&quot; encoding=&quot;utf-8&quot;?&gt;'
            '&lt;Configuration internal=&quot;true&quot;&gt;&lt;/Configuration&gt;',
            "string"
        )
        body = _call_with_body("GetEchoConfiguration", inner)
        _, _, root = self._rpc_ok(body)
        if root is None:
            return ""
        for el in root.iter():
            if el.tag == "xmlEchoConfig":
                return el.text or ""
        return ""

    def get_all_src_plate_names(self) -> list[str]:
        root = self._rpc(_empty_call("GetAllSrcPlateNames"))
        plates = []
        if root is None:
            return plates
        for el in root.iter():
            if el.tag == "PlateType" and el.text:
                plates.append(el.text)
        return plates

    def get_all_dest_plate_names(self) -> list[str]:
        root = self._rpc(_empty_call("GetAllDestPlateNames"))
        plates = []
        if root is None:
            return plates
        for el in root.iter():
            if el.tag == "PlateType" and el.text:
                plates.append(el.text)
        return plates

    def get_plate_info_ex(self, plate_type: str) -> PlateInfo:
        body = _call_with_body(
            "GetPlateInfoEx",
            _element("PlateTypeEx", plate_type, "string")
        )
        root = self._rpc(body)
        info = PlateInfo(name=plate_type)
        if root is None:
            return info
        for el in root.iter():
            tag, text = el.tag, (el.text or "")
            if tag == "Rows": info.rows = int(text) if text else 0
            elif tag == "Columns": info.cols = int(text) if text else 0
            elif tag == "WellCapacity": info.well_capacity = float(text) if text else 0.0
            elif tag == "Fluid": info.fluid = text
            elif tag == "PlateFormat": info.plate_format = text
            elif tag == "PlateUsage": info.usage = text
        return info

    def get_dio(self) -> Optional[ET.Element]:
        return self._rpc(_empty_call("GetDIO"))

    def get_pwr_cal(self) -> Optional[ET.Element]:
        return self._rpc(_empty_call("GetPwrCal"))

    def get_all_protocol_names(self) -> list[str]:
        root = self._rpc(_empty_call("GetAllProtocolNames"))
        protocols = []
        if root is not None:
            for el in root.iter():
                if el.tag == "ProtocolName" and el.text:
                    protocols.append(el.text)
        return protocols

    def get_protocol(self, name: str = "") -> Optional[ET.Element]:
        if name:
            body = _call_with_body(
                "GetProtocol",
                _element("ProtocolName", name, "string")
            )
        else:
            body = _empty_call("GetProtocol")
        return self._rpc(body)

    def retrieve_parameter(self, param: str = "") -> Optional[ET.Element]:
        if param:
            body = _call_with_body(
                "RetrieveParameter",
                _element("Parameter", param, "string")
            )
        else:
            body = _empty_call("RetrieveParameter")
        return self._rpc(body)

    def get_current_src_plate_type(self) -> str:
        root = self._rpc(_empty_call("GetCurrentSrcPlateType"))
        if root is not None:
            for el in root.iter():
                if el.tag == "PlateType" and el.text:
                    return el.text
        return ""

    def get_current_dst_plate_type(self) -> str:
        root = self._rpc(_empty_call("GetCurrentDstPlateType"))
        if root is not None:
            for el in root.iter():
                if el.tag == "PlateType" and el.text:
                    return el.text
        return ""

    def is_storage_mode(self) -> bool:
        body = _call_with_body(
            "IsStorageMode",
            _element("IsInStorageMode", "True", "boolean")
        )
        _, _, root = self._rpc_ok(body)
        if root is not None:
            for el in root.iter():
                if el.tag == "IsInStorageMode":
                    return (el.text or "") == "True"
        return False

    def home_axes(self, timeout: float = 60.0) -> tuple[bool, str]:
        """Home all axes. This blocks until homing completes (~22 seconds)."""
        body = _empty_call("HomeAxes")
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    # -- door / stage operations --

    def open_door(self, timeout: float = 10.0) -> tuple[bool, str]:
        """Open the Echo door. Takes ~400ms."""
        ok, status, _ = self._rpc_ok(_empty_call("OpenDoor"), timeout=timeout)
        return ok, status

    def close_door(self, timeout: float = 10.0) -> tuple[bool, str]:
        """Close the Echo door."""
        ok, status, _ = self._rpc_ok(_empty_call("CloseDoor"), timeout=timeout)
        return ok, status

    def present_src_plate_gripper(self, timeout: float = 30.0) -> tuple[bool, str]:
        """Extend source plate gripper out for plate loading. Takes ~1.4s."""
        body = _empty_call("PresentSrcPlateGripper")
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    def retract_src_plate_gripper(
        self,
        plate_type: str,
        barcode_location: str = "Right-Side",
        barcode: str = "",
        timeout: float = 60.0,
    ) -> tuple[bool, str, str]:
        """
        Retract the source plate gripper. Blocks ~15s while barcode scanning.
        Returns (succeeded, status, barcode_result).
        barcode_result will contain the scan error message
        ('Barcode Reading Error: Failed to read bar code') if no plate was
        present, or the actual barcode if it scanned successfully.
        """
        inner = (
            _element("PlateType", plate_type, "string")
            + _element("BarCodeLocation", barcode_location, "string")
            + _element("BarCode", barcode, "string")
        )
        body = _call_with_body("RetractSrcPlateGripper", inner)
        ok, status, root = self._rpc_ok(body, timeout=timeout)
        barcode_result = ""
        if root is not None:
            # Response contains a BarCode element with the scan result
            for el in root.iter():
                if el.tag == "BarCode":
                    barcode_result = el.text or ""
        return ok, status, barcode_result

    def present_dst_plate_gripper(self, timeout: float = 30.0) -> tuple[bool, str]:
        """Extend destination plate gripper out for plate loading."""
        body = _empty_call("PresentDstPlateGripper")
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    def retract_dst_plate_gripper(
        self,
        plate_type: str,
        barcode_location: str = "Right-Side",
        barcode: str = "",
        timeout: float = 60.0,
    ) -> tuple[bool, str, str]:
        """Retract the destination plate gripper (mirrors src)."""
        inner = (
            _element("PlateType", plate_type, "string")
            + _element("BarCodeLocation", barcode_location, "string")
            + _element("BarCode", barcode, "string")
        )
        body = _call_with_body("RetractDstPlateGripper", inner)
        ok, status, root = self._rpc_ok(body, timeout=timeout)
        barcode_result = ""
        if root is not None:
            for el in root.iter():
                if el.tag == "BarCode":
                    barcode_result = el.text or ""
        return ok, status, barcode_result

    # -- plate map / survey --

    def set_plate_map(
        self,
        plate_type: str,
        rows: int = 0,
        cols: int = 0,
        wells: Optional[list] = None,
        timeout: float = 10.0,
    ) -> tuple[bool, str]:
        """
        Register a plate map with the device.
        Two modes:
          - Full plate (for surveys): pass rows=N, cols=M -> auto-generates A1..<end>
          - Sparse (for targeted transfers): pass wells=[(name, row, col), ...]
        Returns (ok, status).
        """
        if wells is not None:
            plate_map_xml = build_plate_map_xml_sparse(plate_type, wells)
        else:
            plate_map_xml = generate_plate_map_xml(plate_type, rows, cols)
        escaped = (
            plate_map_xml.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        body = _call_with_body(
            "SetPlateMap",
            _element("xmlPlateMap", escaped, "string"),
        )
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    def plate_survey(
        self,
        plate_type: str,
        start_row: int = 0,
        start_col: int = 0,
        num_rows: int = 0,
        num_cols: int = 0,
        save: bool = True,
        check_src: bool = False,
        timeout: float = 120.0,
    ) -> PlateSurveyResult:
        """
        Survey the currently loaded source plate.
        Blocks for ~23 seconds for a full 384-well plate.
        If num_rows/num_cols are 0, surveys the full plate (caller should
        pass plate dimensions from GetPlateInfoEx).
        """
        inner = (
            _element("PlateType", plate_type, "string")
            + _element("StartRow", str(start_row), "int")
            + _element("StartCol", str(start_col), "int")
            + _element("NumRows", str(num_rows), "int")
            + _element("NumCols", str(num_cols), "int")
            + _element("Save", str(save), "boolean")
            + _element("CheckSrc", str(check_src), "boolean")
        )
        body = _call_with_body("PlateSurvey", inner)
        _, status, root = self._rpc_ok(body, timeout=timeout)

        result = PlateSurveyResult(plate_type=plate_type)
        if root is None:
            return result

        # Response contains an embedded XML string (double-escaped) inside
        # <PlateSurvey>...</PlateSurvey> element within the response.
        inner_xml = ""
        for el in root.iter():
            if el.tag == "PlateSurvey" and el.text and el.text.strip().startswith("<"):
                inner_xml = el.text
                break
        result.raw_xml = inner_xml

        if inner_xml:
            # Parse the inner <platesurvey> XML
            try:
                survey_root = ET.fromstring(inner_xml)
                result.barcode = survey_root.get("barcode", "")
                result.date = survey_root.get("date", "")
                result.serial_number = survey_root.get("serial_number", "")
                result.rows = int(survey_root.get("rows", "0") or 0)
                result.cols = int(survey_root.get("cols", "0") or 0)
                result.total_wells = int(survey_root.get("totalWells", "0") or 0)
                for w in survey_root.findall("w"):
                    well = WellSurvey(
                        name=w.get("n", ""),
                        row=int(w.get("r", "0") or 0),
                        col=int(w.get("c", "0") or 0),
                        volume_nL=float(w.get("vl", "0") or 0),
                        current_volume_nL=float(w.get("cvl", "0") or 0),
                        status=w.get("status", ""),
                        fluid=w.get("fld", ""),
                        fluid_units=w.get("fldu", ""),
                        x=float(w.get("x", "0") or 0),
                        y=float(w.get("y", "0") or 0),
                        surface_height=float(w.get("s", "0") or 0),
                        thickness=float(w.get("t", "0") or 0),
                        bottom=float(w.get("b", "0") or 0),
                    )
                    result.wells.append(well)
            except ET.ParseError:
                pass

        return result

    def get_fluid_info(self, fluid_type: str, timeout: float = 10.0) -> FluidInfo:
        """Get properties of a known fluid type (e.g. 'DMSO', 'AQ', 'Glycerol')."""
        body = _call_with_body(
            "GetFluidInfo",
            _element("FluidType", fluid_type, "string"),
        )
        _, _, root = self._rpc_ok(body, timeout=timeout)
        info = FluidInfo(name=fluid_type)
        if root is None:
            return info
        for el in root.iter():
            tag, text = el.tag, (el.text or "")
            if tag == "FluidName": info.name = text
            elif tag == "Description": info.description = text
            elif tag == "FCMin": info.fc_min = float(text) if text else 0.0
            elif tag == "FCMax": info.fc_max = float(text) if text else 0.0
            elif tag == "FCUnits": info.fc_units = text
        return info

    def survey_src_plate(self, plate_type: str) -> PlateSurveyResult:
        """
        High-level source-plate survey (mirrors what the real client does
        after loading a source plate):
          GetCurrentSrcPlateType -> RetrieveParameter(Client_IgnoreDestPlateSensor)
          -> RetrieveParameter(Client_IgnoreSourcePlateSensor)
          -> SetPlateMap(full grid) -> GetPlateInfoEx -> PlateSurvey
          -> GetDIOEx2 -> GetPlateInfoEx -> GetFluidInfo
        Blocks ~23s for a 384-well plate.
        """
        print(f"[survey] Surveying source plate ({plate_type})...")

        # Pre-checks matching the real client
        self.get_current_src_plate_type()
        self.retrieve_parameter("Client_IgnoreDestPlateSensor")
        self.retrieve_parameter("Client_IgnoreSourcePlateSensor")

        # Fetch plate dimensions
        plate_info = self.get_plate_info_ex(plate_type)
        if not plate_info.rows or not plate_info.cols:
            print(f"[survey] ERROR: could not fetch dimensions for {plate_type}")
            return PlateSurveyResult(plate_type=plate_type)

        # Register the plate map
        print(f"[survey] Registering {plate_info.rows}x{plate_info.cols} plate map...")
        ok, status = self.set_plate_map(
            plate_type, plate_info.rows, plate_info.cols
        )
        print(f"[survey]   SetPlateMap: {status}")

        # Refresh plate info (the real client re-fetches)
        self.get_plate_info_ex(plate_type)

        # Run the survey
        print("[survey] Running PlateSurvey (this takes ~23 seconds)...")
        t0 = time.time()
        result = self.plate_survey(
            plate_type,
            start_row=0,
            start_col=0,
            num_rows=plate_info.rows,
            num_cols=plate_info.cols,
            save=True,
            check_src=False,
        )
        elapsed = time.time() - t0
        print(f"[survey]   PlateSurvey complete in {elapsed:.1f}s")
        print(f"[survey]   Wells measured: {len(result.wells)} / {result.total_wells}")
        print(f"[survey]   Barcode: {result.barcode}")

        # Post-survey
        dio = self.get_dio_ex2()
        print(f"[survey]   DIO after: SPP={dio.raw.get('SPP','?')}, "
              f"CF={dio.coupling_fluid_temp:.2f}C")

        self.get_plate_info_ex(plate_type)

        # Fluid info lookup
        if plate_info.fluid:
            fluid = self.get_fluid_info(plate_info.fluid)
            print(f"[survey]   Fluid: {fluid.name} "
                  f"({fluid.fc_min}-{fluid.fc_max} {fluid.fc_units})")

        # Show a sample of well volumes
        if result.wells:
            samples = result.wells[:3] + result.wells[-1:]
            for w in samples:
                print(f"[survey]   {w.name}: vol={w.volume_nL:.1f}nL, "
                      f"fluid={w.fluid}")

        return result

    # -- transfer / dispensing --

    def do_well_transfer(
        self,
        protocol_xml: str,
        print_options: Optional[dict] = None,
        timeout: float = 300.0,
    ) -> TransferResult:
        """
        Execute a liquid transfer. Low-level: caller provides the Protocol XML
        (build via build_protocol_xml helper) and the PrintOptions dict.

        Default PrintOptions match the observed client:
          DoPlateSurvey=False (survey done separately), MonitorPower=False,
          HomogeneousPlate=False, SaveSurvey=True, SavePrint=True,
          SrcPlateSensor=False, DstPlateSensor=False,
          SrcPlateSensorOverride=False, DstPlateSensorOverride=False,
          PlateMap=False

        Returns TransferResult with per-well transfer data and skipped wells.
        """
        opts = {
            "DoPlateSurvey": False,
            "MonitorPower": False,
            "HomogeneousPlate": False,
            "SaveSurvey": True,
            "SavePrint": True,
            "SrcPlateSensor": False,
            "DstPlateSensor": False,
            "SrcPlateSensorOverride": False,
            "DstPlateSensorOverride": False,
            "PlateMap": False,
        }
        if print_options:
            opts.update(print_options)

        # The Protocol XML goes inside <ProtocolName>...</ProtocolName>
        # and must be escaped. Observed wire format uses ampersand-entity escaping.
        escaped = (
            protocol_xml.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

        # Build PrintOptions sub-element
        po_inner = "".join(
            _element(k, str(v), "boolean") for k, v in opts.items()
        )

        inner = (
            _element("ProtocolName", escaped, "string")
            + f'<PrintOptions {ENCODING_ATTR}>{po_inner}</PrintOptions>'
        )
        body = _call_with_body("DoWellTransfer", inner)
        ok, status, root = self._rpc_ok(body, timeout=timeout)

        result = TransferResult(succeeded=ok, status=status)
        if root is None:
            return result

        # The <Value> element contains the embedded transfer-report XML
        inner_xml = ""
        for el in root.iter():
            if el.tag == "Value" and el.text and "<transfer" in el.text:
                inner_xml = el.text
                break
        result.raw_xml = inner_xml

        if inner_xml:
            try:
                t_root = ET.fromstring(inner_xml)
                result.date = t_root.get("date", "")
                result.serial_number = t_root.get("serial_number", "")
                # Plate info
                plates = t_root.findall(".//plateInfo/plate")
                if len(plates) >= 1:
                    result.src_plate = plates[0].get("name", "")
                if len(plates) >= 2:
                    result.dst_plate = plates[1].get("name", "")

                # Successful transfers
                for w in t_root.findall(".//printmap/w"):
                    tw = TransferWell(
                        source_name=w.get("n", ""),
                        source_row=int(w.get("r", "0") or 0),
                        source_col=int(w.get("c", "0") or 0),
                        dest_name=w.get("dn", ""),
                        dest_row=int(w.get("dr", "0") or 0),
                        dest_col=int(w.get("dc", "0") or 0),
                        volume_nL=float(w.get("vt", "0") or 0),
                        actual_volume_nL=float(w.get("avt", "0") or 0),
                        current_volume_nL=float(w.get("cvl", "0") or 0),
                        start_volume_nL=float(w.get("vl", "0") or 0),
                        timestamp=w.get("t", ""),
                        fluid=w.get("fld", ""),
                        fluid_units=w.get("fldu", ""),
                        composition=float(w.get("fc", "0") or 0),
                        fluid_thickness=float(w.get("ft", "0") or 0),
                        reason=w.get("reason", ""),
                    )
                    result.transfers.append(tw)

                # Skipped wells
                for w in t_root.findall(".//skippedwells/w"):
                    sw = SkippedWell(
                        source_name=w.get("n", ""),
                        source_row=int(w.get("r", "0") or 0),
                        source_col=int(w.get("c", "0") or 0),
                        dest_name=w.get("dn", ""),
                        dest_row=int(w.get("dr", "0") or 0),
                        dest_col=int(w.get("dc", "0") or 0),
                        requested_volume_nL=float(w.get("vt", "0") or 0),
                        reason=w.get("reason", ""),
                    )
                    result.skipped.append(sw)
            except ET.ParseError:
                pass

        return result

    def transfer_wells(
        self,
        src_plate_type: str,
        dst_plate_type: str,
        transfers: list,
        protocol_name: str = "transfer",
        do_survey: bool = True,
        close_door: bool = True,
        print_options: Optional[dict] = None,
    ) -> TransferResult:
        """
        High-level transfer workflow matching the observed capture:
          Pre-checks -> SetPlateMap (sparse, src wells only) -> GetPlateInfoEx
          -> CloseDoor -> PlateSurvey (partial, rows needed) -> GetDIOEx2 ->
          GetDIO -> DoWellTransfer.

        `transfers` is a list of (source_well, dest_well, volume_nL) tuples:
            [("A1", "B07", 2.5), ("A2", "F07", 5)]
        Volumes are in nanoliters.

        Assumes both source and destination plates are already loaded
        (use load_src_plate / load_dst_plate first).
        """
        print(f"[transfer] {len(transfers)} transfers: "
              f"{src_plate_type} -> {dst_plate_type}")

        # Pre-checks (mirror real client)
        self.get_current_src_plate_type()
        self.get_current_dst_plate_type()
        self.retrieve_parameter("Client_IgnoreDestPlateSensor")
        self.retrieve_parameter("Client_IgnoreSourcePlateSensor")

        # Build sparse source plate map from just the wells being used
        src_wells = []
        max_src_row = 0
        seen = set()
        for src, dst, vol in transfers:
            if src in seen:
                continue
            seen.add(src)
            r, c = well_name_to_rc(src)
            src_wells.append((src, r, c))
            if r > max_src_row:
                max_src_row = r

        print(f"[transfer] SetPlateMap with {len(src_wells)} source wells...")
        ok, status = self.set_plate_map(src_plate_type, wells=src_wells)
        print(f"[transfer]   {status}")

        self.get_plate_info_ex(src_plate_type)

        if close_door:
            print("[transfer] Closing door...")
            self.close_door()

        if do_survey:
            # Partial survey covering only rows that contain source wells
            num_rows = max_src_row + 1
            src_info = self.get_plate_info_ex(src_plate_type)
            print(f"[transfer] Partial PlateSurvey: rows 0..{max_src_row} "
                  f"of {src_info.cols} cols...")
            t0 = time.time()
            survey = self.plate_survey(
                src_plate_type,
                start_row=0, start_col=0,
                num_rows=num_rows, num_cols=src_info.cols,
                save=True, check_src=False,
            )
            print(f"[transfer]   Survey done in {time.time()-t0:.1f}s "
                  f"({len(survey.wells)} wells)")

        self.get_dio_ex2()
        self.get_dio()

        # Build protocol XML and execute
        proto_xml = build_protocol_xml(transfers, protocol_name=protocol_name)
        print(f"[transfer] DoWellTransfer...")
        t0 = time.time()
        result = self.do_well_transfer(proto_xml, print_options=print_options)
        elapsed = time.time() - t0
        print(f"[transfer]   DoWellTransfer: {result.status} "
              f"({elapsed:.1f}s, {len(result.transfers)} OK, "
              f"{len(result.skipped)} skipped)")

        for tw in result.transfers:
            print(f"[transfer]   OK   {tw.source_name} -> {tw.dest_name}: "
                  f"{tw.actual_volume_nL:.1f}nL ({tw.fluid} {tw.composition:.1f}%)")
        for sw in result.skipped:
            print(f"[transfer]   SKIP {sw.source_name} -> {sw.dest_name}: "
                  f"{sw.reason}")

        return result

    # -- destination plate loading (mirror of load_src_plate) --

    def load_dst_plate(
        self,
        plate_type: str,
        barcode_location: str = "Right-Side",
    ) -> tuple[bool, str]:
        """
        Full destination plate load sequence:
          OpenDoor -> PresentDstPlateGripper -> (user places plate) ->
          GetPwrCal -> GetPlateInfoEx -> RetractDstPlateGripper ->
          RetrieveParameter(Client_IgnoreDestPlateSensor) ->
          GetCurrentDstPlateType -> GetDIOEx2 -> GetCurrentDstPlateType.

        Returns (plate_present, barcode_or_error_message).
        """
        print(f"[plate] Loading destination plate ({plate_type})...")
        print("[plate] Opening door...")
        self.open_door()

        print("[plate] Extending destination gripper...")
        self.present_dst_plate_gripper()

        print("[plate] Pre-retract checks...")
        self.get_pwr_cal()
        self.get_plate_info_ex(plate_type)

        print(f"[plate] Retracting gripper (barcode scan at {barcode_location})...")
        ok, status, barcode = self.retract_dst_plate_gripper(
            plate_type, barcode_location=barcode_location
        )
        print(f"[plate] Retract result: {status}")
        print(f"[plate] Barcode scan: {barcode!r}")

        self.retrieve_parameter("Client_IgnoreDestPlateSensor")
        self.get_current_dst_plate_type()

        dio = self.get_dio_ex2()
        plate_present = self.is_dst_plate_present()
        print(f"[plate] Sensor: DPP={dio.raw.get('DPP','?')} "
              f"(0 = no plate detected)")
        print(f"[plate] Plate present: {plate_present}")

        if not plate_present:
            print("[plate] >>> Destination plate NOT detected. <<<")

        return plate_present, barcode

    # -- eject (unload) sequences --

    def eject_src_plate(self, open_door_first: bool = False) -> tuple[bool, str]:
        """
        Eject the source plate: extend gripper, (user removes plate),
        retract empty. Matches observed capture exactly — retract is sent
        with PlateType=None, BarCodeLocation=None, BarCode="" to tell the
        device the gripper is returning without a plate.

        IMPORTANT STATE INTERLOCK: the destination gripper cannot be extended
        while the source gripper is out. Always eject/retract source first
        if you need to eject both.
        """
        print("[eject] Ejecting source plate...")
        if open_door_first:
            self.open_door()

        ok, status = self.present_src_plate_gripper()
        if not ok:
            print(f"[eject]   PresentSrc failed: {status}")
            return ok, status

        # Real capture polls GetCurrentSrcPlateType a few times here (user
        # takes the plate); no explicit wait is required on our side since
        # RetractSrcPlateGripper runs regardless.

        ok, status, _ = self.retract_src_plate_gripper(
            plate_type="None",
            barcode_location="None",
            barcode="",
        )
        print(f"[eject]   RetractSrc: {status}")
        return ok, status

    def eject_dst_plate(self, open_door_first: bool = False) -> tuple[bool, str]:
        """
        Eject the destination plate: extend gripper, (user removes plate),
        retract empty. Same pattern as eject_src_plate.

        Will SOAP-Fault with 'Unable to proceed. Source plate gripper must
        be inside the instrument.' if the source gripper is still extended.
        """
        print("[eject] Ejecting destination plate...")
        if open_door_first:
            self.open_door()

        ok, status = self.present_dst_plate_gripper()
        if not ok:
            print(f"[eject]   PresentDst failed: {status}")
            return ok, status

        ok, status, _ = self.retract_dst_plate_gripper(
            plate_type="None",
            barcode_location="None",
            barcode="",
        )
        print(f"[eject]   RetractDst: {status}")
        return ok, status

    def eject_all(self) -> tuple[bool, str]:
        """
        Eject source then destination (order matters due to state interlock)
        and close the door. Mirrors the 'clear stage' workflow.
        """
        ok_s, status_s = self.eject_src_plate()
        ok_d, status_d = self.eject_dst_plate()
        print("[eject] Closing door...")
        ok_c, status_c = self.close_door()
        overall = ok_s and ok_d and ok_c
        return overall, f"src={status_s}; dst={status_d}; close={status_c}"

    # -- actuator controls --

    def actuate_bubbler_nozzle(self, up: bool, timeout: float = 10.0) -> tuple[bool, str]:
        """
        Raise or lower the coupling fluid via the bubbler nozzle.
        up=True  -> coupling fluid UP (engaged)
        up=False -> coupling fluid DOWN (disengaged)
        Takes ~1.6 seconds to complete.
        """
        body = _call_with_body(
            "ActuateBubblerNozzle",
            _element("Value", str(up), "boolean")
        )
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    def raise_coupling_fluid(self) -> tuple[bool, str]:
        """Alias: coupling fluid up (engage bubbler nozzle)."""
        return self.actuate_bubbler_nozzle(True)

    def lower_coupling_fluid(self) -> tuple[bool, str]:
        """Alias: coupling fluid down (disengage bubbler nozzle)."""
        return self.actuate_bubbler_nozzle(False)

    def actuate_vacuum_nozzle(self, engage: bool, timeout: float = 10.0) -> tuple[bool, str]:
        """
        Engage or release the vacuum nozzle mechanism (holds plate during
        dispensing). REQUIRES a source plate to be loaded — returns SOAP Fault
        'MM1302001: Unknown Source Plate, inset' if none present.

        Distinct from enable_vacuum_nozzle() — that turns the pump on/off and
        does not require a plate; this actuates the physical mechanism.
        """
        body = _call_with_body(
            "ActuateVacuumNozzle",
            _element("Value", str(engage), "boolean")
        )
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    def enable_vacuum_nozzle(self, on: bool, timeout: float = 10.0) -> tuple[bool, str]:
        """
        Turn the vacuum pump on/off. Used around drying cycles. Distinct from
        actuate_vacuum_nozzle() — that moves the physical mechanism and
        requires a plate; this only controls the pump.
        """
        body = _call_with_body(
            "EnableVacuumNozzle",
            _element("Value", str(on), "boolean")
        )
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    def dry_plate(self, dry_type: str = "TWO_PASS", timeout: float = 60.0) -> tuple[bool, str]:
        """
        Run a dry cycle on the currently loaded plate.
        Observed dry_type: "TWO_PASS". Other types may exist.
        Blocks ~8-20 seconds.

        Note: the event stream may emit a WARN 'Dio action is taking longer
        than expected' during the dry. From one capture this appears to be
        a routine timing-threshold notice, not a failure indicator — the call
        still returns SUCCEEDED=True and the cycle completes normally.
        Treat SUCCEEDED as the source of truth unless we learn otherwise.
        """
        body = _call_with_body(
            "DryPlate",
            _element("Type", dry_type, "string")
        )
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    def actuate_ionizer(self, on: bool, timeout: float = 10.0) -> tuple[bool, str]:
        """
        Turn the ionizer bar on or off.
        Takes ~800ms to complete.
        """
        body = _call_with_body(
            "ActuateIonizer",
            _element("Value", str(on), "boolean")
        )
        ok, status, _ = self._rpc_ok(body, timeout=timeout)
        return ok, status

    # -- plate presence detection --

    def is_src_plate_present(self) -> bool:
        """
        Returns True if a source plate is currently loaded and registered.
        Detection: GetCurrentSrcPlateType returns 'None' when no plate is present.
        """
        plate = self.get_current_src_plate_type()
        return bool(plate) and plate.lower() != "none"

    def is_dst_plate_present(self) -> bool:
        """Returns True if a destination plate is currently loaded."""
        plate = self.get_current_dst_plate_type()
        return bool(plate) and plate.lower() != "none"

    def load_src_plate(
        self,
        plate_type: str,
        barcode_location: str = "Right-Side",
    ) -> tuple[bool, str]:
        """
        Full source plate load sequence as observed in the capture:
          OpenDoor -> PresentSrcPlateGripper -> (user places plate) ->
          GetPwrCal -> GetPlateInfoEx -> GetCurrentSrcPlateType ->
          RetractSrcPlateGripper -> RetrieveParameter -> GetCurrentSrcPlateType.

        In real operation there's a pause for the user to physically load the
        plate between PresentSrcPlateGripper and RetractSrcPlateGripper.
        This method runs them back-to-back; in the observed capture the plate
        was absent, so detection logic runs against that case.

        Returns (plate_present, barcode_or_error_message).
        """
        print(f"[plate] Loading source plate ({plate_type})...")
        print("[plate] Opening door...")
        self.open_door()

        print("[plate] Extending source gripper...")
        self.present_src_plate_gripper()

        print("[plate] Pre-retract checks...")
        self.get_pwr_cal()
        self.get_plate_info_ex(plate_type)
        self.get_current_src_plate_type()

        print(f"[plate] Retracting gripper (barcode scan at {barcode_location})...")
        ok, status, barcode = self.retract_src_plate_gripper(
            plate_type, barcode_location=barcode_location
        )
        print(f"[plate] Retract result: {status}")
        print(f"[plate] Barcode scan: {barcode!r}")

        # The real client always checks this parameter after retraction
        self.retrieve_parameter("Client_IgnoreSourcePlateSensor")

        # Verify plate presence via current plate type
        plate_present = self.is_src_plate_present()

        # Confirm via DIO sensor (SPP = Source Plate Position, 0 = empty)
        dio = self.get_dio_ex2()
        spp = dio.raw.get('SPP', '?')
        print(f"[plate] Sensor: SPP={spp} (0 = no plate detected)")
        print(f"[plate] Plate present: {plate_present}")

        if not plate_present:
            print("[plate] >>> Plate NOT detected — gripper retracted empty. <<<")

        return plate_present, barcode

    # -- high-level sequences --

    def initialize(self):
        """
        Run the full initialization sequence as observed in the capture.
        Returns instrument info dict with key data.
        """
        print("=" * 60)
        print("Echo 655 Initialization")
        print("=" * 60)

        # 1. Subscribe to events
        print("\n[1/10] Subscribing to event stream...")
        self.subscribe_events()
        time.sleep(0.1)

        # 2. Get digital I/O status
        print("[2/10] Reading digital I/O status...")
        dio = self.get_dio_ex2()
        print(f"        Coupling fluid temp: {dio.coupling_fluid_temp:.1f} C")
        print(f"        RF subsystem temp:   {dio.rf_subsystem_temp:.1f} C")

        # 3. Check lock state
        print("[3/10] Checking instrument lock state...")
        locked, lock_status = self.get_instrument_lock_state()
        print(f"        Locked: {locked} - {lock_status}")

        # 4. Set pump direction
        print("[4/10] Setting pump direction...")
        ok, status = self.set_pump_dir(True)
        print(f"        {status}")

        # 5. Enable bubbler pump
        print("[5/10] Enabling bubbler pump...")
        ok, status = self.enable_bubbler_pump(True)
        print(f"        {status}")

        # 6. Get instrument info
        print("[6/10] Getting instrument info...")
        info = self.get_instrument_info()
        print(f"        Model:    {info.model}")
        print(f"        Serial:   {info.serial_number}")
        print(f"        Software: {info.software_version}")
        print(f"        Status:   {info.instrument_status}")
        print(f"        Boot:     {info.boot_time}")

        # 7. Get Echo configuration
        print("[7/10] Getting Echo configuration...")
        config = self.get_echo_configuration()
        if config:
            print(f"        Config length: {len(config)} chars")

        # 8. Re-read DIO
        print("[8/10] Re-reading digital I/O...")
        dio = self.get_dio_ex2()

        # 9. Enumerate source plates
        print("[9/10] Enumerating source plate types...")
        src_plates = self.get_all_src_plate_names()
        for p in src_plates:
            plate_info = self.get_plate_info_ex(p)
            print(f"        {p}: {plate_info.rows}x{plate_info.cols} "
                  f"({plate_info.fluid}, {plate_info.well_capacity} uL)")

        # 10. Enumerate destination plates
        print("[10/10] Enumerating destination plate types...")
        dest_plates = self.get_all_dest_plate_names()
        for p in dest_plates:
            plate_info = self.get_plate_info_ex(p)
            print(f"        {p}: {plate_info.rows}x{plate_info.cols} "
                  f"({plate_info.fluid}, {plate_info.well_capacity} uL)")

        # Additional init calls (DIO, power cal, protocols, parameters)
        print("\n[init] Reading power calibration...")
        self.get_dio()
        self.get_pwr_cal()
        self.get_pwr_cal()
        self.get_dio()

        print("[init] Reading protocols...")
        protocols = self.get_all_protocol_names()
        for p in protocols:
            self.get_protocol(p)
        print(f"        {len(protocols)} protocols loaded")

        print("[init] Reading parameters...")
        self.retrieve_parameter()
        self.retrieve_parameter()

        print("[init] Checking current plate types...")
        src = self.get_current_src_plate_type()
        dst = self.get_current_dst_plate_type()
        print(f"        Current src: {src}")
        print(f"        Current dst: {dst}")

        print("[init] Checking storage mode...")
        storage = self.is_storage_mode()
        print(f"        Storage mode: {storage}")

        # Final DIO read
        self.get_dio_ex2()

        print("\n" + "=" * 60)
        print("Initialization complete")
        print("=" * 60)
        return info

    def home(self):
        """
        Run the home stage sequence: pre-checks then HomeAxes.
        """
        print("\n" + "=" * 60)
        print("Homing Stage")
        print("=" * 60)

        # Pre-checks
        print("[home] Checking instrument lock state...")
        locked, lock_status = self.get_instrument_lock_state()
        print(f"        Locked: {locked} - {lock_status}")

        print("[home] Checking storage mode...")
        storage = self.is_storage_mode()
        print(f"        Storage mode: {storage}")

        # Home
        print("[home] Sending HomeAxes command (this may take ~22 seconds)...")
        t0 = time.time()
        ok, status = self.home_axes(timeout=60.0)
        elapsed = time.time() - t0
        print(f"        Result: SUCCEEDED={ok}, Status={status}")
        print(f"        Elapsed: {elapsed:.1f}s")

        # Post-home DIO polling
        print("[home] Post-home sensor verification...")
        for i in range(4):
            dio = self.get_dio_ex2()
            print(f"        Poll {i+1}: CF={dio.coupling_fluid_temp:.2f}C, "
                  f"RF={dio.rf_subsystem_temp:.2f}C")

        print("\n" + "=" * 60)
        print("Homing complete" if ok else "Homing FAILED")
        print("=" * 60)
        return ok

    def disconnect(self):
        """Clean up event subscription."""
        self.unsubscribe_events()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Echo 655 Client")
    parser.add_argument("ip", help="Echo 655 IP address (e.g. 192.168.0.26)")
    parser.add_argument("--rpc-port", type=int, default=8000, help="RPC port (default: 8000)")
    parser.add_argument("--event-port", type=int, default=8010, help="Event port (default: 8010)")
    parser.add_argument("--init-only", action="store_true", help="Connect and initialize only")
    parser.add_argument("--home-only", action="store_true", help="Skip init, just home")
    parser.add_argument(
        "--load-src",
        metavar="PLATE_TYPE",
        help="Run source plate load sequence (OpenDoor/Present/Retract). "
             "e.g. --load-src 384LDV_DMSO",
    )
    parser.add_argument(
        "--survey-src",
        metavar="PLATE_TYPE",
        help="Survey a source plate that is already loaded. "
             "e.g. --survey-src 384PP_DMSO2",
    )
    parser.add_argument(
        "--load-dst",
        metavar="PLATE_TYPE",
        help="Run destination plate load sequence. e.g. --load-dst 384_CellVis",
    )
    parser.add_argument(
        "--transfer",
        nargs=3,
        metavar=("SRC_TYPE", "DST_TYPE", "TRANSFERS"),
        help="Run a transfer. TRANSFERS is comma-separated triples "
             "'src:dst:volume_nL', e.g. "
             "--transfer 384PP_DMSO2 384_CellVis 'A1:B07:2.5,A2:F07:5'",
    )
    parser.add_argument("--no-survey", action="store_true",
                        help="Skip the pre-transfer PlateSurvey")
    parser.add_argument("--no-close-door", action="store_true",
                        help="Skip closing the door before transfer")
    parser.add_argument("--dry", action="store_true",
                        help="Run DryPlate(TWO_PASS) on the currently loaded plate")
    parser.add_argument("--eject-src", action="store_true",
                        help="Eject source plate (Present then Retract empty)")
    parser.add_argument("--eject-dst", action="store_true",
                        help="Eject destination plate")
    parser.add_argument("--eject-all", action="store_true",
                        help="Eject both plates in order and close the door")
    parser.add_argument(
        "--barcode-loc",
        default="Right-Side",
        help="Barcode scanner location for plate load (default: Right-Side)",
    )
    parser.add_argument("--no-subscribe", action="store_true",
                        help="Don't subscribe to the event stream")
    args = parser.parse_args()

    client = EchoClient(args.ip, rpc_port=args.rpc_port, event_port=args.event_port)

    try:
        if args.load_src:
            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)
            plate_present, barcode = client.load_src_plate(
                args.load_src, barcode_location=args.barcode_loc
            )
            print(f"\nResult: plate_present={plate_present}, barcode={barcode!r}")
        elif args.survey_src:
            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)
            result = client.survey_src_plate(args.survey_src)
            print(f"\nSurvey complete: {len(result.wells)} wells measured")
        elif args.load_dst:
            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)
            plate_present, barcode = client.load_dst_plate(
                args.load_dst, barcode_location=args.barcode_loc
            )
            print(f"\nResult: plate_present={plate_present}, barcode={barcode!r}")
        elif args.transfer:
            src_type, dst_type, transfer_spec = args.transfer
            # Parse "A1:B07:2.5,A2:F07:5" -> [("A1","B07",2.5),...]
            transfers = []
            for item in transfer_spec.split(","):
                parts = item.strip().split(":")
                if len(parts) != 3:
                    raise SystemExit(
                        f"Bad transfer spec {item!r}; use 'src:dst:volume_nL'"
                    )
                transfers.append((parts[0], parts[1], float(parts[2])))

            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)

            result = client.transfer_wells(
                src_type, dst_type, transfers,
                do_survey=not args.no_survey,
                close_door=not args.no_close_door,
            )
            print(f"\nTransfer {'SUCCEEDED' if result.succeeded else 'FAILED'}: "
                  f"{len(result.transfers)} OK, {len(result.skipped)} skipped")
        elif args.dry:
            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)
            ok, status = client.dry_plate()
            print(f"\nDryPlate: ok={ok}, status={status}")
        elif args.eject_src:
            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)
            ok, status = client.eject_src_plate()
            print(f"\nEject source: ok={ok}, status={status}")
        elif args.eject_dst:
            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)
            ok, status = client.eject_dst_plate()
            print(f"\nEject destination: ok={ok}, status={status}")
        elif args.eject_all:
            if not args.no_subscribe:
                client.subscribe_events()
                time.sleep(0.1)
            ok, status = client.eject_all()
            print(f"\nEject all: ok={ok}, status={status}")
        else:
            if not args.home_only:
                client.initialize()
            if not args.init_only:
                client.home()
    except ConnectionRefusedError:
        print(f"\nERROR: Could not connect to Echo at {args.ip}:{args.rpc_port}")
        print("Check that the device is powered on and reachable.")
    except socket.timeout:
        print("\nERROR: Connection timed out. The device may be busy or unreachable.")
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
