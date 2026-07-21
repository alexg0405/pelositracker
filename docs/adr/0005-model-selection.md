# ADR 0005: Model selection

Status: accepted.

The default is equal-weight logit pooling by independent source family. It is a
market consensus, not an independent sport model. Learned stacking,
calibration, and sport models stay ineligible until versioned chronological
out-of-sample artifacts beat simple baselines and declare exact supported
segments. Missing/invalid artifacts produce display-only `WATCH`; feature flags
alone cannot promote a model.

Version 2 uses event-grouped nested chronological selection, calibration,
validation, and untouched-test folds. The accepted calibrator is monotone beta
or identity; learned stacking must beat the equal-family baseline on validation
proper scores. Uncertainty is derived from aligned event-block coefficient and
execution-cost draws. Artifact construction and live installation are separate
operator actions, and v1 artifacts remain display-only.
