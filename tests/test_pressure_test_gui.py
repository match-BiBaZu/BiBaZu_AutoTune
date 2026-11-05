import copy
import time

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from test_pressure_test import events_for

from bibazu_autotune import pressure_test_app
from bibazu_autotune.pressure_test import PressureRecording, pressure_matrix


class GuiPressureApi:
    def __init__(self):
        self.calls = []
        self.state = {
            "status": "idle",
            "phase": "idle",
            "active": False,
            "completed": 0,
            "total": 90,
            "current": {},
            "results": [],
            "last_rows": [],
            "directory": None,
        }

    def client(self, url):
        api = self

        class Client:
            session_id = None

            def acquire(self):
                self.session_id = "token"
                api.calls.append("connect")
                return {"snapshot_operation_id": "read"}

            def check_pressure_test(self):
                api.calls.append("check")
                return {"operation_id": "check"}

            def operation(self, operation):
                return {"status": "succeeded"}

            def start_pressure_test(self, request_id):
                api.calls.append("start")
                api.state.update(
                    status="running",
                    phase="settling",
                    active=True,
                    current={"pulse_ms": 20, "pressure_mbar": 1000, "repetition": 1},
                )
                return {"operation_id": "start"}

            def abort_pressure_test(self, request_id):
                api.calls.append("abort")
                api.state.update(status="aborted", phase="aborted", active=False)
                return {"operation_id": "abort"}

            def heartbeat(self):
                pass

            def state(self):
                return {"pressure_test": copy.deepcopy(api.state)}

            def release(self):
                self.session_id = None
                api.calls.append("release")

            def close(self):
                pass

        return Client()


@pytest.fixture
def pressure_window(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    api = GuiPressureApi()
    monkeypatch.setattr(pressure_test_app, "PressureClient", api.client)
    window = pressure_test_app.PressureTestWindow("http://127.0.0.1:9999")
    window.show()

    def wait(predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            qt.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(window.status.text())

    yield window, api, wait
    window.worker.stopping.set()
    wait(lambda: not window.worker.isRunning())
    if window.abort_worker:
        wait(lambda: not window.abort_worker.isRunning())
    window.close()
    qt.processEvents()


def test_no_shot_on_open_or_connect_and_abort_is_responsive(pressure_window):
    window, api, wait = pressure_window
    QTest.qWait(150)
    assert not api.calls
    assert not window.start_button.isEnabled()
    window.connect_button.click()
    wait(lambda: window.start_button.isEnabled())
    assert "start" not in api.calls
    window.start_button.click()
    wait(lambda: api.state["active"])
    assert window.abort_button.isEnabled()
    window.abort_button.click()
    wait(lambda: "abort" in api.calls and not window.busy)
    assert api.calls.count("start") == 1
    assert window.worker.cancel.is_set()


def test_close_during_series_sends_abort_before_thread_exit(pressure_window):
    window, api, wait = pressure_window
    window.connect_button.click()
    wait(lambda: window.start_button.isEnabled())
    window.start_button.click()
    wait(lambda: api.state["active"])
    window.close()
    wait(lambda: not window.worker.isRunning())
    assert "abort" in api.calls
    assert window.closing


def test_stored_trace_selection_and_incomplete_values_render(pressure_window, tmp_path):
    window, _, _ = pressure_window
    pulse = pressure_matrix()[0]
    recording = PressureRecording(tmp_path, "test", {})
    result, rows = recording.add(pulse, events_for(pulse))
    window.handle_event(
        "state",
        {
            "status": "completed",
            "phase": "completed",
            "active": False,
            "completed": 1,
            "current": pulse.__dict__,
            "directory": str(recording.path),
            "results": [result],
            "last_rows": rows,
        },
    )
    assert window.table.item(0, 4).text() == "0.6000"
    assert window.curve.rows
    window.curve.rows[20]["bar2"] = None
    assert not window.grab().isNull()
