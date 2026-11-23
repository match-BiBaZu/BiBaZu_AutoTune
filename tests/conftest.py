"""Share the Qt application and isolate GUI startup settings from the user's defaults."""

import pytest

# A failed test can retain Qt widgets in its traceback past fixture teardown.
_qt_application = None


@pytest.fixture(scope="session")
def qt_application():
    from PyQt6.QtWidgets import QApplication

    global _qt_application
    _qt_application = QApplication.instance() or QApplication([])
    return _qt_application


@pytest.fixture(autouse=True)
def isolated_model_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bibazu_autotune.model_defaults.default_path", lambda: tmp_path / "autotune_defaults.json"
    )
