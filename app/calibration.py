from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from .domain.time import parse_provider_timestamp


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    artifact_version: str
    model_version: str
    trained_through: datetime
    evaluated_from: datetime
    evaluated_through: datetime
    sample_size: int
    brier_score: float
    log_loss: float
    supported_markets: frozenset[str]
    calibration_method: str

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationArtifact":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        artifact = cls(
            artifact_version=str(payload["artifact_version"]),
            model_version=str(payload["model_version"]),
            trained_through=parse_provider_timestamp(payload["trained_through"]),
            evaluated_from=parse_provider_timestamp(payload["evaluated_from"]),
            evaluated_through=parse_provider_timestamp(payload["evaluated_through"]),
            sample_size=int(payload["sample_size"]),
            brier_score=float(payload["brier_score"]),
            log_loss=float(payload["log_loss"]),
            supported_markets=frozenset(str(value).strip().casefold()
                                        for value in payload["supported_markets"]),
            calibration_method=str(payload["calibration_method"]).strip().casefold(),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if self.artifact_version != "1":
            raise ValueError("unsupported calibration artifact version")
        if self.trained_through >= self.evaluated_from:
            raise ValueError("calibration evaluation is not out-of-sample")
        if self.evaluated_from >= self.evaluated_through:
            raise ValueError("invalid calibration evaluation interval")
        if self.sample_size < 1000:
            raise ValueError("calibration sample is too small for policy eligibility")
        if not (0 <= self.brier_score <= 1) or self.log_loss < 0:
            raise ValueError("invalid calibration metrics")
        if not self.supported_markets:
            raise ValueError("calibration artifact supports no markets")
        if self.calibration_method != "identity":
            raise ValueError("only chronologically validated identity calibration is supported")


def load_calibration(path: str | None) -> CalibrationArtifact | None:
    return CalibrationArtifact.load(path) if path else None
