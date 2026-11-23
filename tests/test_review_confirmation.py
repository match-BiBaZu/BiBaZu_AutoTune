"""Bulk confirmation preserves manual decisions and raw records, across table filters."""

import os
from uuid import uuid4

import pytest
from test_experiments import plan_assets as plan_assets

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QMessageBox

from bibazu_autotune.engine import atomic_json
from bibazu_autotune.experiments.models import BAD_REVIEW_REASON, GOOD_REVIEW_REASON
from bibazu_autotune.experiments.review import ReviewPage
from bibazu_autotune.experiments.storage import CampaignStore, read_json


@pytest.fixture
def review_case(tmp_path, plan_assets, monkeypatch, qt_application):
    plan, assets = plan_assets
    store = CampaignStore.create(tmp_path / "campaigns", plan, assets=assets)
    records = []
    for index in range(5):
        record = {
            "trial_id": uuid4().hex,
            "slot": index,
            "point_index": index // 2,
            "repetition": index % 2 + 1,
            "created_utc": f"2026-11-18T00:00:0{index}",
            "requested_parameters": plan.parameters_at(plan.points()[index // 2]),
            "status": "completed",
            "resolution": "accepted" if index < 4 else "uncertain",
            "evaluation": {
                "quality": "valid" if index < 4 else "review_required",
                "label": "flipped" if index < 4 else None,
                "reasons": [] if index < 4 else ["interrupted_rotation"],
            },
        }
        store.save_trial(record)
        records.append(record)
    store.annotate(records[1]["trial_id"], "not_flipped", False, "Manuell korrigiert")
    store.annotate(records[2]["trial_id"], "flipped", True, "Manuell ausgeschlossen")
    errors, changes = [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    page = ReviewPage()
    page.review_changed.connect(changes.append)
    page.load(store.directory)
    yield page, store, records, errors, changes
    page.close()
    page.deleteLater()
    qt_application.sendPostedEvents(page, QEvent.Type.DeferredDelete)


def test_confirm_automatic_is_campaign_wide_preserves_manual_and_is_idempotent(review_case):
    page, store, records, errors, changes = review_case
    original = {
        r["trial_id"]: (store.trial_directory(r["trial_id"]) / "trial.json").read_bytes()
        for r in records
    }
    manual = {
        r["trial_id"]: (store.trial_directory(r["trial_id"]) / "annotations.json").read_bytes()
        for r in records[1:3]
    }
    page.filter.setCurrentText("Zu prüfen")
    assert len(page.records) == 1
    assert page.confirm_automatic_button.isEnabled()
    page.confirm_automatic_button.click()
    for index in (0, 3):
        assert store.annotation(records[index]["trial_id"])["label"] == "flipped"
        assert not store.annotation(records[index]["trial_id"])["excluded"]
    assert store.annotation(records[4]["trial_id"]) is None
    assert all(
        (store.trial_directory(key) / "annotations.json").read_bytes() == value
        for key, value in manual.items()
    )
    assert all(
        (store.trial_directory(key) / "trial.json").read_bytes() == value
        for key, value in original.items()
    )
    assert changes == [str(store.directory)]
    assert not page.confirm_automatic_button.isEnabled()
    page.confirm_automatic()
    assert changes == [str(store.directory)]
    assert (
        len(
            read_json(store.trial_directory(records[0]["trial_id"]) / "annotations.json")["history"]
        )
        == 1
    )
    assert not errors


def test_bulk_confirmation_reports_partial_failure_and_can_retry(review_case, monkeypatch):
    page, store, records, errors, changes = review_case
    original = page.store.annotate
    failed = records[0]["trial_id"]

    def locked(trial_id, *args):
        if trial_id == failed:
            raise PermissionError("Gesperrt")
        return original(trial_id, *args)

    monkeypatch.setattr(page.store, "annotate", locked)
    page.confirm_automatic_button.click()
    assert store.annotation(failed) is None
    assert store.annotation(records[3]["trial_id"]) is not None
    assert page.confirm_automatic_button.isEnabled()
    assert "Gesperrt" in errors[0]
    assert len(changes) == 1
    monkeypatch.setattr(page.store, "annotate", original)
    page.confirm_automatic_button.click()
    assert store.annotation(failed) is not None
    assert len(changes) == 2
    assert not page.confirm_automatic_button.isEnabled()


def test_repeated_review_clicks_do_not_append_reasons_or_duplicate_history(review_case):
    page, store, records, errors, changes = review_case
    trial_id = records[0]["trial_id"]
    directory = store.trial_directory(trial_id)
    original = (directory / "trial.json").read_bytes()
    path = directory / "annotations.json"
    for label, reason, count in (
        ("flipped", GOOD_REVIEW_REASON, 1),
        (None, BAD_REVIEW_REASON, 2),
        ("overflipped", GOOD_REVIEW_REASON, 3),
    ):
        page.quick_review(trial_id, label)
        assert store.annotation(trial_id)["reason"] == reason
        assert len(read_json(path)["history"]) == count
        saved = path.read_bytes()
        page.quick_review(trial_id, label)
        assert path.read_bytes() == saved
    assert (directory / "trial.json").read_bytes() == original
    assert not errors


def test_review_switch_preserves_custom_note_and_manual_save_is_idempotent(review_case):
    page, store, records, errors, changes = review_case
    trial_id = records[1]["trial_id"]
    page.quick_review(trial_id, None)
    assert store.annotation(trial_id)["reason"] == "Manuell korrigiert; " + BAD_REVIEW_REASON
    page.quick_review(trial_id, "not_flipped")
    assert store.annotation(trial_id)["reason"] == "Manuell korrigiert; " + GOOD_REVIEW_REASON
    path = store.trial_directory(trial_id) / "annotations.json"
    original = path.read_bytes()
    page.annotate()
    page.annotate()
    assert path.read_bytes() == original
    assert not errors


def test_legacy_accumulated_reasons_are_displayed_once_without_rewriting_history(review_case):
    page, store, records, errors, changes = review_case
    trial_id = records[0]["trial_id"]
    path = store.trial_directory(trial_id) / "annotations.json"
    legacy = {
        "label": "flipped",
        "excluded": False,
        "created_utc": "2026-11-18",
        "reason": "; ".join(
            ["Eigene Notiz", GOOD_REVIEW_REASON, BAD_REVIEW_REASON, GOOD_REVIEW_REASON]
        ),
    }
    atomic_json(path, {"schema_version": 1, "history": [legacy]})
    original = path.read_bytes()
    page.refresh()
    assert page.table.item(0, 7).text() == "Eigene Notiz; " + GOOD_REVIEW_REASON
    assert path.read_bytes() == original
    page.quick_review(trial_id, "flipped")
    history = read_json(path)["history"]
    assert history[0] == legacy
    assert history[-1]["reason"] == "Eigene Notiz; " + GOOD_REVIEW_REASON
    assert len(history) == 2
    saved = path.read_bytes()
    page.quick_review(trial_id, "flipped")
    assert path.read_bytes() == saved
    assert not errors
