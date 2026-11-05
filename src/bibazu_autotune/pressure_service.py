"""Headless, serialized PLC API. No GUI or sibling repository imports."""

from __future__ import annotations

import argparse
import copy
import itertools
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .api_models import Parameters, PressureTestRequest
from .api_runtime import ApiError, LocalApiServer, SessionLease, SessionRequest
from .plc import AdsBackend, PlcConfig
from .plc_control_lease import PlcControlLease
from .pressure_test_runner import PressureTestRunner


class PressureService:
    def __init__(
        self,
        backend=None,
        *,
        lease=None,
        mutex_factory=PlcControlLease.acquire,
        pressure_test_dir=None,
        pressure_test_adapter=None,
    ):
        self.backend = backend or AdsBackend()
        self.lease = lease or SessionLease()
        self.mutex_factory = mutex_factory
        self.mutex = None
        self.lock = threading.RLock()
        self.commands = queue.PriorityQueue()
        self.sequence = itertools.count()
        self.operations = {}
        self.pending = set()
        self.cancel_start = threading.Event()
        self.closed = threading.Event()
        self.thread = None
        self.watchdog = None
        self.connected = False
        self.last_error = None
        self.snapshot = {}
        self.metadata = {}
        self.owned = False
        self.cancel_pressure_test = threading.Event()
        self.cancelled_pressure_requests = set()
        self.pressure_test = PressureTestRunner(
            self.backend,
            pressure_test_dir or Path(__file__).resolve().parents[2] / "data/pressure_tests",
            self.cancel_pressure_test,
            self.pressure_test_guard,
            adapter=pressure_test_adapter,
        )
        self.app = self.create_app()

    def start(self):
        with self.lock:
            if self.thread:
                return
            self.mutex = self.mutex_factory()
            if self.mutex is None:
                raise RuntimeError("Eine andere Anwendung besitzt die SPS. Pressure GUI schließen.")
            self.thread = threading.Thread(target=self.run, name="autotune-plc", daemon=True)
            self.watchdog = threading.Thread(target=self.watch, name="autotune-lease", daemon=True)
            self.thread.start()
            self.watchdog.start()

    def stop(self):
        with self.lock:
            if not self.thread or self.closed.is_set():
                return
            self.closed.set()
            self.lease.invalidate()
            self.cancel_start.set()
            self.cancel_pressure_test.set()
            self.enqueue("shutdown", None, priority=-2)
        self.thread.join(5)
        self.watchdog.join(1)

    def watch(self):
        while not self.closed.wait(0.1):
            self.expire()

    def expire(self):
        with self.lock:
            if self.owned and not self.lease.active():
                self.owned = False
                self.cancel_start.set()
                self.cancel_pressure_test.set()
                if self.pressure_test.active:
                    self.enqueue("pressure_test_abort", None, priority=-1)
                self.enqueue("conveyor_stop", None, priority=-1)

    def state(self):
        with self.lock:
            return copy.deepcopy(
                {
                    "api_version": 1,
                    "service_kind": "autotune-plc",
                    "connected": self.connected,
                    "session_active": self.lease.active(),
                    "parameters": self.snapshot.get("parameters"),
                    "conveyor": self.snapshot.get("conveyor"),
                    "machine_metadata": self.metadata,
                    "error": self.last_error,
                    "pressure_test": self.pressure_test.snapshot(),
                }
            )

    def acquire(self, client):
        with self.lock:
            self.expire()
            if self.pending:
                raise ApiError(409, "Eine Operation läuft noch.")
            result = self.lease.acquire()
            self.owned = True
            operation = self.submit(result["session_id"], "read")
            result["snapshot_operation_id"] = operation["operation_id"]
            return result

    def enqueue(self, kind, token, parameters=None, priority=0):
        operation_id = uuid.uuid4().hex
        operation = {"operation_id": operation_id, "kind": kind, "status": "pending"}
        self.operations[operation_id] = operation
        self.pending.add(operation_id)
        # Completed history is bounded; pending entries are never evicted.
        for key in list(self.operations):
            if len(self.operations) <= 256:
                break
            if key not in self.pending:
                del self.operations[key]
        self.commands.put((priority, next(self.sequence), operation_id, kind, token, parameters))
        return dict(operation)

    def submit(self, token, kind, parameters=None):
        with self.lock:
            if self.closed.is_set():
                raise ApiError(503, "Dienst wird beendet.")
            self.lease.require(token)
            if kind == "pressure_test_abort":
                if parameters:
                    self.cancelled_pressure_requests.add((token, parameters))
                if not parameters or parameters == getattr(self.pressure_test, "request_id", None):
                    self.cancel_pressure_test.set()
            elif self.pressure_test.active:
                if kind != "conveyor_stop":
                    raise ApiError(409, "Drucktest läuft; zuerst abbrechen.")
                self.cancel_pressure_test.set()
            if kind == "conveyor_stop":
                self.cancel_start.set()
            elif kind != "pressure_test_abort" and self.pending:
                raise ApiError(409, "Eine Operation läuft bereits.")
            elif kind == "conveyor_start":
                self.cancel_start.clear()
            elif kind == "pressure_test_start":
                if (token, parameters) in self.cancelled_pressure_requests:
                    raise ApiError(409, "Diese Drucktestanforderung wurde bereits abgebrochen.")
                self.cancel_pressure_test.clear()
            return self.enqueue(
                kind,
                token,
                parameters,
                priority=-1 if kind in {"conveyor_stop", "pressure_test_abort"} else 0,
            )

    def pressure_test_guard(self, token):
        self.guard(token)
        if self.cancel_pressure_test.is_set():
            raise RuntimeError("Drucktest abgebrochen; keine weiteren Stöße.")

    def guard(self, token, starting=False):
        self.lease.require(token)
        if self.closed.is_set() or (starting and self.cancel_start.is_set()):
            raise RuntimeError("Operation abgebrochen")

    def refresh(self):
        snapshot = self.backend.snapshot()
        with self.lock:
            self.snapshot = snapshot
            self.connected = True
            self.last_error = None
        return snapshot

    def run(self):
        try:
            while True:
                try:
                    _, _, operation_id, kind, token, parameters = self.commands.get(
                        timeout=0.02 if self.pressure_test.active else 0.25
                    )
                except queue.Empty:
                    if self.pressure_test.active:
                        self.pressure_test.tick()
                    elif self.connected:
                        try:
                            self.refresh()
                        except Exception as exc:
                            self.connection_failed(exc)
                    continue
                if kind == "shutdown":
                    self.pressure_test.abort("Testfenster wird geschlossen.")
                    deadline = time.monotonic() + 3.5
                    while self.pressure_test.active and time.monotonic() < deadline:
                        self.pressure_test.tick()
                        time.sleep(0.02)
                    if self.pressure_test.active:
                        self.pressure_test.complete("failed", "SPS-Schließen nicht bestätigt.")
                    break
                result = {"status": "failed"}
                try:
                    # Internal expiry stops have no token; every user command rechecks its lease.
                    if token is not None and kind != "pressure_test_abort":
                        self.guard(token, kind == "conveyor_start")
                    if kind == "read":
                        self.backend.connect()
                        snapshot = self.refresh()
                        metadata = self.backend.machine_metadata()
                        with self.lock:
                            self.metadata = metadata
                    elif kind == "parameters":
                        snapshot = self.backend.apply(
                            parameters, lambda token=token: self.guard(token)
                        )
                    elif kind == "pressure_test_check":
                        result["pressure_test_info"] = self.pressure_test.check()
                        snapshot = self.snapshot
                    elif kind == "pressure_test_start":
                        if (token, parameters) in self.cancelled_pressure_requests:
                            raise RuntimeError(
                                "Drucktestanforderung wurde vor dem Start abgebrochen."
                            )
                        self.pressure_test.start(token, parameters)
                        snapshot = self.snapshot
                    elif kind == "pressure_test_abort":
                        if not parameters or parameters == getattr(
                            self.pressure_test, "request_id", None
                        ):
                            self.pressure_test.abort()
                        snapshot = self.snapshot
                    elif kind == "conveyor_start":
                        snapshot = self.backend.start_conveyor(
                            lambda token=token: self.guard(token, True)
                        )
                    else:
                        snapshot = self.backend.stop_conveyor()
                    with self.lock:
                        self.snapshot = snapshot
                        self.connected = True
                    result.update(status="succeeded", snapshot=snapshot)
                except Exception as exc:
                    result["error"] = str(exc)
                    # Read actual state after partial writes, without retrying the write.
                    try:
                        result["snapshot"] = self.refresh()
                    except Exception:
                        self.connection_failed(exc)
                with self.lock:
                    self.operations[operation_id].update(result)
                    self.pending.discard(operation_id)
                self.pressure_test.tick()
        finally:
            try:
                if self.connected:
                    self.backend.stop_conveyor()
            except Exception:
                pass
            try:
                self.backend.close()
            finally:
                self.mutex.close()

    def connection_failed(self, exc):
        with self.lock:
            self.connected = False
            self.last_error = str(exc)
            self.lease.invalidate()
            self.cancel_start.set()
        try:
            self.backend.close()
        except Exception:
            pass
        self.expire()

    def create_app(self):
        @asynccontextmanager
        async def lifespan(app):
            self.start()
            try:
                yield
            finally:
                self.stop()

        app = FastAPI(title="BiBaZu AutoTune PLC API", version="1.0.0", lifespan=lifespan)

        @app.middleware("http")
        async def local_clients_only(request: Request, call_next):
            host = request.headers.get("host", "").split(":")[0]
            if host not in {"127.0.0.1", "localhost", "testserver"} or (
                request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin")
            ):
                return JSONResponse(
                    {"detail": "Nur lokale native Clients erlaubt."}, status_code=403
                )
            return await call_next(request)

        @app.exception_handler(ApiError)
        async def api_error(request, exc):
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)

        @app.get("/api/v1/health")
        def health():
            state = self.state()
            return {key: state[key] for key in ("api_version", "service_kind", "connected")}

        @app.get("/api/v1/capabilities")
        def capabilities():
            return {
                "api_version": 1,
                "service_kind": "autotune-plc",
                "array_count": 4,
                "nozzles_per_array": 6,
                "conveyor_start": True,
                "parameters_schema": Parameters.model_json_schema(),
                "pressure_test": True,
            }

        app.get("/api/v1/state")(self.state)

        @app.post("/api/v1/sessions", status_code=201)
        def acquire(body: SessionRequest):
            return self.acquire(body.client)

        @app.post("/api/v1/sessions/heartbeat")
        def heartbeat(x_session_id: str = Header()):
            return self.lease.heartbeat(x_session_id)

        @app.delete("/api/v1/sessions/current")
        def release(x_session_id: str = Header()):
            with self.lock:
                self.lease.release(x_session_id)
                self.expire()
            return {"released": True}

        @app.put("/api/v1/parameters", status_code=202)
        def parameters(body: Parameters, x_session_id: str = Header()):
            return self.submit(x_session_id, "parameters", body.model_dump())

        @app.get("/api/v1/operations/{operation_id}")
        def operation(operation_id: str):
            with self.lock:
                if operation_id not in self.operations:
                    raise HTTPException(404, "Operation nicht gefunden.")
                return copy.deepcopy(self.operations[operation_id])

        @app.post("/api/v1/conveyor/start", status_code=202)
        def start(x_session_id: str = Header()):
            return self.submit(x_session_id, "conveyor_start")

        @app.post("/api/v1/conveyor/stop", status_code=202)
        def stop(x_session_id: str = Header()):
            return self.submit(x_session_id, "conveyor_stop")

        @app.post("/api/v1/pressure-test/check", status_code=202)
        def pressure_check(x_session_id: str = Header()):
            return self.submit(x_session_id, "pressure_test_check")

        @app.post("/api/v1/pressure-test/start", status_code=202)
        def pressure_test_start(body: PressureTestRequest, x_session_id: str = Header()):
            return self.submit(x_session_id, "pressure_test_start", body.request_id)

        @app.post("/api/v1/pressure-test/abort", status_code=202)
        def pressure_test_abort(body: PressureTestRequest, x_session_id: str = Header()):
            return self.submit(x_session_id, "pressure_test_abort", body.request_id)

        return app


def add_plc_arguments(parser):
    defaults = PlcConfig()
    parser.add_argument("--plc-ip", default=defaults.ip)
    parser.add_argument("--ams-net-id", default=defaults.ams_net_id)
    parser.add_argument("--ads-port", type=int, default=defaults.port)


def configured_service(args):
    return PressureService(
        AdsBackend(PlcConfig(args.plc_ip, args.ams_net_id, args.ads_port)),
        pressure_test_dir=getattr(args, "pressure_test_dir", None),
        lease=SessionLease(ttl=2) if getattr(args, "pressure_test_mode", False) else None,
    )


def start_bundled(args):
    service = configured_service(args)
    # Acquire the process mutex synchronously so ownership errors reach the GUI.
    service.start()
    server = LocalApiServer(service.app, 0)
    try:
        server.start()
        deadline = time.monotonic() + 5
        while not server.server.started:
            if time.monotonic() > deadline or not server.thread.is_alive():
                raise RuntimeError("PLC API konnte nicht starten.")
            time.sleep(0.01)
    except Exception:
        server.stop()
        service.stop()
        raise
    return service, server


def main():
    parser = argparse.ArgumentParser(description="BiBaZu AutoTune · headless PLC API")
    add_plc_arguments(parser)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    service = configured_service(args)
    try:
        service.start()
        uvicorn.run(service.app, host="127.0.0.1", port=args.port)
    finally:
        service.stop()


if __name__ == "__main__":
    main()
