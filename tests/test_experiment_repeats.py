"""Finite repeat scheduling independent of camera/clip IO timings."""

import time
from dataclasses import replace

import pytest
from test_experiments import plan_assets as plan_assets

from bibazu_autotune.experiments.demo import DemoPressure, DemoVision
from bibazu_autotune.experiments.runner import ExperimentRunner
from bibazu_autotune.experiments.storage import read_json


@pytest.fixture
def schedule(tmp_path, plan_assets, monkeypatch):
    plan, assets = plan_assets
    pressure = DemoPressure()
    vision = DemoVision(pressure)
    monkeypatch.setattr(vision, "trial", lambda key: {"status": "completed"})
    runner = ExperimentRunner(pressure, vision)
    runner.connect()
    runner.tick()
    decisions = {"uncertain": {0, 3}, "retries_valid": True}

    def until(state):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            runner.tick()
            if runner.state == state:
                return
            time.sleep(0.001)
        raise AssertionError(f"Expected {state}, got {runner.state}")

    def install_store():
        def finish(record, result):
            uncertain = record["slot"] in decisions["uncertain"] and (
                not record["replaces_trial_id"] or not decisions["retries_valid"]
            )
            record = dict(
                record,
                status="completed",
                resolution=None,
                evaluation={
                    "quality": "review_required" if uncertain else "valid",
                    "label": None if uncertain else "flipped",
                    "rotation_deg": None,
                    "reasons": ["test_gap"] if uncertain else [],
                },
            )
            runner.store.save_trial(record)
            return record

        monkeypatch.setattr(runner.store, "finish", finish)

    def create(timing):
        runner.create(
            tmp_path / timing,
            replace(plan, settling_seconds=0, uncertain_repeat_timing=timing),
            assets,
            {},
        )
        install_store()
        until("gate")

    yield runner, decisions, create, install_store, until
    runner.close()
    vision.close()


def test_series_end_retries_only_uncertain_slots_after_all_regular_repetitions(schedule):
    runner, decisions, create, install, until = schedule
    create("series_end")
    runner.confirm()
    until("review_pending")
    original = runner.store.trials()
    assert len(original) == 2 and runner.slot == 2
    assert len(runner.store.pending_repeats()) == 1
    runner.repeat_uncertain()
    until("gate")
    records = runner.store.trials()
    assert len(records) == 3 and runner.slot == 2
    assert records[-1]["replaces_trial_id"] == original[0]["trial_id"]
    assert records[-1]["repetition"] == 1
    assert not runner.store.pending_repeats()


def test_campaign_end_batches_survive_resume_and_confirm_each_parameter_change(schedule):
    runner, decisions, create, install, until = schedule
    create("campaign_end")
    for point in range(4):
        runner.confirm()
        until("review_pending" if point == 3 else "gate")
        assert len(runner.store.trials()) == (point + 1) * 2
    directory = runner.store.directory
    originals = runner.store.pending_repeats()
    runner.repeat_uncertain()
    until("gate")
    assert runner.repeat_active and len(runner.store.trials()) == 9
    assert read_json(directory / "progress.json")["scheduler"]["repeat_active"]
    runner.release()
    runner.connect()
    runner.tick()
    runner.resume(directory)
    install()
    until("gate")
    assert runner.repeat_active and runner.slot == 3
    assert len(runner.store.trials()) == 9
    runner.confirm()
    until("complete")
    records = runner.store.trials()
    assert len(records) == 10
    assert {r["replaces_trial_id"] for r in records[8:]} == {r["trial_id"] for r in originals}
    assert runner.slot == 8 and not runner.store.pending_repeats()


def test_repeated_uncertainty_is_offered_again_without_infinite_automatic_retries(schedule):
    runner, decisions, create, install, until = schedule
    decisions["retries_valid"] = False
    create("series_end")
    runner.confirm()
    until("review_pending")
    runner.repeat_uncertain()
    until("review_pending")
    assert len(runner.store.trials()) == 3
    for _ in range(10):
        runner.tick()
    assert len(runner.store.trials()) == 3
    runner.defer_uncertain()
    assert runner.state == "gate"
    for point in range(1, 4):
        runner.confirm()
        until("review_pending" if point in (1, 3) else "gate")
        if point == 1:
            runner.defer_uncertain()
    assert len(runner.store.pending_repeats()) == 2
    runner.defer_uncertain()
    assert runner.state == "complete"
    assert len(runner.pending_repeat_slots) == 2


@pytest.mark.parametrize(
    "label,excluded,resolved",
    [("not_flipped", False, True), (None, True, True), (None, False, False)],
)
def test_manual_decision_updates_pending_series_without_changing_raw_result(
    schedule, label, excluded, resolved
):
    runner, decisions, create, install, until = schedule
    create("series_end")
    runner.confirm()
    until("review_pending")
    record = runner.store.pending_repeats()[0]
    path = runner.store.trial_directory(record["trial_id"]) / "trial.json"
    original = path.read_bytes()
    runner.store.annotate(record["trial_id"], label, excluded, "Geprüft")
    runner.refresh_reviews(runner.store.directory)
    assert len(runner.pending_repeat_slots) == (0 if resolved else 1)
    assert runner.state == ("gate" if resolved else "review_pending")
    assert len(runner.store.trials()) == 2
    assert path.read_bytes() == original


def test_repeat_button_rechecks_saved_reviews_even_without_gui_notification(schedule):
    runner, decisions, create, install, until = schedule
    create("series_end")
    runner.confirm()
    until("review_pending")
    record = runner.store.pending_repeats()[0]
    runner.store.annotate(record["trial_id"], "not_flipped", False, "Gut")
    assert runner.pending_repeat_slots  # Old displayed count is intentionally stale.
    runner.repeat_uncertain()
    assert runner.state == "gate"
    assert not runner.pending_repeat_slots
    assert len(runner.store.trials()) == 2


def finish_campaign(runner, until):
    for point in range(4):
        runner.confirm()
        until("review_pending" if point == 3 else "gate")


def test_reviewed_campaign_stays_complete_after_resume(schedule):
    runner, decisions, create, install, until = schedule
    create("campaign_end")
    finish_campaign(runner, until)
    directory = runner.store.directory
    first, second = runner.store.pending_repeats()
    runner.store.annotate(first["trial_id"], "not_flipped", False, "Gut")
    runner.store.annotate(second["trial_id"], None, True, "Schlecht")
    runner.refresh_reviews(directory)
    assert runner.state == "complete" and not runner.pending_repeat_slots
    runner.release()
    runner.connect()
    runner.tick()
    runner.resume(directory)
    install()
    until("complete")
    assert not runner.repeat_queue and not runner.pending_repeat_slots
    assert len(runner.store.trials()) == 8


def test_review_of_queued_retry_does_not_interrupt_current_attempt(schedule):
    runner, decisions, create, install, until = schedule
    create("campaign_end")
    finish_campaign(runner, until)
    queued = runner.store.pending_repeats()[1]
    runner.repeat_uncertain()
    active = runner.record["trial_id"]
    runner.store.annotate(queued["trial_id"], None, True, "Schlecht")
    runner.refresh_reviews(runner.store.directory)
    assert runner.record["trial_id"] == active
    assert len(runner.repeat_queue) == 2  # The active head remains until normal completion.
    until("complete")
    assert len(runner.store.trials()) == 9
    assert not runner.store.pending_repeats()


def test_confirm_drops_reviewed_queued_task_without_starting_next_combination(schedule):
    runner, decisions, create, install, until = schedule
    decisions["uncertain"] = {0, 3, 5}
    create("campaign_end")
    finish_campaign(runner, until)
    runner.repeat_uncertain()
    until("gate")
    assert runner.slot == 3
    queued = runner.repeat_queue[0]
    runner.store.annotate(queued["trial_id"], "flipped", False, "Gut")
    runner.confirm()
    assert runner.state == "gate" and runner.slot == 5
    assert len(runner.store.trials()) == 9
    runner.confirm()  # Explicit new confirmation for the changed pressure/duration.
    until("complete")
    assert len(runner.store.trials()) == 10


def test_resume_prunes_a_persisted_retry_queue_after_review(schedule):
    runner, decisions, create, install, until = schedule
    create("campaign_end")
    finish_campaign(runner, until)
    runner.repeat_uncertain()
    until("gate")
    directory = runner.store.directory
    remaining = runner.repeat_queue[0]
    runner.release()
    runner.store.annotate(remaining["trial_id"], None, True, "Schlecht")
    runner.connect()
    runner.tick()
    runner.resume(directory)
    install()
    until("complete")
    assert not runner.repeat_queue and not runner.repeat_active
    assert len(runner.store.trials()) == 9


def test_review_of_old_attempt_does_not_hide_a_new_uncertain_retry(schedule):
    runner, decisions, create, install, until = schedule
    decisions["retries_valid"] = False
    create("series_end")
    runner.confirm()
    until("review_pending")
    original = runner.store.pending_repeats()[0]
    runner.repeat_uncertain()
    until("review_pending")
    latest = runner.store.pending_repeats()[0]
    assert latest["trial_id"] != original["trial_id"]
    runner.store.annotate(original["trial_id"], "flipped", False, "Alten Versuch geprüft")
    runner.refresh_reviews(runner.store.directory)
    assert runner.state == "review_pending"
    assert runner.pending_repeat_slots == {latest["slot"]: latest["trial_id"]}
    runner.store.annotate(latest["trial_id"], None, True, "Neuen Versuch ausgeschlossen")
    runner.refresh_reviews(runner.store.directory)
    assert runner.state == "gate" and not runner.pending_repeat_slots


def test_pruning_reviewed_queue_keeps_new_uncertainty_visible(schedule):
    runner, decisions, create, install, until = schedule
    decisions["retries_valid"] = False
    create("campaign_end")
    finish_campaign(runner, until)
    runner.repeat_uncertain()
    until("gate")
    remaining = runner.repeat_queue[0]
    runner.store.annotate(remaining["trial_id"], None, True, "Schlecht")
    runner.refresh_reviews(runner.store.directory)
    assert runner.state == "review_pending"
    assert not runner.repeat_active and not runner.repeat_queue
    assert len(runner.pending_repeat_slots) == 1
