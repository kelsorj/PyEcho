"""
Unit tests for echo_client.py — all run offline with no hardware required.
"""

import gzip
import html
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import pytest

from echo_client import (
    EchoClient,
    EchoValidationError,
    PlateSurveyResult,
    WellSurvey,
    _build_request,
    _parse_response,
    _recv_all,
    _row_letter,
    build_plate_map_xml_sparse,
    build_protocol_xml,
    generate_plate_map_xml,
    well_name_to_rc,
    _validate_volume_nL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _soap_xml(body: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
        '<SOAP-ENV:Body>' + body + '</SOAP-ENV:Body>'
        '</SOAP-ENV:Envelope>'
    )


def _make_http_response(xml: str, gzip_body: bool = True) -> bytes:
    body = gzip.compress(xml.encode()) if gzip_body else xml.encode()
    header = (
        b"HTTP/1.0 200 OK\r\n"
        b"Content-Type: text/xml\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n"
    )
    return header + body


# ---------------------------------------------------------------------------
# 1a. Transport — _build_request
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def test_gzip_magic_bytes_in_body(self):
        raw = _build_request("192.168.1.1:8000", "<SOAP/>")
        split = raw.index(b"\r\n\r\n") + 4
        body = raw[split:]
        assert body[:2] == b"\x1f\x8b", "Body should start with gzip magic bytes"

    def test_body_is_decompressible(self):
        xml = "<Test>hello</Test>"
        raw = _build_request("host:8000", xml)
        split = raw.index(b"\r\n\r\n") + 4
        body = raw[split:]
        decompressed = gzip.decompress(body)
        assert b"<Test>hello</Test>" in decompressed

    def test_content_length_matches_compressed_body(self):
        raw = _build_request("host:8000", "<SOAP/>")
        split = raw.index(b"\r\n\r\n") + 4
        headers = raw[:split].decode()
        body = raw[split:]
        cl_line = next(
            line for line in headers.split("\n")
            if line.lower().startswith("content-length:")
        )
        declared_length = int(cl_line.split(":", 1)[1].strip())
        assert declared_length == len(body)

    def test_is_client_header_included_by_default(self):
        raw = _build_request("host:8000", "<SOAP/>")
        split = raw.index(b"\r\n\r\n") + 4
        headers = raw[:split].decode()
        assert "Client: 3.1.1" in headers

    def test_is_client_header_omitted_when_false(self):
        raw = _build_request("host:8000", "<SOAP/>", is_client=False)
        split = raw.index(b"\r\n\r\n") + 4
        headers = raw[:split].decode()
        assert "Client:" not in headers


# ---------------------------------------------------------------------------
# 1b. Transport — _recv_all
# ---------------------------------------------------------------------------

class TestRecvAll:
    def test_assembles_header_then_body(self):
        body = b"<xml/>"
        header = (
            b"HTTP/1.0 200 OK\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"\r\n"
        )
        sock = MagicMock()
        sock.recv.side_effect = [header, body]
        result = _recv_all(sock)
        assert result == header + body

    def test_multiple_body_chunks(self):
        body = b"A" * 100
        header = b"HTTP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n"
        # Deliver body in 10-byte chunks
        chunks = [header] + [body[i:i+10] for i in range(0, 100, 10)]
        sock = MagicMock()
        sock.recv.side_effect = chunks
        result = _recv_all(sock)
        assert result == header + body

    def test_empty_response_returns_empty(self):
        sock = MagicMock()
        sock.recv.return_value = b""
        result = _recv_all(sock)
        assert result == b""

    def test_sets_socket_timeout(self):
        sock = MagicMock()
        sock.recv.return_value = b""
        _recv_all(sock, timeout=42.0)
        sock.settimeout.assert_called_once_with(42.0)


# ---------------------------------------------------------------------------
# 1c. Transport — _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_gzip_body_parsed(self):
        xml = _soap_xml("<Ping/>")
        raw = _make_http_response(xml, gzip_body=True)
        root = _parse_response(raw)
        assert root is not None

    def test_plain_text_fallback(self):
        xml = _soap_xml("<Ping/>")
        raw = _make_http_response(xml, gzip_body=False)
        root = _parse_response(raw)
        assert root is not None

    def test_returns_none_for_empty_body(self):
        raw = b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n"
        root = _parse_response(raw)
        assert root is None

    def test_returns_none_for_no_header_separator(self):
        root = _parse_response(b"garbage no separator")
        assert root is None

    def test_root_element_accessible(self):
        xml = _soap_xml("<GetDIOEx2Response><SUCCEEDED>True</SUCCEEDED></GetDIOEx2Response>")
        raw = _make_http_response(xml, gzip_body=True)
        root = _parse_response(raw)
        assert root is not None
        tags = [el.tag.split("}")[-1] for el in root.iter()]
        assert "SUCCEEDED" in tags


# ---------------------------------------------------------------------------
# 1d. _rpc_ok — SUCCEEDED/Fault parsing
# ---------------------------------------------------------------------------

class TestRpcOk:
    def _client(self):
        c = EchoClient.__new__(EchoClient)
        return c

    def test_succeeded_true(self):
        xml = _soap_xml(
            "<Response><SUCCEEDED>True</SUCCEEDED><Status>Completed</Status></Response>"
        )
        root = ET.fromstring(xml)
        c = self._client()
        with patch.object(c, "_rpc", return_value=root):
            ok, status, r = c._rpc_ok("body")
        assert ok is True
        assert status == "Completed"
        assert r is root

    def test_succeeded_false(self):
        xml = _soap_xml(
            "<Response><SUCCEEDED>False</SUCCEEDED>"
            "<Status>MM0202007: Problem calc. well fluid volume</Status></Response>"
        )
        root = ET.fromstring(xml)
        c = self._client()
        with patch.object(c, "_rpc", return_value=root):
            ok, status, _ = c._rpc_ok("body")
        assert ok is False
        assert "MM0202007" in status

    def test_soap_fault(self):
        xml = _soap_xml(
            '<SOAP-ENV:Fault xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
            "<faultcode>Server</faultcode>"
            "<faultstring>MM1302001: Unknown Source Plate, inset</faultstring>"
            "</SOAP-ENV:Fault>"
        )
        root = ET.fromstring(xml)
        c = self._client()
        with patch.object(c, "_rpc", return_value=root):
            ok, status, _ = c._rpc_ok("body")
        assert ok is False
        assert "MM1302001" in status

    def test_no_response_returns_false(self):
        c = self._client()
        with patch.object(c, "_rpc", return_value=None):
            ok, status, r = c._rpc_ok("body")
        assert ok is False
        assert r is None


# ---------------------------------------------------------------------------
# 2a. well_name_to_rc
# ---------------------------------------------------------------------------

class TestWellNameToRc:
    def test_single_letter_a1(self):
        assert well_name_to_rc("A1") == (0, 0)

    def test_single_letter_h12(self):
        assert well_name_to_rc("H12") == (7, 11)

    def test_single_letter_p24(self):
        assert well_name_to_rc("P24") == (15, 23)

    def test_double_letter_aa1(self):
        assert well_name_to_rc("AA1") == (26, 0)

    def test_double_letter_ab3(self):
        assert well_name_to_rc("AB3") == (27, 2)

    def test_case_insensitive(self):
        assert well_name_to_rc("a1") == well_name_to_rc("A1")
        assert well_name_to_rc("h12") == well_name_to_rc("H12")

    def test_zero_padded_column(self):
        assert well_name_to_rc("B07") == (1, 6)

    def test_invalid_no_letters_raises(self):
        with pytest.raises(EchoValidationError):
            well_name_to_rc("123")

    def test_invalid_no_digits_raises(self):
        with pytest.raises(EchoValidationError):
            well_name_to_rc("ABC")

    def test_invalid_empty_raises(self):
        with pytest.raises(EchoValidationError):
            well_name_to_rc("")

    def test_invalid_spaces_raises(self):
        with pytest.raises(EchoValidationError):
            well_name_to_rc("A 1")

    def test_is_also_value_error(self):
        with pytest.raises(ValueError):
            well_name_to_rc("bad")


# ---------------------------------------------------------------------------
# 2b. _validate_volume_nL
# ---------------------------------------------------------------------------

class TestValidateVolumeNL:
    def test_valid_2_5(self):
        _validate_volume_nL(2.5)  # no exception

    def test_valid_5_0(self):
        _validate_volume_nL(5.0)

    def test_valid_100_0(self):
        _validate_volume_nL(100.0)

    def test_zero_raises(self):
        with pytest.raises(EchoValidationError, match="positive"):
            _validate_volume_nL(0.0)

    def test_negative_raises(self):
        with pytest.raises(EchoValidationError, match="positive"):
            _validate_volume_nL(-2.5)

    def test_non_multiple_raises(self):
        with pytest.raises(EchoValidationError, match="multiple of 2.5"):
            _validate_volume_nL(3.0)

    def test_non_multiple_1_raises(self):
        with pytest.raises(EchoValidationError, match="multiple of 2.5"):
            _validate_volume_nL(1.0)

    def test_context_appears_in_message(self):
        with pytest.raises(EchoValidationError, match="A1→B07"):
            _validate_volume_nL(3.0, "A1→B07")


# ---------------------------------------------------------------------------
# 3. _row_letter
# ---------------------------------------------------------------------------

class TestRowLetter:
    def test_zero_is_a(self):
        assert _row_letter(0) == "A"

    def test_25_is_z(self):
        assert _row_letter(25) == "Z"

    def test_26_is_aa(self):
        assert _row_letter(26) == "AA"

    def test_27_is_ab(self):
        assert _row_letter(27) == "AB"

    def test_51_is_az(self):
        assert _row_letter(51) == "AZ"

    def test_52_is_ba(self):
        assert _row_letter(52) == "BA"


# ---------------------------------------------------------------------------
# 4. generate_plate_map_xml
# ---------------------------------------------------------------------------

class TestGeneratePlateMapXml:
    def test_384_well_count(self):
        xml = generate_plate_map_xml("384LDV_DMSO", rows=16, cols=24)
        root = ET.fromstring(xml)
        wells = root.findall(".//Well")
        assert len(wells) == 384

    def test_1536_well_count(self):
        xml = generate_plate_map_xml("1536", rows=32, cols=48)
        root = ET.fromstring(xml)
        wells = root.findall(".//Well")
        assert len(wells) == 1536

    def test_first_well_is_a1(self):
        xml = generate_plate_map_xml("TestPlate", rows=16, cols=24)
        root = ET.fromstring(xml)
        first = root.findall(".//Well")[0]
        assert first.get("n") == "A1"
        assert first.get("r") == "0"
        assert first.get("c") == "0"

    def test_last_well_row_col(self):
        xml = generate_plate_map_xml("TestPlate", rows=16, cols=24)
        root = ET.fromstring(xml)
        last = root.findall(".//Well")[-1]
        assert last.get("r") == "15"
        assert last.get("c") == "23"

    def test_plate_type_in_attribute(self):
        xml = generate_plate_map_xml("MyPlate", rows=8, cols=12)
        root = ET.fromstring(xml)
        assert root.get("p") == "MyPlate"


# ---------------------------------------------------------------------------
# 5. build_plate_map_xml_sparse
# ---------------------------------------------------------------------------

class TestBuildPlateMapXmlSparse:
    def test_only_listed_wells_present(self):
        wells = [("A1", 0, 0), ("B3", 1, 2), ("H12", 7, 11)]
        xml = build_plate_map_xml_sparse("TestPlate", wells)
        root = ET.fromstring(xml)
        found = root.findall(".//Well")
        assert len(found) == 3

    def test_well_attributes_correct(self):
        wells = [("A1", 0, 0)]
        xml = build_plate_map_xml_sparse("TestPlate", wells)
        root = ET.fromstring(xml)
        w = root.findall(".//Well")[0]
        assert w.get("n") == "A1"
        assert w.get("r") == "0"
        assert w.get("c") == "0"

    def test_empty_wells_list(self):
        xml = build_plate_map_xml_sparse("TestPlate", [])
        root = ET.fromstring(xml)
        assert root.findall(".//Well") == []

    def test_plate_type_in_attribute(self):
        xml = build_plate_map_xml_sparse("MyType", [("A1", 0, 0)])
        root = ET.fromstring(xml)
        assert root.get("p") == "MyType"


# ---------------------------------------------------------------------------
# 6. build_protocol_xml
# ---------------------------------------------------------------------------

class TestBuildProtocolXml:
    def test_two_transfers_produce_two_wp_elements(self):
        transfers = [("A1", "B07", 2.5), ("A2", "F07", 5.0)]
        xml = build_protocol_xml(transfers)
        root = ET.fromstring(xml)
        wps = root.findall(".//wp")
        assert len(wps) == 2

    def test_well_names_in_attributes(self):
        xml = build_protocol_xml([("O10", "B08", 10.0)])
        root = ET.fromstring(xml)
        wp = root.findall(".//wp")[0]
        assert wp.get("n") == "O10"
        assert wp.get("dn") == "B08"

    def test_volume_integer_no_decimal(self):
        xml = build_protocol_xml([("A1", "B01", 5.0)])
        assert 'v="5"' in xml

    def test_volume_float_preserved(self):
        xml = build_protocol_xml([("A1", "B01", 2.5)])
        assert 'v="2.5"' in xml

    def test_empty_transfers_no_wp(self):
        xml = build_protocol_xml([])
        root = ET.fromstring(xml)
        assert root.findall(".//wp") == []

    def test_protocol_name_in_output(self):
        xml = build_protocol_xml([], protocol_name="my-run")
        root = ET.fromstring(xml)
        assert root.get("Name") == "my-run"


# ---------------------------------------------------------------------------
# 7. EchoClient data-class parsing via mocked _rpc
# ---------------------------------------------------------------------------

class TestGetInstrumentInfo:
    def test_fields_populated(self):
        xml = _soap_xml(
            "<GetInstrumentInfoResponse>"
            "<SerialNumber>SN-001</SerialNumber>"
            "<InstrumentName>Echo 655</InstrumentName>"
            "<IPAddress>192.168.1.10</IPAddress>"
            "<SoftwareVersion>3.2.2</SoftwareVersion>"
            "<BootTime>2024-01-01T00:00:00</BootTime>"
            "<InstrumentStatus>Ready</InstrumentStatus>"
            "<Model>Echo 655</Model>"
            "</GetInstrumentInfoResponse>"
        )
        root = ET.fromstring(xml)
        c = EchoClient.__new__(EchoClient)
        with patch.object(c, "_rpc", return_value=root):
            info = c.get_instrument_info()
        assert info.serial_number == "SN-001"
        assert info.model == "Echo 655"
        assert info.software_version == "3.2.2"
        assert info.instrument_status == "Ready"
        assert info.ip_address == "192.168.1.10"


class TestGetDioEx2:
    def test_temperatures_and_flags(self):
        xml = _soap_xml(
            "<GetDIOEx2Response>"
            "<MAP>True</MAP><MVP>False</MVP><CFE>True</CFE>"
            "<DPP>1</DPP><SPP>2</SPP>"
            "<CouplingFluidTemp>25.5</CouplingFluidTemp>"
            "<RFSubsystemTemp>30.1</RFSubsystemTemp>"
            "</GetDIOEx2Response>"
        )
        root = ET.fromstring(xml)
        c = EchoClient.__new__(EchoClient)
        with patch.object(c, "_rpc", return_value=root):
            dio = c.get_dio_ex2()
        assert dio.MAP is True
        assert dio.MVP is False
        assert dio.CFE is True
        assert dio.DPP == 1
        assert dio.SPP == 2
        assert abs(dio.coupling_fluid_temp - 25.5) < 0.001
        assert abs(dio.rf_subsystem_temp - 30.1) < 0.001


class TestGetPlateInfoEx:
    def test_dimensions_parsed(self):
        xml = _soap_xml(
            "<GetPlateInfoExResponse>"
            "<Rows>16</Rows><Columns>24</Columns>"
            "<WellCapacity>65.0</WellCapacity>"
            "<Fluid>DMSO</Fluid>"
            "<PlateFormat>384</PlateFormat>"
            "<PlateUsage>Source</PlateUsage>"
            "</GetPlateInfoExResponse>"
        )
        root = ET.fromstring(xml)
        c = EchoClient.__new__(EchoClient)
        with patch.object(c, "_rpc", return_value=root):
            info = c.get_plate_info_ex("384LDV_DMSO")
        assert info.rows == 16
        assert info.cols == 24
        assert abs(info.well_capacity - 65.0) < 0.001
        assert info.fluid == "DMSO"


class TestPlateSurveyParsing:
    def test_wells_parsed_from_embedded_xml(self):
        inner = (
            '<platesurvey barcode="" date="2024-01-01" serial_number="SN-001"'
            ' rows="16" cols="24" totalWells="5">'
            '<w n="A1" r="0" c="0" vl="1000.0" cvl="1000.0" status="OK"'
            ' fld="DMSO" fldu="nL" x="1.0" y="1.0" s="0.0" t="0.0" b="0.0"/>'
            '<w n="A2" r="0" c="1" vl="900.0" cvl="900.0" status="OK"'
            ' fld="DMSO" fldu="nL" x="2.0" y="1.0" s="0.0" t="0.0" b="0.0"/>'
            "</platesurvey>"
        )
        # The real instrument sends the inner XML as escaped text inside <PlateSurvey>.
        # html.escape replicates that double-encoding so ET sees it as text content.
        xml = _soap_xml(
            "<PlateSurveyResponse>"
            "<SUCCEEDED>True</SUCCEEDED><Status>OK</Status>"
            "<PlateSurvey>" + html.escape(inner) + "</PlateSurvey>"
            "</PlateSurveyResponse>"
        )
        root = ET.fromstring(xml)
        c = EchoClient.__new__(EchoClient)
        with patch.object(c, "_rpc_ok", return_value=(True, "OK", root)):
            result = c.plate_survey("384LDV_DMSO")
        assert len(result.wells) == 2
        assert result.wells[0].name == "A1"
        assert abs(result.wells[0].volume_nL - 1000.0) < 0.001
        assert result.wells[1].name == "A2"
        assert result.total_wells == 5
