import argparse
import subprocess
import sys
from importlib.metadata import distribution

import httpx
from test_pressure_service import MemoryMutex, MemoryPlc

from bibazu_autotune.plc import AdsBackend
from bibazu_autotune.pressure_service import PressureService, start_bundled


def test_autotune_starts_without_importing_or_requiring_training(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; import bibazu_autotune.app; "
            "assert not any(m.startswith('bibazu_autotune_training') for m in sys.modules); "
            "assert not any(m.startswith('bibazu_autotune.experiments') for m in sys.modules)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    package = distribution("bibazu-autotune")
    assert not any(
        "bibazu-autotune-training" in requirement for requirement in package.requires or []
    )
    assert {point.name for point in package.entry_points} == {
        "bibazu-autotune",
        "bibazu-autotune-api",
    }


def test_headless_entrypoint_imports_no_gui_or_sibling_code(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; import bibazu_autotune.pressure_service; "
            "assert 'PressureControlGUI' not in sys.modules; "
            "assert not any(m.startswith('PyQt6') for m in sys.modules)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "bibazu_autotune.pressure_service",
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--ams-net-id" in result.stdout
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "bibazu_autotune",
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--pressure-url" in result.stdout


def test_bundled_startup_uses_free_port_and_makes_no_plc_writes(monkeypatch):
    plc, mutex = MemoryPlc(), MemoryMutex()
    service = PressureService(
        AdsBackend(connection_factory=lambda *args: plc), mutex_factory=lambda: mutex
    )
    monkeypatch.setattr("bibazu_autotune.pressure_service.configured_service", lambda _: service)
    running, server = start_bundled(argparse.Namespace())
    try:
        assert running is service
        assert server.port > 0
        with httpx.Client(trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{server.port}/api/v1/health")
        assert response.json()["service_kind"] == "autotune-plc"
        assert response.json()["connected"] is False
        assert not plc.writes
    finally:
        server.stop()
        service.stop()
    assert not service.thread.is_alive()
    assert mutex.closed
