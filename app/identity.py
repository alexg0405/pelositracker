from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import json
import math
import re
import unicodedata
from uuid import NAMESPACE_URL, uuid5

from .domain.time import ensure_utc
from .matching import start_timestamp, team_match_score


def canonical_text(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(char for char in raw if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", raw.casefold()))


def stable_id(kind: str, *parts: object) -> str:
    payload = ":".join([kind, *(canonical_text(part) for part in parts)])
    return str(uuid5(NAMESPACE_URL, f"pelositracker:{payload}"))


def canonical_line(line_value: object | None) -> str | None:
    """Alnum-safe token for a market line that PRESERVES the sign and decimal.

    ``canonical_text`` keeps only ``[a-z0-9]``, so it collapses ``-6.5`` and
    ``+6.5`` (and ``6.5``) to the same ``"6 5"`` -- which would make opposite
    spread lines share a market identity. Encoding the sign as a word and the
    point as ``p`` keeps the token alnum-safe (so it survives ``stable_id``'s own
    ``canonical_text``) while remaining distinct: ``neg6p5`` vs ``pos6p5``."""
    if line_value is None:
        return None
    try:
        value = float(line_value)  # type: ignore[arg-type]  # non-floatable handled below
    except (TypeError, ValueError):
        return canonical_text(line_value) or None
    return f"{'neg' if value < 0 else 'pos'}{abs(value):g}".replace(".", "p")


@dataclass(frozen=True, slots=True)
class CanonicalParticipant:
    participant_id: str
    sport: str
    canonical_name: str

    @classmethod
    def create(cls, sport: str, name: str) -> "CanonicalParticipant":
        normalized_sport = canonical_text(sport)
        normalized_name = canonical_text(name)
        return cls(stable_id("participant", normalized_sport, normalized_name),
                   normalized_sport, normalized_name)


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    canonical_event_id: str
    sport: str
    league: str
    starts_at: datetime | None
    home: CanonicalParticipant
    away: CanonicalParticipant

    @classmethod
    def create(cls, sport: str, league: str, starts_at: datetime | None,
               home: str, away: str) -> "CanonicalEvent":
        home_participant = CanonicalParticipant.create(sport, home)
        away_participant = CanonicalParticipant.create(sport, away)
        utc_start = ensure_utc(starts_at) if starts_at else None
        start_key = utc_start.isoformat() if utc_start else "unknown-start"
        return cls(
            stable_id("event", sport, league, start_key,
                      home_participant.participant_id, away_participant.participant_id),
            canonical_text(sport), canonical_text(league), utc_start,
            home_participant, away_participant,
        )


@dataclass(frozen=True, slots=True)
class CanonicalMarket:
    market_id: str
    canonical_event_id: str
    market_type: str
    line_value: str | None
    period_scope: str

    @classmethod
    def create(cls, event_id: str, market_type: str, line_value: object | None,
               period_scope: str = "full_game") -> "CanonicalMarket":
        line = canonical_line(line_value)
        kind = canonical_text(market_type)
        scope = canonical_text(period_scope)
        return cls(stable_id("market", event_id, kind, line or "none", scope),
                   event_id, kind, line, scope)


class MappingStatus(str, Enum):
    MAPPED = "mapped"
    AMBIGUOUS = "ambiguous"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class ProviderEventCandidate:
    provider_object_id: str
    home: str
    away: str
    starts_at: object
    sport: str
    league: str = ""


@dataclass(frozen=True, slots=True)
class MappingDecision:
    provider: str
    provider_object_id: str
    canonical_id: str | None
    status: MappingStatus
    confidence: float
    reason: str
    orientation: str = "unknown"
    algorithm_version: str = "fellegi-sunter-inspired-v1"
    threshold: float = 0.70
    human_override: bool = False
    evidence_json: str = "{}"


def resolve_event_mapping(provider: str, target: CanonicalEvent,
                          candidates: list[ProviderEventCandidate], *,
                          tolerance_seconds: float = 4 * 3600,
                          threshold: float = 0.70) -> MappingDecision:
    target_start = target.starts_at.timestamp() if target.starts_at else None
    eligible: list[tuple[float, ProviderEventCandidate, str, dict[str, float], bool]] = []
    for candidate in candidates:
        if canonical_text(candidate.sport) != target.sport:
            continue
        if target.league and canonical_text(candidate.league) not in {"", target.league}:
            continue
        direct = (team_match_score(target.home.canonical_name, candidate.home),
                  team_match_score(target.away.canonical_name, candidate.away))
        reverse = (team_match_score(target.home.canonical_name, candidate.away),
                   team_match_score(target.away.canonical_name, candidate.home))
        valid: list[tuple[str, tuple[int, int]]] = []
        if direct[0] is not None and direct[1] is not None:
            valid.append(("direct", (direct[0], direct[1])))
        if reverse[0] is not None and reverse[1] is not None:
            valid.append(("reversed", (reverse[0], reverse[1])))
        if not valid:
            continue
        orientation, scores = max(valid, key=lambda item: sum(item[1]))
        candidate_start = start_timestamp(candidate.starts_at)
        time_evidence = target_start is not None and candidate_start is not None
        if target_start is not None:
            if candidate_start is None:
                continue
            delta = abs(candidate_start - target_start)
            if delta > tolerance_seconds:
                continue
        else:
            delta = tolerance_seconds
        name_score = (scores[0] + scores[1]) / 204.0
        time_score = 1.0 - min(delta / tolerance_seconds, 1.0)
        components = {"participant_score": name_score, "start_time_score": time_score}
        eligible.append((0.75 * name_score + 0.25 * time_score,
                         candidate, orientation, components, time_evidence))

    if not eligible:
        return MappingDecision(provider, "", None, MappingStatus.QUARANTINED, 0.0,
                               "no candidate matched sport, both participants, and start time")
    eligible.sort(key=lambda item: (-item[0], item[1].provider_object_id))
    best_score, best, orientation, components, time_evidence = eligible[0]
    evidence = json.dumps({
        "candidate_ids": [item[1].provider_object_id for item in eligible],
        "best_components": components,
    }, sort_keys=True, separators=(",", ":"))
    if len(eligible) > 1 and abs(best_score - eligible[1][0]) < 0.03:
        return MappingDecision(provider, best.provider_object_id, None,
                               MappingStatus.AMBIGUOUS, best_score,
                               "multiple provider events have indistinguishable identity evidence",
                               orientation=orientation, threshold=threshold, evidence_json=evidence)
    if not time_evidence:
        return MappingDecision(provider, best.provider_object_id, None,
                               MappingStatus.AMBIGUOUS, best_score,
                               "no start-time evidence to distinguish a possible rematch",
                               orientation=orientation, threshold=threshold, evidence_json=evidence)
    if best_score < threshold:
        return MappingDecision(provider, best.provider_object_id, None,
                               MappingStatus.QUARANTINED, best_score,
                               f"match confidence {best_score:.2f} below threshold {threshold:.2f}",
                               orientation=orientation, threshold=threshold, evidence_json=evidence)
    return MappingDecision(provider, best.provider_object_id, target.canonical_event_id,
                           MappingStatus.MAPPED, min(best_score, 1.0),
                           "matched sport, league, both participants, and start time",
                           orientation=orientation, threshold=threshold, evidence_json=evidence)


# ---------------------------------------------------------------------------
# Fellegi-Sunter record linkage with a global one-to-one assignment.
#
# ``resolve_event_mapping`` above decides one target against a candidate list
# with a bounded heuristic score. The functions below implement the linkage the
# roadmap calls for: a weight-of-evidence score w(gamma) = sum_j log(m_j/u_j)
# over comparison fields (participants, orientation, league, scheduled start),
# two decision thresholds (link / non-link / possible), and a deterministic
# maximum-weight *one-to-one* assignment so two targets can never map to the
# same provider event. "Missing" is its own comparison level -- it is neither
# agreement nor disagreement and contributes zero evidence.
# ---------------------------------------------------------------------------

# Exact team match: matching.team_match_score returns 100 + token_count.
_EXACT_MATCH_SCORE = 101


@dataclass(frozen=True, slots=True)
class ComparisonWeights:
    """Declared log-likelihood-ratio weights ``log(m/u)`` per comparison level.

    These are principled defaults, not data-fitted m/u estimates; the structure
    (agreement is positive evidence, a far start time is strong negative
    evidence, missing is zero) is what matters and can be recalibrated later."""
    participants_exact: float = 6.0     # both sides exact token match
    participants_strong: float = 3.0    # both sides matched, at least one fuzzy
    league_agree: float = 0.5
    time_exact: float = 3.0             # within 5 minutes
    time_close: float = 1.0             # within tolerance
    time_far: float = -10.0             # beyond tolerance -> a different fixture
    link_threshold: float = 3.0
    nonlink_threshold: float = 0.0
    ambiguity_margin: float = 0.75      # rival within this weight => unresolved


_DEFAULT_WEIGHTS = ComparisonWeights()


@dataclass(frozen=True, slots=True)
class MappingOverride:
    """Immutable provenance for a human override of a linkage decision."""
    actor: str
    at: str
    reason: str
    old_evidence: str
    new_evidence: str


@dataclass(frozen=True, slots=True)
class LinkageDecision:
    provider: str
    target_canonical_id: str
    provider_object_id: str | None
    status: MappingStatus
    weight: float                 # Fellegi-Sunter total log-likelihood ratio
    posterior: float              # logistic(weight), interpretable in [0, 1]
    orientation: str
    reason: str
    link_threshold: float
    nonlink_threshold: float
    algorithm_version: str = "fellegi-sunter-v1"
    evidence_json: str = "{}"
    override: MappingOverride | None = None


@dataclass(frozen=True, slots=True)
class _ScoredEdge:
    weight: float
    orientation: str
    participant_level: str
    time_level: str
    start_evidence: bool          # both starts present AND within tolerance
    components: dict[str, float]


def _logistic(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _participant_evidence(
    target: CanonicalEvent, candidate: ProviderEventCandidate, weights: ComparisonWeights
) -> tuple[str, float, str] | None:
    """Return ``(orientation, weight, level)`` for the participant comparison, or
    ``None`` when the two sides do not both match in either orientation."""
    direct = (team_match_score(target.home.canonical_name, candidate.home),
              team_match_score(target.away.canonical_name, candidate.away))
    reverse = (team_match_score(target.home.canonical_name, candidate.away),
               team_match_score(target.away.canonical_name, candidate.home))
    options: list[tuple[str, tuple[int, int]]] = []
    if direct[0] is not None and direct[1] is not None:
        options.append(("direct", (direct[0], direct[1])))
    if reverse[0] is not None and reverse[1] is not None:
        options.append(("reversed", (reverse[0], reverse[1])))
    if not options:
        return None
    orientation, (home_score, away_score) = max(options, key=lambda item: sum(item[1]))
    if home_score >= _EXACT_MATCH_SCORE and away_score >= _EXACT_MATCH_SCORE:
        return orientation, weights.participants_exact, "exact_both"
    return orientation, weights.participants_strong, "strong_both"


def _time_evidence(
    target: CanonicalEvent, candidate: ProviderEventCandidate, tolerance: float,
    weights: ComparisonWeights,
) -> tuple[str, float, bool]:
    """Return ``(level, weight, start_evidence)`` for the scheduled-start comparison.

    A missing start on either side is its own level: zero evidence and no
    start-time evidence to rule out a rematch."""
    target_start = target.starts_at.timestamp() if target.starts_at else None
    candidate_start = start_timestamp(candidate.starts_at)
    if target_start is None or candidate_start is None:
        return "missing", 0.0, False
    delta = abs(candidate_start - target_start)
    if delta <= 300.0:
        return "exact", weights.time_exact, True
    if delta <= tolerance:
        return "close", weights.time_close, True
    return "far", weights.time_far, False


def _score_edge(
    target: CanonicalEvent, candidate: ProviderEventCandidate, tolerance: float,
    weights: ComparisonWeights,
) -> _ScoredEdge | None:
    """Fellegi-Sunter weight for one target/candidate pair, or ``None`` when the
    pair is not comparable (different sport, disagreeing league, or the
    participants do not both match)."""
    if canonical_text(candidate.sport) != target.sport:
        return None
    candidate_league = canonical_text(candidate.league)
    if target.league and candidate_league and candidate_league != target.league:
        return None  # league disagreement: not the same competition
    participants = _participant_evidence(target, candidate, weights)
    if participants is None:
        return None
    orientation, participant_weight, level = participants
    league_weight = (weights.league_agree
                     if target.league and candidate_league == target.league else 0.0)
    time_level, time_weight, start_evidence = _time_evidence(
        target, candidate, tolerance, weights)
    total = participant_weight + league_weight + time_weight
    components = {
        "participants": participant_weight,
        "league": league_weight,
        "start_time": time_weight,
    }
    return _ScoredEdge(total, orientation, level, time_level, start_evidence, components)


def _linkage_evidence(edge: _ScoredEdge, competitor_ids: list[str]) -> str:
    return json.dumps({
        "weight_components": edge.components,
        "participant_level": edge.participant_level,
        "start_time_level": edge.time_level,
        "competitor_candidate_ids": sorted(competitor_ids),
    }, sort_keys=True, separators=(",", ":"))


def resolve_event_mappings(
    provider: str, targets: list[CanonicalEvent],
    candidates: list[ProviderEventCandidate], *,
    tolerance_seconds: float = 4 * 3600,
    weights: ComparisonWeights = _DEFAULT_WEIGHTS,
) -> list[LinkageDecision]:
    """Link a set of targets to provider candidates with Fellegi-Sunter weights
    and a deterministic maximum-weight one-to-one assignment.

    Returns one :class:`LinkageDecision` per target, in input order. A target is
    linked only to an unambiguous, dominant candidate whose start time is
    corroborated; contested, possible-band, or missing-start pairs are left
    unresolved (``AMBIGUOUS``), and a candidate is never assigned to two targets.
    Candidate/target input order does not affect the result.
    """
    edges: dict[tuple[int, int], _ScoredEdge] = {}
    for ti, target in enumerate(targets):
        for ci, candidate in enumerate(candidates):
            scored = _score_edge(target, candidate, tolerance_seconds, weights)
            if scored is not None:
                edges[(ti, ci)] = scored

    # Link-eligible edges: dominant weight and corroborated start time. Sorted by
    # descending weight with deterministic id tie-breaks so input order is moot.
    eligible = sorted(
        (
            (edge.weight, ti, ci)
            for (ti, ci), edge in edges.items()
            if edge.weight >= weights.link_threshold and edge.start_evidence
        ),
        key=lambda item: (-item[0], targets[item[1]].canonical_event_id,
                          candidates[item[2]].provider_object_id),
    )

    decisions: list[LinkageDecision | None] = [None] * len(targets)
    taken_targets: set[int] = set()
    taken_candidates: set[int] = set()
    for weight, ti, ci in eligible:
        if ti in taken_targets or ci in taken_candidates:
            continue
        # A still-available rival edge on the same target or candidate that is
        # within the ambiguity margin means we cannot confidently choose.
        rival_within_margin = any(
            (tj == ti or cj == ci)
            and (tj, cj) != (ti, ci)
            and tj not in taken_targets and cj not in taken_candidates
            and (weight - other_weight) < weights.ambiguity_margin
            for other_weight, tj, cj in eligible
        )
        if rival_within_margin:
            continue
        taken_targets.add(ti)
        taken_candidates.add(ci)
        edge = edges[(ti, ci)]
        competitors = [candidates[cj].provider_object_id
                       for (tj, cj) in edges if tj == ti and cj != ci]
        decisions[ti] = LinkageDecision(
            provider, targets[ti].canonical_event_id, candidates[ci].provider_object_id,
            MappingStatus.MAPPED, weight, _logistic(weight), edge.orientation,
            "linked: dominant, unambiguous, start-time corroborated candidate",
            weights.link_threshold, weights.nonlink_threshold,
            evidence_json=_linkage_evidence(edge, competitors))

    for ti, target in enumerate(targets):
        if decisions[ti] is not None:
            continue
        target_edges = sorted(
            ((edges[(ti, ci)].weight, ci) for ci in range(len(candidates))
             if (ti, ci) in edges),
            key=lambda item: (-item[0], candidates[item[1]].provider_object_id),
        )
        if not target_edges:
            decisions[ti] = LinkageDecision(
                provider, target.canonical_event_id, None, MappingStatus.QUARANTINED,
                float("-inf"), 0.0, "unknown",
                "no candidate matched sport, league, and both participants",
                weights.link_threshold, weights.nonlink_threshold)
            continue
        best_weight, best_ci = target_edges[0]
        edge = edges[(ti, best_ci)]
        competitors = [candidates[cj].provider_object_id
                       for (tj, cj) in edges if tj == ti and cj != best_ci]
        evidence = _linkage_evidence(edge, competitors)
        object_id = candidates[best_ci].provider_object_id
        if best_weight <= weights.nonlink_threshold:
            status, reason = (MappingStatus.QUARANTINED,
                              f"weight {best_weight:.2f} at or below non-link "
                              f"{weights.nonlink_threshold:.2f}")
        elif best_ci in taken_candidates:
            status, reason = (MappingStatus.AMBIGUOUS,
                              "best provider event already linked to a higher-weight target")
        elif not edge.start_evidence:
            status, reason = (MappingStatus.AMBIGUOUS,
                              "no start-time evidence to distinguish a possible rematch")
        else:
            status, reason = (MappingStatus.AMBIGUOUS,
                              "match weight in the possible band or contested by a rival candidate")
        decisions[ti] = LinkageDecision(
            provider, target.canonical_event_id, object_id, status, best_weight,
            _logistic(best_weight), edge.orientation, reason,
            weights.link_threshold, weights.nonlink_threshold, evidence_json=evidence)

    return [decision for decision in decisions if decision is not None]


def apply_linkage_override(decision: LinkageDecision, *, actor: str,
                           at: datetime | str, reason: str,
                           provider_object_id: str | None,
                           status: MappingStatus = MappingStatus.MAPPED,
                           new_evidence: str = "{}") -> LinkageDecision:
    """Return a copy of ``decision`` with a human override applied and its full
    provenance (actor, timestamp, reason, old and new evidence) recorded."""
    timestamp = ensure_utc(at).isoformat() if isinstance(at, datetime) else str(at)
    override = MappingOverride(actor, timestamp, reason,
                               old_evidence=decision.evidence_json, new_evidence=new_evidence)
    return LinkageDecision(
        decision.provider, decision.target_canonical_id, provider_object_id, status,
        decision.weight, decision.posterior, decision.orientation,
        f"human override by {actor}: {reason}", decision.link_threshold,
        decision.nonlink_threshold, decision.algorithm_version, new_evidence, override)
