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

    def respond(self, request):
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
        elif path == "profile/save-dialog":
            self.operations["save"] = {"status": "succeeded"}
            result = {"operation_id": "save"}
        elif path == "state" and request.url.host == "pressure.local":
            result = {
                "parameters": self.parameters,
                "roadmap_transition": {
                    "part_name": "Dk1i",
                    "title": "Transition 5-0",
                    "source_pose_id": 5,
                    "target_pose_id": 0,
                    "actuation": "free_y",
                    "component_edge_ids": [],
                },
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
    engine.save(results[0]["parameters"])
    engine.tick()
    assert engine.state == "save_dialog"
    engine.tick()
    assert engine.state == "ready"
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
