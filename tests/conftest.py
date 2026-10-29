"""Keep GUI startup-default reads/writes isolated from the real user's settings."""

import pytest


@pytest.fixture(autouse=True)
def isolated_model_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bibazu_autotune.model_defaults.default_path", lambda: tmp_path / "autotune_defaults.json"
    )
