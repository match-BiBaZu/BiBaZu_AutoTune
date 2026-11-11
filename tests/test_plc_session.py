import copy

import pytest
from test_engine import Services, parameters

from bibazu_autotune.clients import ApiFailure
from bibazu_autotune.plc_session import PlcSession


def connected_session():
    services = Services()
    pressure, unused = services.clients()
    unused.close()
    now, events = [0.0], []
    session = PlcSession(pressure, lambda *e: events.append(e), clock=lambda: now[0])
    session.connect()
    session.tick()
    return session, services, now, events


def test_connection_is_read_only_and_heartbeats_use_only_sps():
    session, services, now, events = connected_session()
    try:
        assert session.state == "ready"
        assert (
            next(value for name, value in events if name == "connected")["parameters"]
            == parameters()
        )
        assert not any(method == "PUT" for method, _, _ in services.calls)
        now[0] = 0.6
        session.tick()
        assert any(path.endswith("sessions/heartbeat") for _, path, _ in services.calls)
        assert not any("trials" in path or "measurements" in path for _, path, _ in services.calls)
        assert not services.trials
    finally:
        session.release()
        session.pressure.close()


def test_updates_and_band_controls_stay_independent():
    session, services, _, events = connected_session()
    try:
        session.conveyor(True)
        session.tick()
        assert services.moving
        changed = copy.deepcopy(parameters())
        changed["arrays"][0]["pressure_mbar"] = 1700
        session.apply_parameters(changed)
        assert session.state == "updating"
        session.tick()
        assert session.state == "ready" and services.moving
        assert ("parameters_applied", changed) in events
        session.conveyor(False)
        session.tick()
        assert not services.moving
    finally:
        session.release()
        session.pressure.close()


def test_pending_write_times_out_without_retry_and_release_cancels_readback():
    session, services, now, events = connected_session()
    try:
        services.pending = True
        session.apply_parameters(parameters())
        session.tick()
        now[0] = 16
        with pytest.raises(ApiFailure, match="keine automatische Wiederholung"):
            session.tick()
        session.release()
        session.tick()
        assert session.state == "disconnected"
        assert not any(name == "parameters_applied" for name, _ in events)
        assert sum(method == "PUT" for method, _, _ in services.calls) == 1
    finally:
        session.release()
        session.pressure.close()
