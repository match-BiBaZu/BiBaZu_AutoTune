"""Ranking, bounds and dialog integration of read-only GP recommendations."""

from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import QTimer
from test_gui import offline_window as offline_window
from test_model_gui import prepare

from bibazu_autotune.ml.artifacts import FLIP_CLASSES
from bibazu_autotune.ml.features import FEATURES, context_for_candidate, context_from_snapshot
from bibazu_autotune.ml.search import GridRange, grid_axes, score_grid


def test_grid_tie_breaking_limits_and_out_of_range():
    pytest.importorskip("threadpoolctl")

    class Context:
        def analytical_gauge(self, duration):
            return 1500 + duration

        def row(self, pressure, duration, reference):
            return pressure, duration, reference, *([1.0] * (len(FEATURES) - 3))

    context = Context()
    model = SimpleNamespace(
        classes=FLIP_CLASSES,
        feature_bounds=np.array(
            [(500, 1000), (100, 500), (0, 6000), *([(1, 1)] * (len(FEATURES) - 3))]
        ),
        probabilities=lambda features: np.tile([0.1, 0.8, 0.1], (len(features), 1)),
    )
    grid = score_grid(model, context, GridRange(800, 1200, 400), GridRange(100, 110, 10))
    assert [(p.pressure_mbar, p.pulse_duration_ms) for p in grid] == [
        (800, 100),
        (800, 110),
        (1200, 100),
        (1200, 110),
    ]
    assert grid[0].outside_features == ()
    assert grid[-1].outside_features == ("pressure_mbar",)
    with pytest.raises(ValueError, match="10000"):
        grid_axes(GridRange(0, 6000, 1), GridRange(1, 500, 1))
    with pytest.raises(ValueError, match="6000"):
        grid_axes(GridRange(0, 6001, 1), GridRange(1, 1, 1))


def test_actual_autotune_candidate_and_archived_snapshot_share_features(
    offline_window,
    tmp_path,
    monkeypatch,
):
    window, services, errors, wait = offline_window
    settings, model_panel = prepare(window, wait, tmp_path)
    window.editor.speed.setValue(30)
    window.calculate_button.click()
    wait(lambda: settings.model_result is not None)
    candidate = window.autotune_results.selected_candidate()
    assert window.ml_button.isEnabled()
    parameters = window.editor.parameters()
    config = model_panel.configuration()
    live = context_for_candidate(settings.analysis, candidate, config, parameters)
    recorded = window.editor.parameters()
    recorded["arrays"][candidate.mount.array_index - 1]["offset_mm"] = live.offset_mm
    archived = context_from_snapshot(
        {
            "material": asdict(settings.analysis.material),
            "candidate": asdict(candidate),
            "configuration": asdict(config),
        },
        recorded,
        candidate.mount.array_index,
    )
    assert live.row(2000, 70, live.analytical_gauge(70)) == archived.row(
        2000,
        70,
        archived.analytical_gauge(70),
    )

    opened = []

    def close_dialog():
        opened.append(window.ml_dialog.status.text())
        window.ml_dialog.reject()

    monkeypatch.setattr("bibazu_autotune.ml.recommendation.model_directory", lambda: tmp_path)
    QTimer.singleShot(0, close_dialog)
    window.ml_button.click()
    assert opened and "kein trainiertes Modell" in opened[0]
    assert window.editor.parameters() == parameters
    assert not services.calls and not errors
