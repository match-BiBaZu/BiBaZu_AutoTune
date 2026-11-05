import time

from fastapi.testclient import TestClient
from test_engine import parameters
from test_pressure_service import MemoryMutex, MemoryPlc, acquire, operation, wait_for
from test_pressure_test import ClockedPulsePlc

from bibazu_autotune.plc import AdsBackend
from bibazu_autotune.pressure_service import PressureService

REQUEST = {"request_id": "a" * 32}


def test_missing_plc_feature_blocks_check_without_writing(tmp_path):
    plc = MemoryPlc()
    service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc),
        mutex_factory=MemoryMutex,
        pressure_test_dir=tmp_path,
    )
    with TestClient(service.app) as client:
        headers = acquire(client)
        assert client.get("/api/v1/capabilities").json()["pressure_test"] is True
        result = operation(client, client.post("/api/v1/pressure-test/check", headers=headers))
        assert result["status"] == "failed"
        assert "Kurzimpuls" in result["error"]
        assert not plc.writes


def test_running_pressure_test_rejects_other_writes_and_abort_has_priority(tmp_path):
    plc = MemoryPlc()
    pulse = ClockedPulsePlc(time.monotonic)
    service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc),
        mutex_factory=MemoryMutex,
        pressure_test_dir=tmp_path,
        pressure_test_adapter=pulse,
    )
    with TestClient(service.app) as client:
        headers = acquire(client)
        checked = operation(client, client.post("/api/v1/pressure-test/check", headers=headers))
        assert checked["status"] == "succeeded"
        started = operation(
            client, client.post("/api/v1/pressure-test/start", json=REQUEST, headers=headers)
        )
        assert started["status"] == "succeeded"
        assert (
            client.put("/api/v1/parameters", json=parameters(), headers=headers).status_code == 409
        )
        assert client.post("/api/v1/conveyor/start", headers=headers).status_code == 409
        assert (
            client.post("/api/v1/pressure-test/start", json=REQUEST, headers=headers).status_code
            == 409
        )
        aborted = operation(
            client, client.post("/api/v1/pressure-test/abort", json=REQUEST, headers=headers)
        )
        assert aborted["status"] == "succeeded"
        wait_for(lambda: not service.pressure_test.active)
        assert service.pressure_test.snapshot()["status"] == "aborted"
        assert pulse.stopped and not pulse.fired
        assert not any("MAIN.OpenValve5" in write for write in plc.writes)


def test_expired_session_cancels_series_before_next_pulse(tmp_path):
    plc = MemoryPlc()
    pulse = ClockedPulsePlc(time.monotonic)
    service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc),
        mutex_factory=MemoryMutex,
        pressure_test_dir=tmp_path,
        pressure_test_adapter=pulse,
    )
    with TestClient(service.app) as client:
        headers = acquire(client)
        operation(client, client.post("/api/v1/pressure-test/start", json=REQUEST, headers=headers))
        service.lease.invalidate()
        service.expire()
        wait_for(lambda: not service.pressure_test.active)
        assert pulse.stopped and not pulse.fired
        assert service.pressure_test.snapshot()["status"] in {"aborted", "failed"}


def test_abort_received_before_start_prevents_delayed_request(tmp_path):
    plc = MemoryPlc()
    pulse = ClockedPulsePlc(time.monotonic)
    service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc),
        mutex_factory=MemoryMutex,
        pressure_test_dir=tmp_path,
        pressure_test_adapter=pulse,
    )
    with TestClient(service.app) as client:
        headers = acquire(client)
        operation(client, client.post("/api/v1/pressure-test/abort", json=REQUEST, headers=headers))
        assert (
            client.post("/api/v1/pressure-test/start", json=REQUEST, headers=headers).status_code
            == 409
        )
        assert not pulse.fired and not pulse.subscribed
