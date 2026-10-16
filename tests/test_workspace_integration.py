"""End-to-end workflow over two real HTTP servers, without sibling repositories."""

import json
import time

import httpx
from fastapi import FastAPI, Header
from test_engine import context, parameters
from test_pressure_service import MemoryMutex, MemoryPlc, wait_for

from bibazu_autotune.api_runtime import LocalApiServer, SessionLease, SessionRequest
from bibazu_autotune.clients import PressureClient, VisionClient
from bibazu_autotune.engine import Engine
from bibazu_autotune.plc import AdsBackend
from bibazu_autotune.pressure_service import PressureService
from bibazu_autotune.profiles import profile_parameters


def synthetic_vision():
    """Camera protocol fixture; capture processing remains the camera app's concern."""
    app, lease, trials = FastAPI(), SessionLease(), {}

    @app.get("/api/v1/capabilities")
    def capabilities():
        return {"api_version": 1}

    @app.post("/api/v1/sessions")
    def acquire(body: SessionRequest):
        return lease.acquire()

    @app.post("/api/v1/sessions/heartbeat")
    def heartbeat(x_session_id: str = Header()):
        return lease.heartbeat(x_session_id)

    @app.delete("/api/v1/sessions/current")
    def release(x_session_id: str = Header()):
        lease.release(x_session_id)
        return {"released": True}

    @app.get("/api/v1/state")
    def state():
        return {"stale_seconds": 0, "capture_config": {"start_mode": "manual"}}

    @app.post("/api/v1/trials")
    def start(body: dict, x_session_id: str = Header()):
        lease.require(x_session_id)
        trials[body["trial_id"]] = {**body, "status": "armed"}
        return trials[body["trial_id"]]

    @app.post("/api/v1/trials/{trial_id}/trigger")
    def trigger(trial_id: str, x_session_id: str = Header()):
        lease.require(x_session_id)
        trials[trial_id].update(
            {
                "status": "completed",
                "stop_reason": "part_left",
                "clip_path": "synthetic/clip",
                "measurements": [{"timestamp": 1.0, "angle_deg": 12.0}],
            }
        )
        return trials[trial_id]

    @app.get("/api/v1/trials/{trial_id}")
    def trial(trial_id: str):
        return trials[trial_id]

    @app.get("/api/v1/measurements")
    def measurements(after: int = 0):
        return {"measurements": [], "latest_cursor": 0, "next_cursor": 0, "overflow": False}

    return app


def test_two_real_apis_and_profile_roundtrip(tmp_path):
    plc = MemoryPlc()
    pressure_service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc), mutex_factory=MemoryMutex
    )
    servers = [LocalApiServer(pressure_service.app, 0), LocalApiServer(synthetic_vision(), 0)]
    clients = []
    engine = None
    try:
        for server in servers:
            server.start()
            wait_for(lambda server=server: server.server.started)
        clients = [
            PressureClient(f"http://127.0.0.1:{servers[0].port}"),
            VisionClient(f"http://127.0.0.1:{servers[1].port}"),
        ]
        events = []
        engine = Engine(*clients, tmp_path / "runs", emit=lambda *event: events.append(event))
        engine.set_context(context())
        engine.connect()

        def until(state):
            def tick():
                engine.tick()
                return engine.state == state

            wait_for(tick)

        until("ready")
        assert engine.baseline == parameters()
        assert plc.writes == []
        for pressure in (2000, 2300):
            candidate = parameters()
            candidate["arrays"][1]["pressure_mbar"] = pressure
            engine.start(candidate)
            until("waiting")
            assert engine.camera_armed
            engine.conveyor(True)

            def motor_done():
                engine.tick()
                return engine.conveyor_operation is None

            wait_for(motor_done)
            engine.trigger()
            until("ready")
            engine.conveyor(False)
            wait_for(motor_done)
        results = [value for kind, value in events if kind == "result"]
        assert len(results) == 2
        assert all(result["status"] == "completed" for result in results)
        assert all(result["result"]["measurements"] for result in results)
        assert results[0]["trial_id"] != results[1]["trial_id"]
        path = tmp_path / "selected.json"
        engine.save(results[0]["parameters"], path)
        until("ready")
        profile = json.loads(path.read_text("utf-8"))
        assert profile_parameters(profile) == results[0]["parameters"]
        assert profile["roadmap_transition"] == context()
        assert profile["ur_ry_angle_deg"] == 18
        assert profile["conveyor_enabled"] is False
        engine.last_heartbeat = time.monotonic() - 6
        engine.tick()
    finally:
        if engine:
            engine.release()
        for client in clients:
            client.close()
        for server in servers:
            server.stop()
        pressure_service.stop()


def test_manual_http_control_without_roadmap_or_camera(tmp_path):
    plc = MemoryPlc()
    service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc), mutex_factory=MemoryMutex
    )
    server = LocalApiServer(service.app, 0)

    def no_camera(request):
        raise httpx.ConnectError("camera unavailable", request=request)

    vision = VisionClient(
        "http://127.0.0.1:8766",
        client=httpx.Client(
            base_url="http://127.0.0.1:8766/api/v1/", transport=httpx.MockTransport(no_camera)
        ),
    )
    pressure = None
    engine = None
    try:
        server.start()
        wait_for(lambda: server.server.started)
        pressure = PressureClient(f"http://127.0.0.1:{server.port}")
        engine = Engine(pressure, vision, tmp_path)
        engine.connect()

        def idle():
            engine.tick()
            return engine.state == "ready" and engine.conveyor_operation is None

        wait_for(idle)
        assert engine.context is None
        assert not engine.vision_connected
        candidate = parameters()
        candidate["arrays"][0]["enabled"] = True
        candidate["arrays"][1]["pressure_mbar"] = 1900
        engine.apply_parameters(candidate)
        wait_for(idle)
        assert pressure.state()["parameters"] == candidate
        engine.conveyor(True)
        wait_for(idle)
        assert pressure.state()["conveyor"]["moving"]
        engine.conveyor(False)
        wait_for(idle)
        assert not pressure.state()["conveyor"]["moving"]
        assert not list(tmp_path.iterdir())
    finally:
        if engine:
            engine.release()
        if pressure:
            pressure.close()
        vision.close()
        server.stop()
        service.stop()
