import json

import pytest

from app.calibration import CalibrationArtifact
from app.engine import SignalEngine


def _draws(count: int = 200):
    return [[1.0, 1.0, 0.0] for _ in range(count)]


def artifact_v1(tmp_path, **overrides):
    payload = {
        "artifact_version": "1",
        "model_version": "consensus-policy-1",
        "trained_through": "2025-12-31T23:59:59Z",
        "evaluated_from": "2026-01-01T00:00:00Z",
        "evaluated_through": "2026-06-30T23:59:59Z",
        "sample_size": 1500,
        "brier_score": 0.21,
        "log_loss": 0.62,
        "supported_markets": ["moneyline"],
        "calibration_method": "identity",
    }
    payload.update(overrides)
    path = tmp_path / "calibration-v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def artifact_v2(tmp_path, **segment_overrides):
    segment = {
        "sport": "*",
        "league": "*",
        "market": "moneyline",
        "sample_size": 1500,
        "devig_method": "shin",
        "consensus_method": "equal_family_logit",
        "calibration_method": "beta",
        "beta_coefficients": {"a": 1.0, "b": 1.0, "c": 0.0},
        "beta_bootstrap_coefficients": _draws(),
        "execution_cost_offsets": [0.0] * 200,
        "candidate_metrics": {
            "devig": {
                "shin": {"sample_size": 1200, "brier_score": 0.21, "log_loss": 0.61},
                "proportional": {
                    "sample_size": 1200, "brier_score": 0.22, "log_loss": 0.63
                },
            },
            "consensus": {
                "equal_family_logit": {
                    "sample_size": 1200, "brier_score": 0.21, "log_loss": 0.61
                },
            },
        },
        "min_probability_positive": 0.95,
        "min_expected_value_dollars": 1.0,
    }
    segment.update(segment_overrides)
    payload = {
        "artifact_version": "2",
        "model_version": "consensus-policy-2",
        "model_hash": "a" * 64,
        "model_trained_through": "2025-09-30T23:59:59Z",
        "calibration_trained_through": "2025-12-31T23:59:59Z",
        "validation_through": "2026-01-31T23:59:59Z",
        "evaluated_from": "2026-02-01T00:00:00Z",
        "evaluated_through": "2026-06-30T23:59:59Z",
        "sample_size": 1500,
        "brier_score": 0.21,
        "log_loss": 0.62,
        "uncertainty_method": "event_block_bootstrap",
        "segments": [segment],
    }
    path = tmp_path / "calibration-v2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_legacy_identity_artifact_loads_but_is_display_only(tmp_path):
    loaded = CalibrationArtifact.load(artifact_v1(tmp_path))
    assert loaded.sample_size == 1500
    assert loaded.supported_markets == {"moneyline"}
    assert loaded.calibration_method == "identity"
    assert not loaded.eligible_for_action
    assert loaded.policy_for("basketball", "nba", "moneyline") is None
    engine = SignalEngine()
    engine.install_calibration(loaded)
    assert engine._model_policies([], "basketball", "nba") == []


def test_actionable_v2_artifact_selects_policy_and_applies_beta_map(tmp_path):
    loaded = CalibrationArtifact.load(artifact_v2(tmp_path))
    policy = loaded.policy_for("basketball", "nba", "moneyline")
    assert loaded.eligible_for_action
    assert policy is not None
    assert policy.devig_method == "shin"
    assert policy.consensus_method == "equal_family_logit"
    assert policy.calibrate(0.37) == pytest.approx(0.37)
    assert len(policy.beta_bootstrap_coefficients) == 200
    assert len(policy.uncertainty_draws) == 200
    assert policy.uncertainty_draws[0]["consensus_method"] == "equal_family_logit"
    assert policy.to_engine_dict()["min_probability_positive"] == 0.95


def test_v2_hierarchical_policy_prefers_the_most_specific_segment(tmp_path):
    path = artifact_v2(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    specific = dict(payload["segments"][0])
    specific.update({"sport": "basketball", "league": "nba", "devig_method": "proportional"})
    payload["segments"].append(specific)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = CalibrationArtifact.load(path)
    policy = loaded.policy_for("basketball", "nba", "moneyline")
    fallback = loaded.policy_for("basketball", "wnba", "moneyline")
    assert policy is not None and policy.devig_method == "proportional"
    assert fallback is not None and fallback.devig_method == "shin"


def test_v2_hierarchy_prefers_league_market_before_sport_market(tmp_path):
    path = artifact_v2(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    sport_fallback = dict(payload["segments"][0])
    sport_fallback.update({
        "sport": "basketball", "league": "*", "devig_method": "proportional"
    })
    league_fallback = dict(payload["segments"][0])
    league_fallback.update({
        "sport": "*", "league": "nba", "devig_method": "shin"
    })
    payload["segments"] = [sport_fallback, league_fallback]
    path.write_text(json.dumps(payload), encoding="utf-8")

    policy = CalibrationArtifact.load(path).policy_for(
        "basketball", "nba", "moneyline"
    )

    assert policy is not None and policy.league == "nba"


def test_in_sample_small_or_statistically_incomplete_artifact_is_rejected(tmp_path):
    path = artifact_v2(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evaluated_from"] = "2025-12-01T00:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="out-of-sample"):
        CalibrationArtifact.load(path)

    with pytest.raises(ValueError, match="too small"):
        CalibrationArtifact.load(artifact_v2(tmp_path, sample_size=999))
    with pytest.raises(ValueError, match="bootstrap"):
        CalibrationArtifact.load(artifact_v2(
            tmp_path, beta_bootstrap_coefficients=_draws(199),
            execution_cost_offsets=[0.0] * 199,
        ))
    with pytest.raises(ValueError, match="monotone"):
        CalibrationArtifact.load(artifact_v2(
            tmp_path, beta_coefficients={"a": -1.0, "b": 1.0, "c": 0.0},
        ))


def test_selected_methods_require_recorded_chronological_candidate_metrics(tmp_path):
    with pytest.raises(ValueError, match="selected de-vig"):
        CalibrationArtifact.load(artifact_v2(
            tmp_path,
            candidate_metrics={
                "devig": {"proportional": {"sample_size": 1200, "log_loss": 0.63}},
                "consensus": {
                    "equal_family_logit": {
                        "sample_size": 1200, "brier_score": 0.21, "log_loss": 0.61
                    }
                },
            },
        ))
    with pytest.raises(ValueError, match="selected consensus"):
        CalibrationArtifact.load(artifact_v2(
            tmp_path,
            candidate_metrics={
                "devig": {
                    "shin": {"sample_size": 1200, "brier_score": 0.21, "log_loss": 0.61}
                },
                "consensus": {},
            },
        ))
    with pytest.raises(ValueError, match="insufficient sample"):
        CalibrationArtifact.load(artifact_v2(
            tmp_path,
            candidate_metrics={
                "devig": {
                    "shin": {"sample_size": 999, "brier_score": 0.21, "log_loss": 0.61}
                },
                "consensus": {
                    "equal_family_logit": {
                        "sample_size": 1200, "brier_score": 0.21, "log_loss": 0.61
                    }
                },
            },
        ))


def test_actionable_artifact_requires_validation_boundary_and_brier_metrics(tmp_path):
    path = artifact_v2(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("validation_through")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit validation interval"):
        CalibrationArtifact.load(path)

    path = artifact_v2(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["segments"][0]["candidate_metrics"]["devig"]["shin"]["brier_score"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Brier score"):
        CalibrationArtifact.load(path)
