"""Optional sibling-repo integration over real TCP, with synthetic hardware.

Run with the vision repo's development Python and AutoTune's src on PYTHONPATH.
Standalone AutoTune checkouts skip this test when the sibling projects are absent.
"""

# ruff: noqa: E402
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WORKSPACE = Path(__file__).resolve().parents[2]
PRESSURE = WORKSPACE / "BiBaZu_Big_Boi/CSVSaver"
VISION = WORKSPACE / "USBaZu_Baumer_Kamera_image_processing"
if not PRESSURE.is_dir() or not VISION.is_dir():
    pytest.skip("sibling repositories are not available", allow_module_level=True)
pytest.importorskip("numpy")
pytest.importorskip("pyads")
pytest.importorskip("cv2")
sys.path[:0] = [str(PRESSURE), str(VISION / "src"), str(VISION / "tests")]

import numpy as np
import PressureControlGUI as gui
from bibazu_autotune.clients import PressureClient, VisionClient
from bibazu_autotune.engine import Engine
from bibazu_vision import worker as worker_module
from bibazu_vision.api import VisionApi
from bibazu_vision.camera import CameraConfig
from bibazu_vision.processing import ProcessingConfig
from bibazu_vision.recording import CaptureConfig
from local_api_runtime import LocalApiServer
from pressure_api import PressureApi
from PyQt6.QtWidgets import QApplication, QFileDialog, QWidget
from roadmap_transition_dialog import selection_from_roadmap_transition_metadata
from test_pressure_api import MemoryPlc
from test_worker import FakeAcquisitionCamera


def test_two_real_apis_and_profile_roundtrip(tmp_path, monkeypatch):
    qt = QApplication.instance() or QApplication([])
    camera = FakeAcquisitionCamera(CameraConfig(mode="live"))
    monkeypatch.setattr(worker_module, "BaumerCamera", lambda _: camera)
    with patch.object(gui.AdsController, "start"):
        pressure_window = gui.PressureControlWindow()
    plc = MemoryPlc()
    pressure_window.ads.worker.client.plc = plc
    pressure_window.ads.connected = True
    pressure_window.selected_roadmap_transition = selection_from_roadmap_transition_metadata(
        {
            "roadmap_path": str(tmp_path / "Dk1i_roadmap.json"),
            "part_name": "Dk1i",
            "edge_id": "a20:5->0:free_y",
            "source_pose_id": 5,
            "target_pose_id": 0,
            "actuation": "free_y",
            "signed_angle_deg": -89.9,
        }
    )
    pressure_window.profile_directory = tmp_path
    pressure_api = PressureApi(pressure_window)
    pressure_window.ads.thread.started.disconnect(pressure_window.ads.worker.start)
    pressure_window.ads.thread.start()
    camera_worker = worker_module.PipelineWorker(
        CameraConfig(mode="live"),
        ProcessingConfig(threshold=100, min_area=20),
        CaptureConfig(
            start_mode="manual",
            pre_roll_seconds=0,
            quiet_seconds=0.03,
            wait_seconds=2,
            max_seconds=3,
        ),
        tmp_path / "camera",
    )
    vision_window = QWidget()
    vision_window.worker = camera_worker
    vision_api = VisionApi(vision_window)
    servers = [LocalApiServer(pressure_api.app, 0), LocalApiServer(vision_api.app, 0)]
    for server in servers:
        server.start()
    pool = ThreadPoolExecutor(1)
    camera_worker.start()

    def wait(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qt.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        raise AssertionError("condition timed out")

    def run(callback):
        future = pool.submit(callback)
        wait(future.done, 8)
        return future.result()

    camera.items.put((np.full((64, 96), 185, np.uint8), False))
    wait(lambda: camera_worker.snapshot() is not None)
    clients = [
        PressureClient(f"http://127.0.0.1:{servers[0].port}"),
        VisionClient(f"http://127.0.0.1:{servers[1].port}"),
    ]
    events = []
    engine = Engine(*clients, tmp_path / "runs", emit=lambda *event: events.append(event))
    try:
        wait(lambda: all(server.server.started for server in servers))
        run(engine.connect)
        run(engine.tick)
        assert engine.state == "ready"
        assert engine.context["part_name"] == "Dk1i"
        for pressure in (2000, 2300):
            candidate = engine.baseline.copy()
            candidate["arrays"] = [dict(array) for array in engine.baseline["arrays"]]
            for array in candidate["arrays"]:
                array["pressure_mbar"] = pressure
                array["pulse_duration_ms"] = 50
            run(lambda candidate=candidate: engine.start(candidate))
            run(engine.tick)
            wait(lambda: camera_worker.api_capture.trials[engine.current]["status"] == "armed")
            run(engine.trigger)
            camera.send_frame(x=20)
            camera.send_frame(x=25)
            camera.items.put((np.full((64, 96), 185, np.uint8), False))
            time.sleep(0.06)
            camera.items.put((np.full((64, 96), 185, np.uint8), False))
            wait(lambda: not camera_worker.api_capture.busy())
            run(engine.tick)
            assert engine.state == "ready"
        results = [value for kind, value in events if kind == "result"]
        assert len(results) == 2
        assert all(result["status"] == "completed" for result in results)
        assert all(result["result"]["measurements"] for result in results)
        assert results[0]["trial_id"] != results[1]["trial_id"]
        path = tmp_path / "selected.json"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: (str(path), ""))
        run(lambda: engine.save(results[0]["parameters"]))
        run(engine.tick)
        run(engine.tick)
        wait(path.exists)
        profile = json.loads(path.read_text("utf-8"))
        assert profile["version"] == 13
        assert profile["arrays"][1]["pressure_mbar"] == 2000
        assert profile["roadmap_transition"]["edge_id"] == "a20:5->0:free_y"
        assert all("MAIN.GuiConveyorEnabled" not in write for write in plc.writes)
        assert pressure_window.conveyor_enabled.isEnabled()
    finally:
        run(engine.release)
        for client in clients:
            client.close()
        for server in servers:
            server.stop()
        camera_worker.stop()
        assert camera_worker.wait(3000)
        pressure_api.timer.stop()
        pressure_window.ads.thread.quit()
        assert pressure_window.ads.thread.wait(2000)
        pressure_window.ads.worker.client.plc = None
        pressure_window.ur_angle.shutdown()
        pool.shutdown()
        pressure_window.deleteLater()
        vision_window.deleteLater()
        qt.processEvents()
