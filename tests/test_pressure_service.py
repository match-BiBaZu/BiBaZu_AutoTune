"""Real HTTP/ADS-adapter tests with memory symbols; never connect to hardware."""

import copy
import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_engine import parameters

from bibazu_autotune.api_runtime import SessionLease
from bibazu_autotune.plc import AdsBackend, array_symbols
from bibazu_autotune.pressure_service import PressureService


class MemoryMutex:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class MemoryPlc:
    def __init__(self):
        self.values = {
            "MAIN.GuiConveyorSpeedMmPerSec": 80.0,
            "MAIN.ConveyorServoVelocityOnly": True,
            "MAIN.ConveyorServoCommissioned": True,
            "MAIN.ConveyorServoAccelerationRpmPerSec": 50.0,
            "MAIN.StepperPosReadyToExecute": True,
        }
        for array in parameters()["arrays"]:
            for field, name in array_symbols(array["index"]).items():
                self.values[name] = (
                    array["nozzles_enabled"][int(field[7:]) - 1]
                    if field.startswith("nozzle_")
                    else array[field]
                )
        self.writes = []
        self.threads = set()
        self.partial = False
        self.mismatch = False
        self.block_reset = False
        self.reset_seen = threading.Event()
        self.offline = False
        self.close_count = 0

    def set_timeout(self, timeout):
        pass

    def open(self):
        pass

    def close(self):
        self.close_count += 1

    def read_by_name(self, name, *_):
        if self.offline:
            raise RuntimeError("ADS offline")
        self.threads.add(threading.get_ident())
        return self.values.get(name, 0)

    def read_list_by_name(self, names, **kwargs):
        return {name: self.read_by_name(name) for name in names}

    def write_list_by_name(self, values, **kwargs):
        self.threads.add(threading.get_ident())
        self.writes.append(dict(values))
        for name, value in values.items():
            if not self.mismatch or "PressureMbar" not in name:
                self.values[name] = value
        if "MAIN.GuiConveyorEnabled" in values:
            moving = values["MAIN.GuiConveyorEnabled"]
            self.values.update(
                {
                    "MAIN.StepperPosBusy": moving,
                    "MAIN.ConveyorServoState": 30 if moving else 0,
                    "MAIN.ConveyorServo1TargetVelocity": 100 if moving else 0,
                    "MAIN.ConveyorServo2TargetVelocity": 100 if moving else 0,
                }
            )
        if values.get("MAIN.GuiConveyorReset"):
            self.reset_seen.set()
            if self.block_reset:
                self.values["MAIN.ConveyorServoState"] = 10
                self.values["MAIN.StepperPosReadyToExecute"] = False
        return {name: "write failed" if self.partial else "no error" for name in values}


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.005)
    raise AssertionError("condition timed out")


def operation(client, response):
    assert response.status_code in {201, 202}, response.text
    body = response.json()
    operation_id = body.get("snapshot_operation_id") or body["operation_id"]

    def done():
        result = client.get(f"/api/v1/operations/{operation_id}").json()
        return result if result["status"] != "pending" else None

    return wait_for(done)


@pytest.fixture
def hardware():
    plc, mutex = MemoryPlc(), MemoryMutex()
    service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc), mutex_factory=lambda: mutex
    )
    with TestClient(service.app) as client:
        yield service, plc, client
    assert not service.thread.is_alive()
    assert mutex.closed


def acquire(client):
    response = client.post("/api/v1/sessions", json={"client": "test"})
    result = operation(client, response)
    assert result["status"] == "succeeded", result
    return {"X-Session-Id": response.json()["session_id"]}


def test_read_only_connect_write_readback_and_no_transition_required(hardware):
    service, plc, client = hardware
    headers = acquire(client)
    assert plc.writes == []
    assert client.get("/api/v1/state").json()["parameters"] == parameters()
    candidate = copy.deepcopy(parameters())
    candidate["arrays"][1]["pressure_mbar"] = 2100
    result = operation(client, client.put("/api/v1/parameters", json=candidate, headers=headers))
    assert result["status"] == "succeeded"
    assert result["snapshot"]["parameters"] == candidate
    assert all("MAIN.GuiConveyorEnabled" not in write for write in plc.writes)
    assert len(plc.threads) == 1
    assert client.post("/api/v1/profile/save-dialog", headers=headers).status_code == 404
    assert client.post("/api/v1/sessions", json={}).status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        {"pressure_mbar": 6001},
        {"pulse_duration_ms": 0},
        {"delay_ms": -1},
        {"offset_mm": 5001.0},
        {"pressure_mbar": True},
        {"nozzles_enabled": [True]},
        {"index": 2},
        {"extra": 1},
    ],
)
def test_invalid_input_never_writes(hardware, change):
    _, plc, client = hardware
    headers = acquire(client)
    candidate = parameters()
    candidate["arrays"][0].update(change)
    assert client.put("/api/v1/parameters", json=candidate, headers=headers).status_code == 422
    assert plc.writes == []


@pytest.mark.parametrize("mode", ["moving", "calibration", "partial", "mismatch", "owner"])
def test_write_failures_are_reported_without_retry(hardware, mode):
    _, plc, client = hardware
    headers = acquire(client)
    plc.values["MAIN.GuiConveyorEnabled"] = mode == "moving"
    plc.values["MAIN.GuiConveyorCalibrationMode"] = mode == "calibration"
    plc.values["MAIN.GuiReorientationControlActive"] = mode == "owner"
    plc.partial, plc.mismatch = mode == "partial", mode == "mismatch"
    candidate = parameters()
    candidate["arrays"][0]["pressure_mbar"] = 1500
    result = operation(client, client.put("/api/v1/parameters", json=candidate, headers=headers))
    assert result["status"] == "failed"
    assert result["error"]
    assert len(plc.writes) == (1 if mode in {"partial", "mismatch"} else 0)


def test_conveyor_start_and_stop_sequence(hardware):
    _, plc, client = hardware
    headers = acquire(client)
    result = operation(client, client.post("/api/v1/conveyor/start", headers=headers))
    assert result["status"] == "succeeded", result
    assert result["snapshot"]["conveyor"]["moving"]
    assert any(write.get("MAIN.GuiConveyorReset") for write in plc.writes)
    assert (
        operation(client, client.post("/api/v1/conveyor/stop", headers=headers))["status"]
        == "succeeded"
    )
    assert not client.get("/api/v1/state").json()["conveyor"]["moving"]


def test_stop_cancels_start_without_late_enable(hardware):
    _, plc, client = hardware
    headers = acquire(client)
    plc.block_reset = True
    start = client.post("/api/v1/conveyor/start", headers=headers)
    assert plc.reset_seen.wait(2)
    stop = client.post("/api/v1/conveyor/stop", headers=headers)
    assert operation(client, stop)["status"] == "succeeded"
    assert operation(client, start)["status"] == "failed"
    assert not any(write.get("MAIN.GuiConveyorEnabled") for write in plc.writes)


def test_expiry_invalidates_queued_write_and_stops(hardware):
    service, plc, client = hardware
    headers = acquire(client)
    plc.block_reset = True
    start = client.post("/api/v1/conveyor/start", headers=headers)
    assert plc.reset_seen.wait(2)
    now = [100.0]
    service.lease.clock = lambda: now[0]
    service.lease.deadline = 101
    now[0] = 102
    service.expire()
    assert operation(client, start)["status"] == "failed"
    wait_for(lambda: not service.pending)
    assert not any(write.get("MAIN.GuiConveyorEnabled") for write in plc.writes)
    assert any(write.get("MAIN.GuiConveyorEnabled") is False for write in plc.writes)
    assert client.put("/api/v1/parameters", json=parameters(), headers=headers).status_code == 409


def test_expired_queued_parameters_do_not_write(hardware):
    service, plc, client = hardware
    headers = acquire(client)
    entered, proceed = threading.Event(), threading.Event()
    original = service.backend.apply

    def blocked(parameters, guard):
        entered.set()
        assert proceed.wait(2)
        return original(parameters, guard)

    service.backend.apply = blocked
    response = client.put("/api/v1/parameters", json=parameters(), headers=headers)
    assert entered.wait(2)
    service.lease.invalidate()
    proceed.set()
    assert operation(client, response)["status"] == "failed"
    assert not any("MAIN.GuiPressureMbar1" in write for write in plc.writes)


def test_origin_rebinding_offline_and_reconnect(hardware):
    service, plc, client = hardware
    headers = acquire(client)
    assert (
        client.post(
            "/api/v1/conveyor/start", headers={**headers, "Origin": "https://example.org"}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/state", headers={"Host": "example.org"}).status_code == 403
    plc.offline = True
    wait_for(lambda: not service.connected)
    assert client.post("/api/v1/sessions/heartbeat", headers=headers).status_code == 409
    plc.offline = False
    wait_for(lambda: not service.pending)
    acquire(client)


def test_process_ownership_conflict():
    service = PressureService(mutex_factory=lambda: None)
    with pytest.raises(RuntimeError, match="besitzt die SPS"):
        service.start()


@pytest.mark.parametrize("ending", ["expiry", "release", "shutdown"])
def test_session_or_service_end_stops_running_conveyor(hardware, ending):
    service, plc, client = hardware
    headers = acquire(client)
    result = operation(client, client.post("/api/v1/conveyor/start", headers=headers))
    assert result["status"] == "succeeded"
    assert plc.values["MAIN.GuiConveyorEnabled"]
    if ending == "expiry":
        service.lease.invalidate()
        service.expire()
    elif ending == "release":
        assert client.delete("/api/v1/sessions/current", headers=headers).status_code == 200
    else:
        service.stop()
    wait_for(lambda: not plc.values["MAIN.GuiConveyorEnabled"])
    assert not any(write.get("MAIN.GuiConveyorEnabled") for write in plc.writes[-1:])


def test_session_clock_is_injectable():
    now = [10]
    lease = SessionLease(clock=lambda: now[0])
    lease.acquire()
    assert lease.active()
    now[0] += 16
    assert not lease.active()
