"""Synchronous API clients. Call from a worker, never from the Qt UI thread."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx


class ApiFailure(RuntimeError):
    pass


class Client:
    service_name = "API"

    def __init__(self, url, client=None):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("V1 unterstützt ausschließlich lokale HTTP-Adressen.")
        self.http = client or httpx.Client(
            base_url=url.rstrip("/") + "/api/v1/",
            trust_env=False,
            timeout=httpx.Timeout(5.0, connect=1.0),
        )
        self.session_id = None
        self.url = url

    def request(self, method, path, **kwargs):
        headers = {"X-Session-Id": self.session_id} if self.session_id else {}
        try:
            response = self.http.request(method, path, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except ValueError:
                detail = exc.response.text
            raise ApiFailure(
                f"{self.service_name} ({self.url}): {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            detail = (
                "Zeitüberschreitung (timed out)"
                if isinstance(exc, httpx.TimeoutException)
                else str(exc)
            )
            raise ApiFailure(f"{self.service_name} ({self.url}): {detail}") from exc

    def acquire(self):
        capabilities = self.request("GET", "capabilities")
        if capabilities.get("api_version") != 1:
            raise ApiFailure("Nicht unterstützte API-Version")
        result = self.request("POST", "sessions", json={"client": "BiBaZu AutoTune"})
        self.session_id = result["session_id"]
        return result

    def heartbeat(self):
        return self.request("POST", "sessions/heartbeat")

    def release(self):
        if self.session_id:
            try:
                self.request("DELETE", "sessions/current")
            finally:
                self.session_id = None

    def state(self):
        return self.request("GET", "state")

    def close(self):
        self.http.close()


class PressureClient(Client):
    service_name = "SPS-API"

    def apply(self, parameters):
        return self.request("PUT", "parameters", json=parameters)

    def operation(self, operation_id):
        return self.request("GET", f"operations/{operation_id}")

    def start_conveyor(self):
        return self.request("POST", "conveyor/start")

    def stop_conveyor(self):
        return self.request("POST", "conveyor/stop")


class VisionClient(Client):
    service_name = "Kamera-API"

    def start(self, run_id, trial_id, context, timeout_seconds=60.0):
        return self.request(
            "POST",
            "trials",
            json={
                "run_id": run_id,
                "trial_id": trial_id,
                "context": context,
                "timeout_seconds": timeout_seconds,
            },
        )

    def trial(self, trial_id):
        return self.request("GET", f"trials/{trial_id}")

    def trigger(self, trial_id):
        return self.request("POST", f"trials/{trial_id}/trigger")

    def abort(self, trial_id):
        return self.request("POST", f"trials/{trial_id}/abort")

    def measurements(self, after=0):
        return self.request("GET", "measurements", params={"after": after})
