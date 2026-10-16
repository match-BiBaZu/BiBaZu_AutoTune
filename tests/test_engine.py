import copy
import json

import httpx
import pytest

from bibazu_autotune.clients import ApiFailure, PressureClient, VisionClient
from bibazu_autotune.engine import Engine
from bibazu_autotune.recipes import validate_parameters, validate_recipe


def parameters():
    return {
        "conveyor_speed_mm_per_sec": 80.0,
        "arrays": [
            {
                "index": index,
                "enabled": index == 2,
                "nozzles_enabled": [True, True, True, False, False, False],
                "pressure_mbar": 2300,
                "delay_ms": 0,
                "pulse_duration_ms": 50,
                "offset_mm": 10.0,
                "conveyor_velocity_delay": True,
            }
            for index in range(1, 5)
        ],
    }


def context():
    return {
        "part_name": "Dk1i",
        "title": "Transition 5-0",
        "source_pose_id": 5,
        "target_pose_id": 0,
        "actuation": "free_y",
        "component_edge_ids": [],
    }


class Services:
    def __init__(self):
        self.calls = []
        self.parameters = parameters()
        self.trials = {}
        self.operations = {}
        self.sequence = 0
        self.fail_endpoint = None
        self.operation_failure = False
        self.pending = False
        self.moving = False
        self.fail_host = None

    def respond(self, request):
        if request.url.host == self.fail_host:
            raise httpx.ConnectTimeout("timed out", request=request)
        self.calls.append((request.method, request.url.path, request.content))
        path = request.url.path.split("/api/v1/")[1]
        if self.fail_endpoint and self.fail_endpoint in path:
            return httpx.Response(503, json={"detail": "lost connection"})
        body = json.loads(request.content) if request.content else {}
        if path == "capabilities":
            result = {"api_version": 1}
        elif path == "sessions":
            result = {"session_id": "lease", "snapshot_operation_id": "read"}
            self.operations["read"] = {"status": "succeeded"}
        elif path.startswith("sessions/"):
            result = {"ok": True}
        elif path == "parameters":
            self.parameters = body
            operation_id = str(len(self.operations))
            self.operations[operation_id] = {
                "status": "failed"
                if self.operation_failure
                else "pending"
                if self.pending
                else "succeeded",
                "error": "readback mismatch",
            }
            result = {"operation_id": operation_id}
        elif path.startswith("operations/"):
            result = self.operations[path.split("/")[1]]
        elif path in {"conveyor/start", "conveyor/stop"}:
            self.moving = path.endswith("start")
            operation_id = "motor" + str(len(self.operations))
            self.operations[operation_id] = {"status": "succeeded"}
            result = {"operation_id": operation_id}
        elif path == "state" and request.url.host == "pressure.local":
            result = {
                "parameters": self.parameters,
                "connected": True,
                "conveyor": {
                    "moving": self.moving,
                    "enabled": self.moving,
                    "ready": True,
                    "fault_code": 0,
                },
                "machine_metadata": {"light_barrier_debounce_ms": 20},
            }
        elif path == "state":
            result = {"stale_seconds": 0, "capture_config": {"start_mode": "manual"}}
        elif path == "trials":
            self.trials[body["trial_id"]] = {**body, "status": "armed"}
            result = self.trials[body["trial_id"]]
        elif path.startswith("trials/"):
            trial_id = path.split("/")[1]
            if path.endswith("/abort"):
                self.trials[trial_id]["status"] = "aborted"
            result = self.trials[trial_id]
        elif path == "measurements":
            result = {
                "measurements": [],
                "next_cursor": self.sequence,
                "latest_cursor": self.sequence,
                "overflow": False,
            }
        else:
            raise AssertionError(f"Unexpected endpoint {path}")
        return httpx.Response(200, json=result)

    def clients(self):
        transport = httpx.MockTransport(self.respond)
        pressure = PressureClient(
            "http://127.0.0.1",
            client=httpx.Client(base_url="http://pressure.local/api/v1/", transport=transport),
        )
        vision = VisionClient(
            "http://127.0.0.1",
            client=httpx.Client(base_url="http://vision.local/api/v1/", transport=transport),
        )
        return pressure, vision


def connect(tmp_path):
    services = Services()
    pressure, vision = services.clients()
    events = []
    engine = Engine(pressure, vision, tmp_path, emit=lambda *event: events.append(event))
    engine.set_context(context())
    engine.connect()
    engine.tick()
    assert engine.state == "ready"
    return engine, services, events


def test_two_variants_are_manual_and_result_selection_saves(tmp_path):
    engine, services, events = connect(tmp_path)
    trial_ids = []
    for pressure in (2000, 2300):
        candidate = parameters()
        candidate["arrays"][1]["pressure_mbar"] = pressure
        engine.start(candidate)
        engine.tick()
        assert engine.state == "waiting"
        trial_id = engine.current
        trial_ids.append(trial_id)
        engine.trigger()
        services.trials[trial_id].update(
            status="completed",
            measurements=[{"timestamp": 1.0, "angle_deg": 12.0}],
            clip_path="synthetic/clip",
        )
        engine.tick()
        assert engine.state == "ready"
        for _ in range(3):
            engine.tick()
        assert len(services.trials) == len(trial_ids)
    assert trial_ids[0] != trial_ids[1]
    results = [value for kind, value in events if kind == "result"]
    path = tmp_path / "profile.json"
    engine.save(results[0]["parameters"], path)
    engine.tick()
    assert engine.state == "ready"
    profile = json.loads(path.read_text("utf-8"))
    assert profile["version"] == 13
    assert profile["roadmap_transition"] == context()
    assert services.parameters["arrays"][1]["pressure_mbar"] == 2000
    for trial_id in trial_ids:
        record = json.loads((tmp_path / engine.run_id / f"{trial_id}.json").read_text("utf-8"))
        assert record["result"]["trial_id"] == trial_id
        assert record["run_id"] == engine.run_id
    assert not any("conveyor" in path for _, path, _ in services.calls)


def test_heartbeat_abort_and_release(tmp_path):
    engine, services, events = connect(tmp_path)
    now = [100.0]
    engine.clock = lambda: now[0]
    engine.last_heartbeat = 100.0
    now[0] += 5.1
    engine.tick()
    assert len([p for _, p, _ in services.calls if p.endswith("heartbeat")]) == 2
    engine.start(parameters())
    engine.tick()
    trial_id = engine.current
    engine.abort()
    engine.tick()
    assert engine.state == "ready"
    assert services.trials[trial_id]["status"] == "aborted"
    engine.release()
    assert engine.state == "disconnected"


@pytest.mark.parametrize("endpoint", ["operations", "measurements", "trials", "sessions/heartbeat"])
def test_connection_loss_never_restarts(tmp_path, endpoint):
    engine, services, events = connect(tmp_path)
    engine.start(parameters())
    engine.tick()
    services.fail_endpoint = endpoint
    if endpoint == "operations":
        # End the current capture, then exercise a failed readback operation.
        services.fail_endpoint = None
        engine.abort()
        engine.tick()
        engine.start(parameters())
        services.fail_endpoint = endpoint
    if endpoint == "sessions/heartbeat":
        engine.last_heartbeat = -1000
    with pytest.raises(ApiFailure) as error:
        engine.tick()
    engine.fail(error.value)
    assert engine.state == "disconnected"
    previous = len(services.trials)
    engine.tick()
    assert len(services.trials) == previous


def test_failed_readback_does_not_arm_camera(tmp_path):
    engine, services, events = connect(tmp_path)
    services.operation_failure = True
    engine.start(parameters())
    with pytest.raises(ApiFailure, match="readback"):
        engine.tick()
    assert services.trials == {}


def test_cancel_pending_write_invalidates_session_without_retry(tmp_path):
    engine, services, events = connect(tmp_path)
    services.pending = True
    engine.start(parameters())
    engine.abort()
    assert engine.state == "disconnected"
    assert services.trials == {}
    assert len([path for method, path, _ in services.calls if method == "PUT"]) == 1


def test_disk_error_prevents_parameter_write(tmp_path, monkeypatch):
    engine, services, events = connect(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("bibazu_autotune.engine.atomic_json", fail)
    with pytest.raises(OSError, match="disk full"):
        engine.start(parameters())
    assert not any(method == "PUT" for method, _, _ in services.calls)


def test_recipe_validation_and_transition_identity():
    baseline = parameters()
    assert validate_parameters(baseline) == baseline
    bad = copy.deepcopy(baseline)
    bad["arrays"][0]["pressure_mbar"] = True
    with pytest.raises(ValueError):
        validate_parameters(bad)
    context = {"part_name": "Dk1i", "source_pose_id": 5, "target_pose_id": 0}
    recipe = {
        "schema_version": 1,
        "roadmap_transition": context,
        "variants": [{"name": "a", "parameters": baseline}],
    }
    assert len(validate_recipe(recipe, context)) == 1
    with pytest.raises(ValueError, match="anderen"):
        validate_recipe(recipe, {**context, "target_pose_id": 1})


def test_local_context_is_not_read_from_pressure(tmp_path):
    services = Services()
    engine = Engine(*services.clients(), tmp_path)
    engine.connect()
    engine.tick()
    assert engine.state == "ready"
    assert engine.context is None
    with pytest.raises(ApiFailure, match="auswählen"):
        engine.start(parameters())
    engine.set_context(context())
    original_run = engine.run_id
    run = json.loads((tmp_path / original_run / "run.json").read_text("utf-8"))
    assert run["roadmap_transition"] == context()
    engine.set_context(context())
    assert engine.run_id == original_run
    engine.set_context({**context(), "edge_id": "different-choice"})
    assert engine.run_id != original_run
    engine.start(parameters())
    with pytest.raises(ApiFailure, match="während"):
        engine.set_context(context())


def test_conveyor_manual_control_and_trial_operation_guard(tmp_path):
    engine, services, _ = connect(tmp_path)
    engine.conveyor(True)
    engine.tick()
    assert services.moving
    engine.conveyor(False)
    engine.tick()
    engine.start(parameters())
    with pytest.raises(ApiFailure, match="Bandstart"):
        engine.conveyor(True)
    engine.tick()
    assert engine.camera_armed
    engine.conveyor(True)
    engine.tick()
    assert services.moving
    engine.conveyor(False)
    engine.tick()
    assert not services.moving


def test_manual_arrays_and_belt_need_neither_roadmap_nor_camera(tmp_path):
    services = Services()
    services.fail_host = "vision.local"
    events = []
    engine = Engine(*services.clients(), tmp_path, emit=lambda *event: events.append(event))
    engine.connect()
    engine.tick()
    assert engine.state == "ready"
    assert engine.context is None
    assert not engine.vision_connected
    candidate = parameters()
    candidate["arrays"][0]["enabled"] = True
    candidate["arrays"][1]["nozzles_enabled"][0] = False
    candidate["arrays"][1]["pressure_mbar"] = 1900
    candidate["conveyor_speed_mm_per_sec"] = 90.0
    engine.apply_parameters(candidate)
    assert engine.state == "updating"
    with pytest.raises(ApiFailure, match="Bandstart"):
        engine.conveyor(True)
    engine.tick()
    assert engine.state == "ready"
    assert services.parameters == candidate
    assert ("parameters_applied", candidate) in events
    assert not services.trials
    assert not list(tmp_path.iterdir())
    engine.conveyor(True)
    engine.tick()
    assert services.moving
    engine.conveyor(False)
    engine.tick()
    assert not services.moving


def test_failed_manual_parameter_readback_is_not_reported_as_applied(tmp_path):
    engine, services, events = connect(tmp_path)
    services.operation_failure = True
    engine.apply_parameters(parameters())
    with pytest.raises(ApiFailure, match="readback"):
        engine.tick()
    assert not any(kind == "parameters_applied" for kind, _ in events)


def test_cancel_save_has_no_hardware_or_file_effect(tmp_path):
    engine, services, _ = connect(tmp_path)
    count = len(services.calls)
    engine.save(parameters(), "")
    assert engine.state == "ready"
    assert len(services.calls) == count
    assert not list(tmp_path.glob("*.json"))


def test_save_disk_failure_reports_applied_parameters_without_disconnect(tmp_path, monkeypatch):
    engine, services, events = connect(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("bibazu_autotune.engine.atomic_json", fail)
    candidate = parameters()
    candidate["arrays"][1]["pressure_mbar"] = 1700
    engine.save(candidate, tmp_path / "profile.json")
    engine.tick()
    assert engine.state == "ready"
    assert services.parameters == candidate
    assert not (tmp_path / "profile.json").exists()
    assert any(kind == "error" and "Parameter übernommen" in value for kind, value in events)
    assert not any(kind == "saved" for kind, _ in events)


def test_failed_save_readback_never_creates_profile(tmp_path):
    engine, services, _ = connect(tmp_path)
    services.operation_failure = True
    path = tmp_path / "profile.json"
    engine.save(parameters(), path)
    with pytest.raises(ApiFailure, match="readback"):
        engine.tick()
    assert not path.exists()


def test_camera_timeout_keeps_plc_connected_and_camera_can_be_retried(tmp_path):
    services = Services()
    services.fail_host = "vision.local"
    pressure, vision = services.clients()
    events = []
    engine = Engine(pressure, vision, tmp_path, emit=lambda *event: events.append(event))
    engine.set_context(context())
    engine.connect()
    engine.tick()
    assert engine.state == "ready"
    assert pressure.session_id is not None
    assert engine.baseline == parameters()
    assert not engine.vision_connected
    assert not any(method == "DELETE" for method, _, _ in services.calls)
    assert any(
        kind == "camera_connection"
        and "Kamera-API" in value.get("error", "")
        and "timed out" in value["error"]
        for kind, value in events
    )
    with pytest.raises(ApiFailure, match="Kamera zuerst"):
        engine.start(parameters())
    assert not any(method == "PUT" for method, _, _ in services.calls)
    engine.last_heartbeat = -1000
    engine.tick()
    assert engine.state == "ready"
    services.fail_host = None
    assert engine.connect_camera()
    assert engine.vision_connected
    engine.start(parameters())
    engine.tick()
    assert engine.state == "waiting"


def test_idle_camera_heartbeat_failure_keeps_plc_session(tmp_path):
    engine, services, events = connect(tmp_path)
    services.fail_host = "vision.local"
    engine.last_heartbeat = -1000
    engine.tick()
    assert engine.state == "ready"
    assert engine.pressure.session_id is not None
    assert not engine.vision_connected


def test_plc_timeout_identifies_plc_api(tmp_path):
    services = Services()
    services.fail_host = "pressure.local"
    engine = Engine(*services.clients(), tmp_path)
    with pytest.raises(ApiFailure, match="SPS-API.*timed out"):
        engine.connect()
    assert engine.state == "disconnected"
