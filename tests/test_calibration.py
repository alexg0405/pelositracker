import json

import pytest

from app.calibration import CalibrationArtifact


def artifact(tmp_path, **overrides):
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
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_out_of_sample_calibration_artifact_loads(tmp_path):
    loaded = CalibrationArtifact.load(artifact(tmp_path))
    assert loaded.sample_size == 1500
    assert loaded.supported_markets == {"moneyline"}
    assert loaded.calibration_method == "identity"


def test_in_sample_or_small_artifact_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="out-of-sample"):
        CalibrationArtifact.load(artifact(
            tmp_path, evaluated_from="2025-12-01T00:00:00Z"))
    with pytest.raises(ValueError, match="too small"):
        CalibrationArtifact.load(artifact(tmp_path, sample_size=999))
    with pytest.raises(ValueError, match="identity calibration"):
        CalibrationArtifact.load(artifact(tmp_path, calibration_method="platt"))
