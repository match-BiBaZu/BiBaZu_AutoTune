"""Loopback HTTP hosting and expiring sessions (ported from BiBaZu Pressure Control)."""

import secrets
import socket
import threading
import time

import uvicorn
from pydantic import BaseModel, ConfigDict


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SessionRequest(StrictModel):
    client: str = "external"


class Health(BaseModel):
    api_version: int = 1
    connected: bool


class SessionResponse(BaseModel):
    session_id: str
    expires_in: float
    snapshot_operation_id: str | None = None


class SessionLease:
    """Thread-safe, expiring ownership; queued commands recheck at execution."""

    def __init__(self, ttl=15.0, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.lock = threading.RLock()
        self.token = None
        self.deadline = 0.0

    def active(self):
        with self.lock:
            return self.token is not None and self.clock() < self.deadline

    def acquire(self):
        with self.lock:
            if self.active():
                raise ApiError(409, "Eine externe Sitzung ist bereits aktiv.")
            self.token = secrets.token_urlsafe(32)
            self.deadline = self.clock() + self.ttl
            return {"session_id": self.token, "expires_in": self.ttl}

    def require(self, token):
        with self.lock:
            if not self.active() or not secrets.compare_digest(token or "", self.token):
                raise ApiError(409, "Sitzung fehlt oder ist abgelaufen.")

    def heartbeat(self, token):
        with self.lock:
            self.require(token)
            self.deadline = self.clock() + self.ttl
            return {"expires_in": self.ttl}

    def release(self, token):
        with self.lock:
            self.require(token)
            self.token = None

    def invalidate(self):
        with self.lock:
            self.token = None


class LocalApiServer:
    """One ASGI server thread, prebound loopback socket, no reload or processes."""

    def __init__(self, app, port):
        self.app, self.port = app, port
        self.server = None
        self.thread = None
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            self.sock.bind(("127.0.0.1", self.port))
            self.sock.listen(128)
            self.port = self.sock.getsockname()[1]
        except OSError:
            self.sock.close()
            raise
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                access_log=False,
                lifespan="on",
                timeout_graceful_shutdown=1,
            )
        )
        self.thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.sock]},
            daemon=True,
            name=f"local-api-{self.port}",
        )
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(2)
        if self.sock:
            self.sock.close()
