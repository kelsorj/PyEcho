"""
Integration-style tests for echo_api.py using FastAPI TestClient.
All Echo hardware calls are replaced by MagicMock objects — no instrument needed.
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import echo_api
from echo_api import app, parse_picklist_csv
from echo_client import (
    DIOEx2,
    InstrumentInfo,
    PlateInfo,
    PlateSurveyResult,
    TransferResult,
    TransferWell,
    WellSurvey,
)

client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_echo_state():
    """Ensure no lingering global echo client between tests."""
    echo_api._echo = None
    yield
    echo_api._echo = None


@pytest.fixture
def mock_echo():
    """Inject a mock EchoClient as the live global client."""
    m = MagicMock()
    echo_api._echo = m
    return m


# ---------------------------------------------------------------------------
# 1. Connection lifecycle
# ---------------------------------------------------------------------------

class TestConnection:
    def test_connect_success(self):
        with patch("echo_api.EchoClient") as MockCls:
            inst = MagicMock()
            MockCls.return_value = inst
            inst.get_instrument_info.return_value = InstrumentInfo(
                serial_number="SN-123", model="Echo 655",
                software_version="3.2.2", instrument_status="Ready",
            )
            inst.subscribe_events.return_value = None

            r = client.post("/api/connect", json={"ip": "192.168.1.100"})

        assert r.status_code == 200
        data = r.json()
        assert data["connected"] is True
        assert data["ip"] == "192.168.1.100"
        assert data["info"]["serial_number"] == "SN-123"

    def test_connect_already_connected(self, mock_echo):
        r = client.post("/api/connect", json={"ip": "192.168.1.100"})
        assert r.status_code == 400
        assert "Already connected" in r.json()["detail"]

    def test_connect_empty_ip_rejected(self):
        r = client.post("/api/connect", json={"ip": "   "})
        assert r.status_code == 422

    def test_connect_missing_ip_rejected(self):
        r = client.post("/api/connect", json={"rpc_port": 8000})
        assert r.status_code == 422

    def test_disconnect_when_connected(self, mock_echo):
        r = client.post("/api/disconnect")
        assert r.status_code == 200
        assert r.json()["connected"] is False
        assert echo_api._echo is None

    def test_disconnect_when_not_connected(self):
        r = client.post("/api/disconnect")
        assert r.status_code == 200
        assert r.json()["connected"] is False

    def test_status_not_connected(self):
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["connected"] is False

    def test_status_connected(self, mock_echo):
        mock_echo.ip = "192.168.1.100"
        mock_echo.get_instrument_info.return_value = InstrumentInfo(
            serial_number="SN-001", model="Echo 655"
        )
        mock_echo.get_dio_ex2.return_value = DIOEx2(
            coupling_fluid_temp=25.0, rf_subsystem_temp=28.0
        )
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["connected"] is True


# ---------------------------------------------------------------------------
# 2. Control endpoints
# ---------------------------------------------------------------------------

class TestControls:
    def test_requires_connection(self):
        for path in ["/api/door/open", "/api/door/close", "/api/home"]:
            r = client.post(path)
            assert r.status_code == 503, f"{path} should be 503 when not connected"

    def test_open_door_success(self, mock_echo):
        mock_echo.open_door.return_value = (True, "Door open")
        r = client.post("/api/door/open")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_open_door_soap_failure(self, mock_echo):
        mock_echo.open_door.return_value = (False, "Fault: door locked")
        r = client.post("/api/door/open")
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "door locked" in r.json()["status"]

    def test_close_door_success(self, mock_echo):
        mock_echo.close_door.return_value = (True, "OK")
        r = client.post("/api/door/close")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_home_success(self, mock_echo):
        mock_echo.home_axes.return_value = (True, "Homing complete")
        r = client.post("/api/home")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_dry_valid_type(self, mock_echo):
        mock_echo.dry_plate.return_value = (True, "Dry complete")
        r = client.post("/api/dry", json={"dry_type": "TWO_PASS"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_dry_invalid_type_rejected(self, mock_echo):
        r = client.post("/api/dry", json={"dry_type": "BOGUS_TYPE"})
        assert r.status_code == 422

    def test_dry_default_type(self, mock_echo):
        mock_echo.dry_plate.return_value = (True, "OK")
        r = client.post("/api/dry", json={})
        assert r.status_code == 200

    def test_ionizer_true(self, mock_echo):
        mock_echo.actuate_ionizer.return_value = (True, "Ionizer on")
        r = client.post("/api/ionizer", json={"value": True})
        assert r.status_code == 200

    def test_ionizer_false(self, mock_echo):
        mock_echo.actuate_ionizer.return_value = (True, "Ionizer off")
        r = client.post("/api/ionizer", json={"value": False})
        assert r.status_code == 200

    def test_vacuum_pump(self, mock_echo):
        mock_echo.enable_vacuum_nozzle.return_value = (True, "OK")
        r = client.post("/api/vacuum/pump", json={"value": True})
        assert r.status_code == 200

    def test_coupling_fluid_pump(self, mock_echo):
        mock_echo.enable_bubbler_pump.return_value = (True, "OK")
        r = client.post("/api/coupling-fluid/pump", json={"value": True})
        assert r.status_code == 200

    def test_coupling_fluid_nozzle(self, mock_echo):
        mock_echo.actuate_bubbler_nozzle.return_value = (True, "OK")
        r = client.post("/api/coupling-fluid/nozzle", json={"value": True})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 3. Survey & Transfer
# ---------------------------------------------------------------------------

class TestSurveyAndTransfer:
    def test_survey_returns_well_list(self, mock_echo):
        wells = [
            WellSurvey(name=f"A{i+1}", row=0, col=i, volume_nL=1000.0)
            for i in range(5)
        ]
        mock_echo.survey_src_plate.return_value = PlateSurveyResult(
            plate_type="384LDV_DMSO", wells=wells, rows=16, cols=24, total_wells=5
        )
        r = client.post("/api/survey", json={"plate_type": "384LDV_DMSO"})
        assert r.status_code == 200
        data = r.json()
        assert len(data["wells"]) == 5
        assert data["wells"][0]["name"] == "A1"
        assert data["plate_type"] == "384LDV_DMSO"

    def test_transfer_missing_src_plate_type_rejected(self, mock_echo):
        r = client.post("/api/transfer", json={
            "dst_plate_type": "384_CellVis",
            "transfers": [{"src": "A1", "dst": "B07", "volume_nL": 2.5}],
        })
        assert r.status_code == 422

    def test_transfer_missing_dst_plate_type_rejected(self, mock_echo):
        r = client.post("/api/transfer", json={
            "src_plate_type": "384LDV_DMSO",
            "transfers": [{"src": "A1", "dst": "B07", "volume_nL": 2.5}],
        })
        assert r.status_code == 422

    def test_transfer_negative_volume_rejected(self, mock_echo):
        r = client.post("/api/transfer", json={
            "src_plate_type": "384LDV_DMSO",
            "dst_plate_type": "384_CellVis",
            "transfers": [{"src": "A1", "dst": "B07", "volume_nL": -1.0}],
        })
        assert r.status_code == 422

    def test_transfer_zero_volume_rejected(self, mock_echo):
        r = client.post("/api/transfer", json={
            "src_plate_type": "384LDV_DMSO",
            "dst_plate_type": "384_CellVis",
            "transfers": [{"src": "A1", "dst": "B07", "volume_nL": 0}],
        })
        assert r.status_code == 422

    def test_transfer_bad_well_name_rejected(self, mock_echo):
        r = client.post("/api/transfer", json={
            "src_plate_type": "384LDV_DMSO",
            "dst_plate_type": "384_CellVis",
            "transfers": [{"src": "INVALID", "dst": "B07", "volume_nL": 2.5}],
        })
        assert r.status_code == 400

    def test_transfer_success(self, mock_echo):
        mock_echo.transfer_wells.return_value = TransferResult(
            succeeded=True,
            status="OK",
            src_plate="384LDV_DMSO",
            dst_plate="384_CellVis",
            transfers=[
                TransferWell(source_name="A1", dest_name="B07", volume_nL=2.5,
                             actual_volume_nL=2.5, fluid="DMSO")
            ],
        )
        r = client.post("/api/transfer", json={
            "src_plate_type": "384LDV_DMSO",
            "dst_plate_type": "384_CellVis",
            "transfers": [{"src": "A1", "dst": "B07", "volume_nL": 2.5}],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["succeeded"] is True
        assert len(data["transfers"]) == 1
        assert data["transfers"][0]["source"] == "A1"

    def test_transfer_empty_list_allowed(self, mock_echo):
        mock_echo.transfer_wells.return_value = TransferResult(succeeded=True)
        r = client.post("/api/transfer", json={
            "src_plate_type": "384LDV_DMSO",
            "dst_plate_type": "384_CellVis",
            "transfers": [],
        })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 4. CSV Pick-List Parser
# ---------------------------------------------------------------------------

STANDARD_CSV = "Source Well,Destination Well,Transfer Volume\nA1,B07,2.5\nA2,F07,5\n"
ALIAS_CSV = "Source Well,Destination Well,Volume (nL)\nA1,B07,2.5\n"
TAB_CSV = "Source Well\tDestination Well\tTransfer Volume\nA1\tB07\t2.5\n"
SEMI_CSV = "Source Well;Destination Well;Transfer Volume\nA1;B07;2.5\n"
MISSING_DST_CSV = "Source Well,Transfer Volume\nA1,2.5\n"
BLANK_ROW_CSV = (
    "Source Well,Destination Well,Transfer Volume\n"
    "A1,B07,2.5\n\n\nA2,F07,5\n"
)
PLATE_TYPE_CSV = (
    "Source Well,Destination Well,Transfer Volume,Source Plate Type\n"
    "A1,B07,2.5,384LDV_DMSO\n"
)


class TestParsePicklist:
    def test_standard_columns(self):
        result = parse_picklist_csv(STANDARD_CSV)
        assert len(result["transfers"]) == 2
        assert result["transfers"][0] == {"src": "A1", "dst": "B07", "volume_nL": 2.5}
        assert result["transfers"][1] == {"src": "A2", "dst": "F07", "volume_nL": 5.0}

    def test_alias_volume_column(self):
        result = parse_picklist_csv(ALIAS_CSV)
        assert len(result["transfers"]) == 1
        assert result["transfers"][0]["volume_nL"] == 2.5

    def test_tab_delimited(self):
        result = parse_picklist_csv(TAB_CSV)
        assert len(result["transfers"]) == 1
        assert result["transfers"][0]["src"] == "A1"

    def test_semicolon_delimited(self):
        result = parse_picklist_csv(SEMI_CSV)
        assert len(result["transfers"]) == 1
        assert result["transfers"][0]["dst"] == "B07"

    def test_missing_required_column_raises(self):
        with pytest.raises(ValueError, match="destination well"):
            parse_picklist_csv(MISSING_DST_CSV)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_picklist_csv("")

    def test_blank_rows_skipped(self):
        result = parse_picklist_csv(BLANK_ROW_CSV)
        assert len(result["transfers"]) == 2

    def test_infers_src_plate_type(self):
        result = parse_picklist_csv(PLATE_TYPE_CSV)
        assert result["src_plate_type"] == "384LDV_DMSO"

    def test_no_plate_type_column_returns_none(self):
        result = parse_picklist_csv(STANDARD_CSV)
        assert result["src_plate_type"] is None
        assert result["dst_plate_type"] is None

    def test_invalid_volume_raises(self):
        bad = "Source Well,Destination Well,Transfer Volume\nA1,B07,not-a-number\n"
        with pytest.raises(ValueError, match="not a number"):
            parse_picklist_csv(bad)


class TestParsePicklist_Endpoint:
    def _upload(self, content: bytes, filename: str = "test.csv") -> dict:
        r = client.post(
            "/api/picklist/parse",
            files={"file": (filename, io.BytesIO(content), "text/csv")},
        )
        return r

    def test_standard_upload(self):
        r = self._upload(STANDARD_CSV.encode())
        assert r.status_code == 200
        assert len(r.json()["transfers"]) == 2

    def test_bom_stripped(self):
        bom_csv = ("\ufeff" + STANDARD_CSV).encode("utf-8-sig")
        r = self._upload(bom_csv)
        assert r.status_code == 200
        assert len(r.json()["transfers"]) == 2

    def test_missing_column_returns_400(self):
        r = self._upload(MISSING_DST_CSV.encode())
        assert r.status_code == 400

    def test_filename_in_response(self):
        r = self._upload(STANDARD_CSV.encode(), filename="my_picklist.csv")
        assert r.status_code == 200
        assert r.json()["filename"] == "my_picklist.csv"
