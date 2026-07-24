from datetime import datetime, timedelta, timezone
import json

import pytest

from app.model_training import (
    CandidateSpecification,
    EvaluationObservation,
    build_calibration_artifact,
    chronological_folds,
    event_block_beta_bootstrap,
    fit_beta_calibration,
    load_observations_jsonl,
    score_predictions,
    select_candidate,
    write_artifact,
)
from app.calibration import BetaCoefficients, CalibrationArtifact


UTC = timezone.utc
START = datetime(2025, 1, 1, tzinfo=UTC)


def observation(index: int, *, event_id: str | None = None, outcome: float | None = None):
    baseline = 0.1 + 0.8 * ((index % 20) / 19)
    # Synthetic labels deliberately follow a less-extreme probability so beta
    # calibration has a stable, testable signal without claiming sports edge.
    truth_probability = 0.2 + 0.6 * ((index % 20) / 19)
    label = float(outcome if outcome is not None else (index % 100) / 100 < truth_probability)
    return EvaluationObservation(
        event_id=event_id or f"event-{index}",
        observed_at=START + timedelta(days=index),
        sport="basketball",
        league="nba",
        market="moneyline",
        outcome=label,
        candidate_probabilities={
            "equal_family_logit": baseline,
            "sharp_source": min(0.99, max(0.01, baseline + 0.12)),
        },
        executable_cost=0.5,
        execution_cost_error=0.001 * ((index % 5) - 2),
    )


def test_folds_use_label_availability_not_just_observation_time():
    selection_through = START + timedelta(days=10)
    calibration_through = START + timedelta(days=20)
    validation_through = START + timedelta(days=30)

    def _obs(event_id, observed_day, *, label_available_at=None):
        return EvaluationObservation(
            event_id=event_id, observed_at=START + timedelta(days=observed_day),
            sport="basketball", league="nba", market="moneyline", outcome=1.0,
            candidate_probabilities={"c": 0.6}, executable_cost=0.5,
            execution_cost_error=0.0, label_available_at=label_available_at,
        )

    # One filler per fold so each is non-empty, plus a prediction observed before
    # the selection cutoff whose label only settles inside the calibration window.
    rows = [
        _obs("sel", 3),
        _obs("cal", 13),
        _obs("val", 25),
        _obs("test", 35),
        _obs("late", 5, label_available_at=START + timedelta(days=15)),
    ]
    folds = chronological_folds(
        rows, model_selection_through=selection_through,
        calibration_through=calibration_through, validation_through=validation_through)

    selection_ids = {row.event_id for row in folds.selection}
    calibration_ids = {row.event_id for row in folds.calibration}
    # The late label is NOT usable at the selection origin, so it is excluded from
    # selection and only appears once its label is available (calibration window).
    assert "late" not in selection_ids
    assert "late" in calibration_ids
    assert selection_ids == {"sel"}


def test_multiplicity_report_flags_a_skilled_candidate_over_the_benchmark():
    from app.model_training import multiplicity_report

    rows = []
    for index in range(40):
        label = float(index % 2 == 0)
        rows.append(EvaluationObservation(
            event_id=f"m-{index}", observed_at=START + timedelta(days=index),
            sport="basketball", league="nba", market="moneyline", outcome=label,
            candidate_probabilities={
                "benchmark": 0.5,                        # uninformative reference
                "skilled": 0.85 if label == 1.0 else 0.15,  # tracks the outcome
                "coinflip": 0.5,                          # same as benchmark
            },
            executable_cost=0.5, execution_cost_error=0.0,
        ))
    report = multiplicity_report(rows, candidates=["skilled", "coinflip"],
                                 benchmark="benchmark", draws=400, seed=0)
    assert report["candidates_searched"] == 2
    assert report["reality_check_pvalue"] < 0.05
    assert report["romano_wolf_pvalues"]["skilled"] < 0.05
    assert (report["romano_wolf_pvalues"]["coinflip"]
            > report["romano_wolf_pvalues"]["skilled"])


def test_chronological_folds_are_event_grouped_and_future_safe():
    rows = [observation(i) for i in range(20)]
    folds = chronological_folds(
        rows,
        model_selection_through=START + timedelta(days=4),
        calibration_through=START + timedelta(days=9),
        validation_through=START + timedelta(days=14),
    )
    assert max(row.observed_at for row in folds.selection) < min(
        row.observed_at for row in folds.calibration
    )
    assert max(row.observed_at for row in folds.calibration) < min(
        row.observed_at for row in folds.validation
    )
    assert max(row.observed_at for row in folds.validation) < min(
        row.observed_at for row in folds.test
    )

    crossing = rows + [observation(6, event_id="event-4")]
    with pytest.raises(ValueError, match="crosses a chronological boundary"):
        chronological_folds(
            crossing,
            model_selection_through=START + timedelta(days=4),
            calibration_through=START + timedelta(days=9),
            validation_through=START + timedelta(days=14),
        )


def test_candidate_selection_uses_only_the_supplied_earlier_fold():
    early = [observation(i) for i in range(100)]
    selected, metrics = select_candidate(early)
    assert selected == min(metrics, key=lambda name: metrics[name]["log_loss"])
    assert set(metrics) == {"equal_family_logit", "sharp_source"}
    assert all(metric["sample_size"] == 100 for metric in metrics.values())


def test_beta_fit_is_monotone_and_improves_synthetic_miscalibration():
    rows = [observation(i) for i in range(800)]
    raw = [row.candidate_probabilities["equal_family_logit"] for row in rows]
    labels = [row.outcome for row in rows]
    coefficients = fit_beta_calibration(raw, labels)
    calibrated = [coefficients.calibrate(value) for value in raw]
    assert coefficients.a >= 0 and coefficients.b >= 0
    assert score_predictions(calibrated, labels)["log_loss"] < score_predictions(raw, labels)[
        "log_loss"
    ]


def test_event_block_bootstrap_is_deterministic_and_keeps_event_groups_together():
    rows = [observation(i // 2, event_id=f"event-{i // 2}") for i in range(600)]
    first = event_block_beta_bootstrap(
        rows, candidate="equal_family_logit", draws=200, seed=17
    )
    second = event_block_beta_bootstrap(
        rows, candidate="equal_family_logit", draws=200, seed=17
    )
    assert first == second
    assert len(first) == 200
    assert all(draw.a >= 0 and draw.b >= 0 for draw in first)


def test_artifact_builder_preserves_nested_test_period_and_requires_review(
    tmp_path, monkeypatch
):
    def row(index, when):
        cycle = index % 8
        probability = .25 if cycle < 4 else .75
        outcome = float(cycle == 0 or cycle in {5, 6, 7})
        return EvaluationObservation(
            event_id=f"{when.date()}-{index}",
            observed_at=when + timedelta(seconds=index),
            sport="basketball", league="nba", market="moneyline",
            outcome=outcome,
            candidate_probabilities={"simple": probability},
            executable_cost=.5, execution_cost_error=(index % 3 - 1) * .001,
        )

    rows = [row(i, START) for i in range(1000)]
    rows += [row(i, START + timedelta(days=1)) for i in range(1000)]
    rows += [row(i, START + timedelta(days=2)) for i in range(1000)]
    rows += [row(i, START + timedelta(days=3)) for i in range(1000)]
    monkeypatch.setattr(
        "app.model_training.event_block_pipeline_uncertainty",
        lambda *args, **kwargs: tuple({
            "pipeline": "simple",
            "devig_method": "proportional",
            "consensus_method": "equal_family_logit",
            "sharp_source_family": None,
            "consensus_intercept": 0.0,
            "family_coefficients": {},
            "missing_family_coefficients": {},
            "beta_coefficients": BetaCoefficients(1, 1, 0).as_list(),
            "execution_cost_offset": 0.0,
        } for _ in range(200)),
    )
    payload = build_calibration_artifact(
        rows,
        specifications={"simple": CandidateSpecification(
            devig_method="proportional", consensus_method="equal_family_logit"
        )},
        model_selection_through=START + timedelta(hours=1),
        calibration_through=START + timedelta(days=1, hours=1),
        validation_through=START + timedelta(days=2, hours=1),
        model_version="consensus-test",
        sport="basketball", league="nba", market="moneyline",
        bootstrap_draws=200,
    )
    assert payload["statistical_claim_supported"] is False
    assert payload["sample_size"] == 1000
    assert payload["segments"][0]["selected_pipeline"] == "simple"
    artifact_path = tmp_path / "fitted-v2.json"
    write_artifact(payload, artifact_path)
    loaded = CalibrationArtifact.load(artifact_path)
    assert loaded.eligible_for_action is True
    assert loaded.validation_through < loaded.evaluated_from
    policy = loaded.policy_for("basketball", "nba", "moneyline")
    assert policy is not None and len(policy.uncertainty_draws) == 200
    assert policy.uncertainty_draws[0]["pipeline"] == "simple"


# --- Phase 0.1: JSONL delayed-label leakage protection -----------------------


def _obs_payload(index, *, observed_day, feature_day=None, label_day=None,
                 prob=0.6, outcome=1.0):
    payload = {
        "event_id": f"event-{index}",
        "observed_at": (START + timedelta(days=observed_day)).isoformat(),
        "sport": "basketball", "league": "nba", "market": "moneyline",
        "outcome": outcome,
        "candidate_probabilities": {"c": prob},
        "executable_cost": 0.5, "execution_cost_error": 0.0,
    }
    if feature_day is not None:
        payload["feature_available_at"] = (START + timedelta(days=feature_day)).isoformat()
    if label_day is not None:
        payload["label_available_at"] = (START + timedelta(days=label_day)).isoformat()
    return payload


def _write_jsonl(tmp_path, payloads):
    path = tmp_path / "obs.jsonl"
    path.write_text("\n".join(json.dumps(p) for p in payloads) + "\n", encoding="utf-8")
    return path


def test_jsonl_requires_availability_metadata_by_default(tmp_path):
    # The exact leakage defect: a JSONL row without availability metadata must
    # NOT be silently accepted (it previously defaulted to observed_at).
    path = _write_jsonl(tmp_path, [_obs_payload(0, observed_day=0)])
    with pytest.raises(ValueError, match="JSONL line 1"):
        load_observations_jsonl(path)


def test_jsonl_partial_availability_is_rejected(tmp_path):
    path = _write_jsonl(tmp_path, [_obs_payload(0, observed_day=0, feature_day=0)])
    with pytest.raises(ValueError, match="JSONL line 1"):
        load_observations_jsonl(path)


def test_jsonl_impossible_label_before_observation_is_rejected(tmp_path):
    path = _write_jsonl(
        tmp_path, [_obs_payload(0, observed_day=5, feature_day=5, label_day=3)]
    )
    with pytest.raises(ValueError, match="JSONL line 1"):
        load_observations_jsonl(path)


def test_jsonl_naive_timestamp_is_rejected(tmp_path):
    payload = _obs_payload(0, observed_day=0, feature_day=0, label_day=1)
    payload["label_available_at"] = "2025-01-05T00:00:00"  # no timezone offset
    path = _write_jsonl(tmp_path, [payload])
    with pytest.raises(ValueError, match="JSONL line 1"):
        load_observations_jsonl(path)


def test_jsonl_rejects_unsupported_schema_version(tmp_path):
    payload = _obs_payload(0, observed_day=0, feature_day=0, label_day=1)
    payload["observation_schema_version"] = 99
    path = _write_jsonl(tmp_path, [payload])
    with pytest.raises(ValueError, match="JSONL line 1"):
        load_observations_jsonl(path)


def test_jsonl_legacy_flag_defaults_availability_to_observed_at(tmp_path):
    path = _write_jsonl(tmp_path, [_obs_payload(0, observed_day=7)])
    [row] = load_observations_jsonl(path, allow_legacy_availability=True)
    assert row.feature_available_at == row.observed_at
    assert row.label_available_at == row.observed_at


def test_jsonl_and_direct_object_paths_produce_identical_observations(tmp_path):
    payloads = [
        _obs_payload(0, observed_day=1, feature_day=1, label_day=3),
        _obs_payload(1, observed_day=2, feature_day=2, label_day=9),
    ]
    from_jsonl = load_observations_jsonl(_write_jsonl(tmp_path, payloads))
    direct = [
        EvaluationObservation(
            event_id=p["event_id"],
            observed_at=datetime.fromisoformat(p["observed_at"]),
            sport=p["sport"], league=p["league"], market=p["market"],
            outcome=p["outcome"],
            candidate_probabilities=p["candidate_probabilities"],
            executable_cost=p["executable_cost"],
            execution_cost_error=p["execution_cost_error"],
            feature_available_at=datetime.fromisoformat(p["feature_available_at"]),
            label_available_at=datetime.fromisoformat(p["label_available_at"]),
        )
        for p in payloads
    ]
    assert from_jsonl == direct


def test_jsonl_late_label_is_excluded_from_the_selection_fold(tmp_path):
    # A forecast observed before the selection cutoff whose label only settles in
    # the calibration window must not enter the selection fit through the JSONL
    # path — the leakage the availability fields exist to prevent.
    payloads = [
        _obs_payload(0, observed_day=3, feature_day=3, label_day=3),
        _obs_payload(1, observed_day=13, feature_day=13, label_day=13),
        _obs_payload(2, observed_day=25, feature_day=25, label_day=25),
        _obs_payload(3, observed_day=35, feature_day=35, label_day=35),
        _obs_payload(4, observed_day=5, feature_day=5, label_day=15),
    ]
    rows = load_observations_jsonl(_write_jsonl(tmp_path, payloads))
    folds = chronological_folds(
        rows,
        model_selection_through=START + timedelta(days=10),
        calibration_through=START + timedelta(days=20),
        validation_through=START + timedelta(days=30),
    )
    selection_ids = {r.event_id for r in folds.selection}
    calibration_ids = {r.event_id for r in folds.calibration}
    assert "event-4" not in selection_ids
    assert "event-4" in calibration_ids


def _dense_artifact_rows():
    def row(index, when):
        cycle = index % 8
        probability = .25 if cycle < 4 else .75
        outcome = float(cycle == 0 or cycle in {5, 6, 7})
        return EvaluationObservation(
            event_id=f"{when.date()}-{index}",
            observed_at=when + timedelta(seconds=index),
            sport="basketball", league="nba", market="moneyline",
            outcome=outcome,
            candidate_probabilities={"simple": probability},
            executable_cost=.5, execution_cost_error=(index % 3 - 1) * .001,
        )

    rows = []
    for day in range(4):
        rows += [row(i, START + timedelta(days=day)) for i in range(1000)]
    return rows


def _stub_uncertainty(monkeypatch):
    monkeypatch.setattr(
        "app.model_training.event_block_pipeline_uncertainty",
        lambda *args, **kwargs: tuple({
            "pipeline": "simple", "devig_method": "proportional",
            "consensus_method": "equal_family_logit", "sharp_source_family": None,
            "consensus_intercept": 0.0, "family_coefficients": {},
            "missing_family_coefficients": {},
            "beta_coefficients": BetaCoefficients(1, 1, 0).as_list(),
            "execution_cost_offset": 0.0,
        } for _ in range(200)),
    )


def _build_dense(monkeypatch, *, model_version, input_availability_declared):
    _stub_uncertainty(monkeypatch)
    return build_calibration_artifact(
        _dense_artifact_rows(),
        specifications={"simple": CandidateSpecification(
            devig_method="proportional", consensus_method="equal_family_logit")},
        model_selection_through=START + timedelta(hours=1),
        calibration_through=START + timedelta(days=1, hours=1),
        validation_through=START + timedelta(days=2, hours=1),
        model_version=model_version,
        sport="basketball", league="nba", market="moneyline",
        bootstrap_draws=200,
        input_availability_declared=input_availability_declared,
    )


def test_legacy_availability_marks_artifact_action_ineligible(tmp_path, monkeypatch):
    legacy = _build_dense(
        monkeypatch, model_version="legacy-test", input_availability_declared=False
    )
    assert legacy["action_eligible"] is False
    legacy_path = tmp_path / "legacy.json"
    write_artifact(legacy, legacy_path)
    assert CalibrationArtifact.load(legacy_path).eligible_for_action is False


def test_declared_availability_artifact_stays_action_eligible(tmp_path, monkeypatch):
    declared = _build_dense(
        monkeypatch, model_version="declared-test", input_availability_declared=True
    )
    assert declared["action_eligible"] is True
    declared_path = tmp_path / "declared.json"
    write_artifact(declared, declared_path)
    assert CalibrationArtifact.load(declared_path).eligible_for_action is True
