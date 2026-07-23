"""Familywise multiplicity control for model search.

When several candidate pipelines are searched and the best-scoring one is kept,
its edge over a benchmark is upward-biased by the search itself. These functions
judge "the best of N candidates" against the right null with an event-clustered
bootstrap: White's Reality Check (is *any* searched candidate better than the
benchmark?) and the Romano-Wolf stepdown (per-candidate familywise-error-adjusted
p-values). Both are one-sided in the direction "candidate beats the benchmark".

Inputs are per-observation loss differences ``benchmark_loss - candidate_loss``
(positive => the candidate scored better), plus the ``event_ids`` they belong to
so that overlapping observations from one event resample together. Everything is
dependency-free and deterministic under a fixed seed.
"""
from __future__ import annotations

import random


def _per_event_mean_vector(diffs: list[float], event_ids: list[str],
                           events: list[str]) -> list[float]:
    """Mean loss-difference within each event, aligned to ``events`` order."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for value, event in zip(diffs, event_ids, strict=True):
        sums[event] = sums.get(event, 0.0) + value
        counts[event] = counts.get(event, 0) + 1
    return [sums[event] / counts[event] for event in events]


def _prepare(diffs_by_candidate: dict[str, list[float]], event_ids: list[str]
             ) -> tuple[list[str], list[str], dict[str, list[float]]]:
    candidates = sorted(diffs_by_candidate)
    if not candidates:
        raise ValueError("at least one candidate is required")
    events = sorted(set(event_ids))
    if not events:
        raise ValueError("at least one event is required")
    matrix = {
        name: _per_event_mean_vector(diffs_by_candidate[name], event_ids, events)
        for name in candidates
    }
    return candidates, events, matrix


def _observed(matrix: dict[str, list[float]], candidates: list[str],
              event_count: int) -> dict[str, float]:
    return {name: sum(matrix[name]) / event_count for name in candidates}


def _bootstrap_centered(candidates: list[str], matrix: dict[str, list[float]],
                        observed: dict[str, float], draws: int, seed: int,
                        event_count: int) -> list[dict[str, float]]:
    """Event-block bootstrap of the candidate statistics, recentered under the
    null (subtracting the observed mean) so the resampled maxima estimate the
    familywise null distribution."""
    generator = random.Random(seed)
    centered: list[dict[str, float]] = []
    for _ in range(draws):
        index = [generator.randrange(event_count) for _ in range(event_count)]
        centered.append({
            name: sum(matrix[name][i] for i in index) / event_count - observed[name]
            for name in candidates
        })
    return centered


def reality_check_pvalue(diffs_by_candidate: dict[str, list[float]],
                         event_ids: list[str], *, draws: int = 1000,
                         seed: int = 0) -> float:
    """White's Reality Check p-value for ``H0: no candidate beats the benchmark``."""
    candidates, events, matrix = _prepare(diffs_by_candidate, event_ids)
    observed = _observed(matrix, candidates, len(events))
    centered = _bootstrap_centered(candidates, matrix, observed, draws, seed, len(events))
    observed_max = max(observed.values())
    exceed = sum(1 for row in centered if max(row.values()) >= observed_max)
    return (1 + exceed) / (draws + 1)


def romano_wolf_pvalues(diffs_by_candidate: dict[str, list[float]],
                        event_ids: list[str], *, draws: int = 1000,
                        seed: int = 0) -> dict[str, float]:
    """Romano-Wolf stepdown familywise-error-adjusted p-values per candidate.

    Candidates are considered in decreasing observed advantage; each adjusted
    p-value is computed against the bootstrap maximum over the still-active
    candidates and is enforced monotone down the ordering."""
    candidates, events, matrix = _prepare(diffs_by_candidate, event_ids)
    observed = _observed(matrix, candidates, len(events))
    centered = _bootstrap_centered(candidates, matrix, observed, draws, seed, len(events))
    order = sorted(candidates, key=lambda name: (-observed[name], name))
    remaining = list(candidates)
    adjusted: dict[str, float] = {}
    running = 0.0
    for name in order:
        exceed = sum(
            1 for row in centered if max(row[key] for key in remaining) >= observed[name]
        )
        running = max(running, (1 + exceed) / (draws + 1))
        adjusted[name] = running
        remaining.remove(name)
    return adjusted
